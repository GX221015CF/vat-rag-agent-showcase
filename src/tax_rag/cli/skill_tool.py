from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from tax_rag.config import PROJECT_ROOT, load_toml
from tax_rag.domain.calculation import CalculationInputError, calculate_period_summary, calculate_single_transaction
from tax_rag.services.retrieval_service import ActiveRetrievalService
from tax_rag.services.evidence_service import metadata, period_status


class ToolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def required_text(request: dict, field: str, maximum: int = 4000) -> str:
    value = request.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ToolError("INPUT_ERROR", f"{field}需要非空字符串，长度上限{maximum}")
    return value.strip()


def service_for(request: dict) -> ActiveRetrievalService:
    staging = request.get("candidate_report")
    if not staging:
        return ActiveRetrievalService()
    if not isinstance(staging, str):
        raise ToolError("INPUT_ERROR", "candidate_report需要项目内报告路径字符串")
    path = Path(staging).resolve()
    if not path.is_relative_to(PROJECT_ROOT / "artifacts/skill_v1"):
        raise ToolError("INPUT_ERROR", "候选报告必须位于本项目artifacts/skill_v1")
    report = json.loads(path.read_text(encoding="utf-8"))
    knowledge = Path(report["knowledge_path"]).resolve()
    chroma = Path(report["chroma_path"]).resolve()
    allowed = (PROJECT_ROOT / "data/skill_v1/releases").resolve()
    if not knowledge.is_relative_to(allowed) or not chroma.is_relative_to(allowed):
        raise ToolError("INPUT_ERROR", "候选库路径超出本项目release目录")
    return ActiveRetrievalService(knowledge_path=knowledge, chroma_source_path=chroma, chroma_snapshot_root=knowledge.parent / "query_snapshots")


def scope_check(request: dict) -> None:
    scope = request.get("scope", {})
    if not isinstance(scope, dict):
        raise ToolError("INPUT_ERROR", "scope需要对象")
    supported = {"taxpayer_type": "general", "business": "domestic_goods", "method": "general"}
    for key, expected in supported.items():
        if key in scope and scope[key] != expected:
            raise ToolError("OUT_OF_SCOPE", f"首版{key}仅支持{expected}，收到{scope[key]}")


def retrieve(request: dict, *, lookup: bool = False) -> dict:
    scope_check(request)
    try:
        as_of = date.fromisoformat(request.get("as_of") or date.today().isoformat())
    except (TypeError, ValueError) as exc:
        raise ToolError("INPUT_ERROR", "as_of应为YYYY-MM-DD日期") from exc
    if as_of < date(2026, 1, 1):
        raise ToolError("EVIDENCE_PERIOD_UNSUPPORTED", "当前库以2026年施行的增值税法为核心；历史交易需要当时的增值税暂行条例、细则及相关有效政策，本次不以现行法倒推")
    unresolved = request.get("unresolved")
    if unresolved is not None and not isinstance(unresolved, bool):
        raise ToolError("INPUT_ERROR", "unresolved应为布尔值，表示历史事项在过渡期内是否未处理")
    service = service_for(request)
    try:
        if lookup:
            evidence_id = required_text(request, "evidence_id", 100)
            with closing(service.connect_readonly()) as connection:
                generation = service.active_generation(connection)
                row = connection.execute(
                    "SELECT c.*,d.title AS document_title FROM chunks c JOIN documents d USING(document_id) JOIN index_members i USING(chunk_id) WHERE c.chunk_id=? AND i.generation_id=? AND d.current_version_id=c.version_id AND d.approval_status='APPROVED_ACTIVE' AND c.retrieval_enabled=1",
                    (evidence_id, generation["generation_id"]),
                ).fetchone()
                if row is None:
                    raise ToolError("EVIDENCE_INSUFFICIENT", "该证据不在当前活动索引中，请重新检索")
                source = metadata(connection, row["version_id"])
                state = period_status(source, as_of, unresolved)
                if state == "outside_effective_period":
                    raise ToolError("EVIDENCE_PERIOD_UNSUPPORTED", "该证据在指定期间不适用")
                return {"generation_id": generation["generation_id"], "as_of": as_of.isoformat(), "evidence": [{"evidence_id": row["chunk_id"], "document_title": row["document_title"], "anchor": row["anchor"], "text": row["text"], "source": json.loads(row["source_locator_json"]), "version": source, "period_status": state}]}
        query = required_text(request, "query")
        context = request.get("context", "")
        if not isinstance(context, str) or len(context) > 3000:
            raise ToolError("INPUT_ERROR", "context需要不超过3000字符的已确认事实摘要")
        effective_query = "增值税 " + (context.strip() + " " if context.strip() else "") + query
        generation, hits, channels, plan = service.search_with_trace(effective_query)
        evidence = []
        excluded = []
        with closing(service.connect_readonly()) as connection:
            for hit in hits:
                source = metadata(connection, hit.version_id)
                state = period_status(source, as_of, unresolved)
                if state == "outside_effective_period":
                    excluded.append({"title": hit.document_title, "reason": state})
                    continue
                evidence.append({"evidence_id": hit.chunk_id, "document_id": hit.document_id, "version_id": hit.version_id, "document_title": hit.document_title, "anchor": hit.anchor, "text": hit.text, "source": hit.source, "version": source, "period_status": state, "channel_ranks": hit.ranks})
        if not evidence:
            raise ToolError("EVIDENCE_INSUFFICIENT", "当前活动资料未返回适用期间内的证据，请补充具体事项或核对缺失来源")
        return {"query": query, "context_used": context, "effective_query": effective_query, "as_of": as_of.isoformat(), "knowledge_verified_on": "2026-11-14", "generation_id": generation["generation_id"], "query_plan": plan.as_dict(), "channels": {name: len(rows) for name, rows in channels.items()}, "evidence": evidence, "excluded": excluded, "coverage_note": "结果为候选证据，结论须逐条核对正文；资料核验时点之后的法规变化需另行核查"}
    finally:
        service.close()


