from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tax_rag.config import PROJECT_ROOT, load_toml
from tax_rag.domain.answer_contract import AnswerValidationError, validate_answer_payload
from tax_rag.domain.conversation import answer_task_for_question


class DeepSeekError(RuntimeError):
    def __init__(self, code: str, message: str, diagnostics: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class GenerationResult:
    answer: dict[str, Any]
    model_id: str
    prompt_version: str
    response_id: str | None
    request_context_hash: str
    latency_seconds: float = 0.0
    api_attempts: int = 1
    syntax_recovery: dict[str, Any] | None = None
    optional_fields_defaulted: tuple[str, ...] = ()
    thinking_mode: str | None = None
    raw_answer_text: str | None = None


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[7:-3].strip()
    if text.startswith("```") and text.endswith("```"):
        return text[3:-3].strip()
    return text


def parse_answer_json(content: str) -> tuple[Any, dict | None]:
    text = _strip_json_fence(content)
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        if exc.pos != len(text) or exc.msg != "Expecting ',' delimiter":
            raise
        stack = []
        quoted = False
        escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if not stack or stack.pop() != char:
                    raise exc
        if quoted or not 1 <= len(stack) <= 2:
            raise
        suffix = "".join(reversed(stack))
        repaired = json.loads(text + suffix)
        return repaired, {"type": "missing_eof_delimiters", "appended": suffix,
                          "original_error": exc.msg, "offset": exc.pos,
                          "original_response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def restore_answer_field_levels(payload: Any) -> tuple[Any, dict | None]:
    """Restore the single observed misplaced sibling-field pattern without adding values."""
    if not isinstance(payload, dict) or payload.get("presentation_version") != "points-v3":
        return payload, None
    sibling_keys = {"prerequisites", "essential_explanation", "critical_gaps", "rules", "exceptions",
                    "uncertainties", "calculation_explanation", "follow_up_question"}
    direct = payload.get("direct_conclusion")
    if (set(payload) != {"presentation_version", "status", "direct_conclusion"} or not isinstance(direct, dict)
            or "items" not in direct or not set(direct).issubset(sibling_keys | {"items"})
            or not set(direct).intersection(sibling_keys)):
        return payload, None
    moved_keys = set(direct).intersection(sibling_keys)
    return {"presentation_version": payload["presentation_version"], "status": payload["status"],
            "direct_conclusion": {"items": direct["items"]},
            **{key: direct[key] for key in moved_keys}}, {
                "type": "misnested_answer_fields", "moved_keys": sorted(moved_keys)}


def restore_points_version(payload: Any) -> tuple[Any, dict | None]:
    """Identify an omitted version only from the unambiguous points-v3 container."""
    if not isinstance(payload, dict) or "presentation_version" in payload:
        return payload, None
    direct = payload.get("direct_conclusion")
    if not isinstance(direct, dict) or set(direct) != {"items"} or not isinstance(direct["items"], list):
        return payload, None
    return {**payload, "presentation_version": "points-v3"}, {"type": "omitted_points_version"}


def resolve_citation_ids(payload: Any, hits: list[Any]) -> Any:
    """Resolve only server-issued short IDs before the existing strict validation."""
    if not isinstance(payload, dict):
        return payload
    aliases = {f"E{index}": hit.chunk_id for index, hit in enumerate(hits, start=1)}
    direct = payload.get("direct_conclusion")
    items = [direct]
    if isinstance(direct, dict) and isinstance(direct.get("items"), list):
        items.extend(direct["items"])
    for key in ("rules", "exceptions", "essential_explanation"):
        if isinstance(payload.get(key), list):
            items.extend(payload[key])
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("citation_chunk_ids"), list):
            item["citation_chunk_ids"] = [aliases.get(value, value) if isinstance(value, str) else value for value in item["citation_chunk_ids"]]
    return payload


def validate_effective_date_claims(answer: dict, hits: list[Any]) -> None:
    """Check explicit effective-date statements against the cited document metadata."""
    evidence = {hit.chunk_id: hit for hit in hits}
    direct = answer["direct_conclusion"]
    statements = direct.get("items") or [direct]
    statements = [*statements, *answer.get("rules", []), *answer.get("exceptions", [])]
    pattern = re.compile(
        r"(《([^》]+)》|该法|本法|该条例|本条例)(?:自|将自|已于|于|规定自)?\s*"
        r"(20\d{2})[年-](\d{1,2})[月-](\d{1,2})日?起?(?:正式)?(?:施行|实施|生效)"
    )
    for statement in statements:
        cited = [evidence[cid] for cid in statement.get("citation_chunk_ids", []) if cid in evidence]
        for match in pattern.finditer(statement.get("text", statement.get("claim", ""))):
            title = match[2]
            candidates = [hit for hit in cited if (
                title and (hit.document_title == title or hit.document_title.removeprefix("中华人民共和国") == title)
            ) or (not title and hit.document_title.endswith("条例" if "条例" in match[1] else "法"))]
            dates = {hit.source.get("version", {}).get("effective_from") for hit in candidates}
            dates.discard(None)
            if len(dates) != 1:
                continue
            claimed = f"{int(match[3]):04d}-{int(match[4]):02d}-{int(match[5]):02d}"
            if claimed not in dates:
                raise DeepSeekError("EFFECTIVE_DATE_MISMATCH", "回答中的文件施行日期与本轮来源元数据不一致", {
                    "stage": "effective_date", "document_title": candidates[0].document_title,
                    "claimed_date": claimed, "verified_date": next(iter(dates)),
                })


class DeepSeekService:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_toml("p2b.toml")
        deepseek = self.config["deepseek"]
        prompt = self.config["prompt"]
        self.base_url = str(deepseek["base_url"]).rstrip("/")
        self.model_id = str(deepseek["model"])
        self.api_key_env = str(deepseek["api_key_env"])
        self.timeout_seconds = int(deepseek["timeout_seconds"])
        self.trust_env = bool(deepseek.get("trust_env", False))
        self.max_retries = int(deepseek["max_retries"])
        self.temperature = float(deepseek["temperature"])
        self.max_tokens = int(deepseek["max_tokens"])
        self.thinking_mode = deepseek.get("thinking_mode")
        if self.thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_mode需要enabled、disabled或不配置")
        self.prompt_version = str(prompt["version"])
        self.system_text = (PROJECT_ROOT / Path(prompt["system_text_path"])).read_text(
            encoding="utf-8"
        )

    def build_context(
        self,
        query: str,
        hits: list[Any],
        calculation: dict[str, Any] | None,
        conversation_context: list[dict] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        context = {
            "answer_task": answer_task_for_question(query),
            "scope": "服务覆盖中国境内一般纳税人一般计税及小规模纳税人销售货物基础咨询。这是服务范围，用户的主体、身份和计税方法仍须以用户原话为准。",
            "question": query,
            "approved_evidence": [
                {
                    "chunk_id": hit.chunk_id,
                    "document_title": hit.document_title,
                    "anchor": hit.anchor,
                    "text": hit.text,
                    "source_locator": hit.source,
                    "citation_number": index,
                    "citation_id": f"E{index}",
                    "version": hit.source.get("version", {}),
                    "period_status": hit.source.get("period_status"),
                }
                for index, hit in enumerate(hits, start=1)
            ],
            "local_decimal_calculation": calculation,
        }
        if conversation_context:
            context["conversation_history"] = conversation_context
        if as_of:
            context["policy_as_of"] = as_of
        context["knowledge_verified_on"] = "2026-11-15"
        context["document_effective_dates"] = list({
            (hit.document_title, hit.source.get("version", {}).get("effective_from"))
            for hit in hits if hit.source.get("version", {}).get("effective_from")
        })
        context["document_effective_dates"].sort()
        return context

    def generate(
        self,
        query: str,
        hits: list[Any],
        calculation: dict[str, Any] | None = None,
        *,
        conversation_context: list[dict] | None = None,
        as_of: str | None = None,
    ) -> GenerationResult:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise DeepSeekError("API_KEY_MISSING", "DeepSeek API Key环境变量不存在")
        context = self.build_context(query, hits, calculation, conversation_context, as_of)
        context_json = json.dumps(context, ensure_ascii=False, sort_keys=True)
        context_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        request_payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": self.system_text},
                {"role": "user", "content": context_json},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.thinking_mode is not None:
            request_payload["thinking"] = {"type": self.thinking_mode}
        response_data: dict[str, Any] | None = None
        last_error: Exception | None = None
        started = time.monotonic()
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds, trust_env=self.trust_env) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=request_payload,
                    )
                response.raise_for_status()
                response_data = response.json()
                break
            except httpx.TimeoutException as exc:
                last_error = exc
                error_code = "TIMEOUT"
            except httpx.HTTPStatusError as exc:
                last_error = exc
                error_code = f"HTTP_{exc.response.status_code}"
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    break
            except (httpx.TransportError, ValueError) as exc:
                last_error = exc
                error_code = "TRANSPORT_OR_RESPONSE_ERROR"
            if attempt >= self.max_retries:
                break
        if response_data is None:
            raise DeepSeekError(error_code, "DeepSeek解释生成失败") from last_error
        try:
            finish_reason = response_data["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("INVALID_JSON", "DeepSeek未返回有效结构化JSON") from exc
        if finish_reason == "length":
            raise DeepSeekError("TRUNCATED_RESPONSE", "DeepSeek结构化回答因输出上限被截断")
        try:
            content = response_data["choices"][0]["message"]["content"]
            payload, syntax_recovery = parse_answer_json(content)
            payload, version_recovery = restore_points_version(payload)
            payload, level_recovery = restore_answer_field_levels(payload)
            if version_recovery or level_recovery:
                syntax_recovery = {"steps": [step for step in (syntax_recovery, version_recovery, level_recovery) if step]}
        except json.JSONDecodeError as exc:
            raise DeepSeekError("INVALID_JSON", "DeepSeek未返回有效结构化JSON", {
                "stage": "json_parse", "message": exc.msg, "line": exc.lineno, "column": exc.colno,
                "offset": exc.pos, "nearby": content[max(0, exc.pos - 80):exc.pos + 80],
                "response_length": len(content), "latency_seconds": round(time.monotonic() - started, 3),
                "raw_answer_text": content,
                "raw_answer_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }) from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekError("INVALID_JSON", "DeepSeek未返回有效结构化JSON", {"stage": "response_shape", "error_type": type(exc).__name__}) from exc
        optional_fields_defaulted = tuple(key for key in ("prerequisites", "rules", "exceptions", "uncertainties", "essential_explanation", "critical_gaps") if isinstance(payload, dict) and key not in payload)
        try:
            answer = validate_answer_payload(
                resolve_citation_ids(payload, hits), {hit.chunk_id for hit in hits}, calculation,
            )
        except AnswerValidationError as exc:
            exc.diagnostics = {
                "stage": "answer_contract", "field_types": {key: type(value).__name__ for key, value in payload.items()},
                "latency_seconds": round(time.monotonic() - started, 3),
                "raw_answer_text": content,
                "raw_answer_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            } if isinstance(payload, dict) else {"stage": "answer_contract", "payload_type": type(payload).__name__}
            raise
        validate_effective_date_claims(answer, hits)
        return GenerationResult(
            answer=answer,
            model_id=response_data.get("model") or self.model_id,
            prompt_version=self.prompt_version,
            response_id=response_data.get("id"),
            request_context_hash=context_hash,
            latency_seconds=round(time.monotonic() - started, 3),
            api_attempts=attempt + 1,
            syntax_recovery=syntax_recovery,
            optional_fields_defaulted=optional_fields_defaulted,
            thinking_mode=self.thinking_mode,
            raw_answer_text=content,
        )
