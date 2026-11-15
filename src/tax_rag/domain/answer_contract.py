from __future__ import annotations

from typing import Any


ALLOWED_ANSWER_STATUSES = {
    "ANSWERED",
    "NEED_MORE_FACTS",
    "INSUFFICIENT_EVIDENCE",
}


class AnswerValidationError(ValueError):
    pass


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AnswerValidationError(f"{key}必须是字符串数组")
    return [item.strip() for item in value if item.strip()]


def _citations(value: Any, allowed_chunk_ids: set[str], field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AnswerValidationError(f"{field}必须是chunk_id数组")
    citations = list(dict.fromkeys(value))
    invalid = [item for item in citations if item not in allowed_chunk_ids]
    if invalid:
        raise AnswerValidationError(f"{field}包含不存在或未检索到的chunk_id")
    return citations


def validate_answer_payload(
    payload: Any,
    allowed_chunk_ids: set[str],
    calculation: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AnswerValidationError("模型输出必须是JSON对象")
    status = payload.get("status")
    if status not in ALLOWED_ANSWER_STATUSES:
        raise AnswerValidationError("回答状态无效")

    conclusion = payload.get("direct_conclusion")
    conclusion_items = []
    if payload.get("presentation_version") == "points-v3":
        raw_items = conclusion.get("items") if isinstance(conclusion, dict) else None
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 6:
            raise AnswerValidationError("direct_conclusion.items需要一至六个要点")
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise AnswerValidationError(f"结论要点[{index}]结构无效")
            text = item["text"].strip()
            if not text or "\n" in text or "\r" in text:
                raise AnswerValidationError("每个结论要点必须是单独一项非空文本，不能包含换行")
            citations = _citations(item.get("citation_chunk_ids"), allowed_chunk_ids, f"结论要点[{index}]")
            if status == "ANSWERED" and not citations:
                raise AnswerValidationError("已回答的每个结论要点必须单独绑定引用")
            conclusion_items.append({"text": text, "citation_chunk_ids": citations})
        conclusion = {
            "text": "\n".join(item["text"] for item in conclusion_items),
            "citation_chunk_ids": list(dict.fromkeys(c for item in conclusion_items for c in item["citation_chunk_ids"])),
        }
    if not isinstance(conclusion, dict) or not isinstance(conclusion.get("text"), str):
        raise AnswerValidationError("direct_conclusion结构无效")
    conclusion_text = conclusion["text"].strip()
    if not conclusion_text:
        raise AnswerValidationError("direct_conclusion不能为空")
    conclusion_citations = _citations(
        conclusion.get("citation_chunk_ids"),
        allowed_chunk_ids,
        "direct_conclusion.citation_chunk_ids",
    )

    raw_rules = payload.get("rules", [])
    if not isinstance(raw_rules, list):
        raise AnswerValidationError("rules必须是数组")
    rules = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str):
            raise AnswerValidationError(f"rules[{index}]结构无效")
        claim = item["claim"].strip()
        if not claim:
            raise AnswerValidationError(f"rules[{index}].claim不能为空")
        citations = _citations(
            item.get("citation_chunk_ids"),
            allowed_chunk_ids,
            f"rules[{index}].citation_chunk_ids",
        )
        if not citations:
            raise AnswerValidationError(f"rules[{index}]缺少引用")
        rules.append({"claim": claim, "citation_chunk_ids": citations})

    if status == "ANSWERED" and not conclusion_citations:
        raise AnswerValidationError("ANSWERED必须包含结论引用")

    raw_exceptions = payload.get("exceptions", [])
    if not isinstance(raw_exceptions, list):
        raise AnswerValidationError("exceptions必须是数组")
    exceptions = []
    for index, item in enumerate(raw_exceptions):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise AnswerValidationError(f"exceptions[{index}]结构无效")
        exception_text = item["text"].strip()
        if not exception_text:
            raise AnswerValidationError(f"exceptions[{index}].text不能为空")
        citations = _citations(
            item.get("citation_chunk_ids"),
            allowed_chunk_ids,
            f"exceptions[{index}].citation_chunk_ids",
        )
        if not citations:
            raise AnswerValidationError(f"exceptions[{index}]缺少引用")
        exceptions.append({"text": exception_text, "citation_chunk_ids": citations})

    follow_up = payload.get("follow_up_question")
    if follow_up is not None and not isinstance(follow_up, str):
        raise AnswerValidationError("follow_up_question必须是字符串或null")
    follow_up = follow_up.strip() if isinstance(follow_up, str) else None
    if status == "NEED_MORE_FACTS" and not follow_up:
        raise AnswerValidationError("NEED_MORE_FACTS必须给出一个追问")

    calculation_explanation = payload.get("calculation_explanation")
    if calculation_explanation is not None and not isinstance(calculation_explanation, str):
        raise AnswerValidationError("calculation_explanation必须是字符串或null")
    if calculation is None and calculation_explanation:
        raise AnswerValidationError("没有本地计算结果时模型不得生成计算解释")
    if calculation is not None:
        if not calculation_explanation:
            raise AnswerValidationError("存在本地计算结果时模型必须解释正式输出")
        normalized_explanation = calculation_explanation.replace(",", "").replace(" ", "")
        missing_values = [
            value
            for key, value in calculation["outputs"].items()
            if key != "currency" and value not in normalized_explanation
        ]
        if missing_values:
            raise AnswerValidationError("计算解释中的金额与本地Decimal输出不一致或不完整")

    presentation = payload.get("presentation_version")
    essential = []
    gaps = []
    if presentation is not None:
        if presentation not in {"concise-v2", "points-v3"}:
            raise AnswerValidationError("presentation_version无效")
        raw_essential = payload.get("essential_explanation", [])
        if not isinstance(raw_essential, list) or len(raw_essential) > 2:
            raise AnswerValidationError("essential_explanation需要最多两项")
        for item in raw_essential:
            if not isinstance(item, dict) or not isinstance(item.get("claim"), str) or not item["claim"].strip():
                raise AnswerValidationError("必要解释结构无效")
            citations = _citations(item.get("citation_chunk_ids"), allowed_chunk_ids, "essential_explanation")
            if not citations:
                raise AnswerValidationError("必要解释缺少引用")
            essential.append({"claim": item["claim"].strip(), "citation_chunk_ids": citations})
        gaps = _string_list(payload, "critical_gaps")
        if status == "INSUFFICIENT_EVIDENCE" and not gaps:
            raise AnswerValidationError("证据不足时需给出具体critical_gaps")

    result = {
        "status": status,
        "direct_conclusion": {
            "text": conclusion_text,
            "citation_chunk_ids": conclusion_citations,
        },
        "prerequisites": _string_list(payload, "prerequisites"),
        "rules": rules,
        "exceptions": exceptions,
        "uncertainties": _string_list(payload, "uncertainties"),
        "calculation_explanation": (
            calculation_explanation.strip() if calculation_explanation else None
        ),
        "follow_up_question": follow_up,
    }
    if conclusion_items:
        result["direct_conclusion"]["items"] = conclusion_items
    if presentation is not None:
        result.update(presentation_version=presentation, essential_explanation=essential, critical_gaps=gaps)
    return result


def render_answer(payload: dict[str, Any], hits: list[Any]) -> str:
    rank_by_chunk = {hit.chunk_id: index for index, hit in enumerate(hits, start=1)}

    def refs(chunk_ids: list[str]) -> str:
        labels = [f"[证据{rank_by_chunk[item]}]" for item in chunk_ids if item in rank_by_chunk]
        return "".join(labels)

    direct = payload["direct_conclusion"]
    sections = ["直接结论"]
    if direct.get("items"):
        sections.extend(f"- {item['text']}{refs(item['citation_chunk_ids'])}" for item in direct["items"])
    else:
        sections.append(f"{direct['text']}{refs(direct['citation_chunk_ids'])}")
    if payload["prerequisites"]:
        sections.extend(["", "适用前提或待确认事实"])
        sections.extend(f"- {item}" for item in payload["prerequisites"])
    if payload["calculation_explanation"]:
        sections.extend(["", "确定性计算说明", payload["calculation_explanation"]])
    if payload["follow_up_question"]:
        sections.extend(["", "需要你确认", payload["follow_up_question"]])
    if payload.get("critical_gaps"):
        sections.extend(["", "本题缺口", *payload["critical_gaps"]])
    return "\n".join(sections)