def execute(request: Any) -> dict:
    action = request.get("action") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict):
            raise ToolError("INPUT_ERROR", "请求需要JSON对象")
        if not isinstance(action, str):
            raise ToolError("INPUT_ERROR", "action需要字符串")
        if action in {"search", "evidence"}:
            data = retrieve(request, lookup=action == "evidence")
        elif action in {"single", "period"}:
            if request.get("confirmed") is not True:
                raise ToolError("INPUT_ERROR", "计算前需confirmed=true，确认金额口径、税率或期间四项输入；工具不判断抵扣资格")
            if action == "single":
                result = calculate_single_transaction(required_text(request, "amount", 50), required_text(request, "amount_type", 30), required_text(request, "tax_rate_percent", 50))
            else:
                result = calculate_period_summary(*(required_text(request, name, 50) for name in ("output_tax", "deductible_input_tax", "input_tax_transfer_out", "prior_credit")))
            data = {**result.as_dict(), "confirmation": True, "rounding": {"quantum": "0.01", "mode": "ROUND_HALF_UP"}, "scope_note": "期间结果为一般计税四输入的基础汇总，特殊纳税调整、减免、预缴和附加税费需单独核对"}
        elif action == "health":
            app = load_toml("app.toml")
            with httpx.Client(timeout=5) as client:
                response = client.get(app["embedding"]["base_url"] + "/api/tags")
                response.raise_for_status()
                models = [item["name"] for item in response.json()["models"]]
            service = service_for(request)
            try:
                generation = dict(service.active_generation())
            finally:
                service.close()
            if app["embedding"]["model"] not in models:
                raise ToolError("DEPENDENCY_UNAVAILABLE", "本机Ollama缺少配置的嵌入模型")
            data = {"project_root": str(PROJECT_ROOT), "embedding_model": app["embedding"]["model"], "generation": generation, "external_generation_calls": 0}
        else:
            raise ToolError("INPUT_ERROR", "action支持search、evidence、single、period、health")
        return {"ok": True, "action": action, "data": data}
    except ToolError as exc:
        return {"ok": False, "action": action, "error": {"code": exc.code, "message": str(exc)}}
    except CalculationInputError as exc:
        return {"ok": False, "action": action, "error": {"code": "INPUT_ERROR", "message": str(exc)}}
    except (OSError, httpx.HTTPError, sqlite3.Error, RuntimeError) as exc:
        return {"ok": False, "action": action, "error": {"code": "DEPENDENCY_UNAVAILABLE", "message": f"本机检索依赖不可用，请运行health核对Ollama、模型和活动库。类型{type(exc).__name__}"}}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("请求超过64KiB")
        request = json.loads(raw.decode("utf-8-sig"))
        result = execute(request)
    except (UnicodeError, ValueError) as exc:
        result = {"ok": False, "error": {"code": "INPUT_ERROR", "message": str(exc)}}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
