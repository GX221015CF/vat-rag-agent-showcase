from __future__ import annotations

import json
from typing import Any


STATUS_LABELS = {
    "ANSWERED": "已回答",
    "NEED_MORE_FACTS": "需要补充信息",
    "INSUFFICIENT_EVIDENCE": "证据不足",
}


def _as_dict(row: Any) -> dict[str, Any]:
    return dict(row) if not isinstance(row, dict) else row


def _citations(
    chunk_ids: Any,
    evidence_by_chunk: dict[str, dict[str, Any]],
) -> list[dict[str, Any]] | None:
    if not isinstance(chunk_ids, list) or any(
        not isinstance(chunk_id, str) for chunk_id in chunk_ids
    ):
        return None
    citations: list[dict[str, Any]] = []
    for chunk_id in dict.fromkeys(chunk_ids):
        evidence = evidence_by_chunk.get(chunk_id)
        if evidence is None:
            return None
        citations.append(
            {
                "chunk_id": chunk_id,
                "rank": evidence["rank"],
                "label": f"证据{evidence['rank']}",
                "document_title": evidence["document_title"],
                "anchor": evidence["anchor"],
            }
        )
    return citations


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return [item for item in value if item.strip()]


def _calculation_view(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    outputs = snapshot.get("outputs")
    if not isinstance(outputs, dict):
        return None
    calculation_type = snapshot.get("calculation_type")
    if calculation_type == "SINGLE_TRANSACTION":
        fields = (
            ("不含税金额", "net_amount"),
            ("税额", "tax_amount"),
            ("含税金额", "gross_amount"),
        )
        title = "单笔价税计算"
    elif calculation_type == "PERIOD_SUMMARY":
        fields = (
            ("本期应纳税额", "tax_payable"),
            ("期末留抵", "ending_credit"),
        )
        title = "纳税期间汇总"
    else:
        return None
    if any(not isinstance(outputs.get(key), str) for _label, key in fields):
        return None
    return {
        "title": title,
        "calculation_run_id": snapshot.get("calculation_run_id"),
        "formula_version": snapshot.get("formula_version"),
        "rounding_rule_version": snapshot.get("rounding_rule_version"),
        "items": [
            {"label": label, "value": outputs[key], "unit": "元"}
            for label, key in fields
        ],
    }


def build_answer_view(
    generation_row: Any,
    hit_rows: list[Any],
) -> dict[str, Any] | None:
    generation = _as_dict(generation_row)
    raw_json = generation.get("response_json")
    if generation.get("status") != "SUCCESS" or not isinstance(raw_json, str):
        return None
    try:
        stored = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(stored, dict):
        return None

    if stored.get("schema_version") == "answer-record-v1":
        answer = stored.get("answer")
        calculation_snapshot = stored.get("calculation_snapshot")
    else:
        answer = stored
        calculation_snapshot = None
    stored_trace = stored.get("trace", {})
    if not isinstance(answer, dict):
        return None

    status = answer.get("status")
    if status not in STATUS_LABELS:
        return None
    evidence_by_chunk = {
        row["chunk_id"]: _as_dict(row)
        for row in hit_rows
    }

    direct = answer.get("direct_conclusion")
    if not isinstance(direct, dict):
        return None
    conclusion_items = []
    if answer.get("presentation_version") == "points-v3":
        raw_items = direct.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return None
        for item in raw_items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
                return None
            citations = _citations(item.get("citation_chunk_ids"), evidence_by_chunk)
            if citations is None or (status == "ANSWERED" and not citations):
                return None
            conclusion_items.append({"text": item["text"], "citations": citations})
        direct = {
            "text": "\n".join(item["text"] for item in conclusion_items),
            "citation_chunk_ids": list(dict.fromkeys(c for item in raw_items for c in item["citation_chunk_ids"])),
        }
    if not isinstance(direct.get("text"), str):
        return None
    direct_citations = _citations(
        direct.get("citation_chunk_ids"), evidence_by_chunk
    )
    if direct_citations is None:
        return None

    prerequisites = _string_list(answer.get("prerequisites"))
    uncertainties = _string_list(answer.get("uncertainties"))
    if prerequisites is None or uncertainties is None:
        return None

    raw_rules = answer.get("rules")
    raw_exceptions = answer.get("exceptions")
    if not isinstance(raw_rules, list) or not isinstance(raw_exceptions, list):
        return None

    rules: list[dict[str, Any]] = []
    for item in raw_rules:
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str):
            return None
        citations = _citations(item.get("citation_chunk_ids"), evidence_by_chunk)
        if citations is None:
            return None
        rules.append({"claim": item["claim"], "citations": citations})

    exceptions: list[dict[str, Any]] = []
    for item in raw_exceptions:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            return None
        citations = _citations(item.get("citation_chunk_ids"), evidence_by_chunk)
        if citations is None:
            return None
        exceptions.append({"text": item["text"], "citations": citations})

    calculation_explanation = answer.get("calculation_explanation")
    follow_up_question = answer.get("follow_up_question")
    if calculation_explanation is not None and not isinstance(
        calculation_explanation, str
    ):
        return None
    if follow_up_question is not None and not isinstance(follow_up_question, str):
        return None

    concise = answer.get("presentation_version") in {"concise-v2", "points-v3"}
    essential = []
    for item in answer.get("essential_explanation", []):
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str):
            return None
        citations = _citations(item.get("citation_chunk_ids"), evidence_by_chunk)
        if citations is None:
            return None
        essential.append({"claim": item["claim"], "citations": citations})
    critical_gaps = _string_list(answer.get("critical_gaps", []))
    if critical_gaps is None:
        return None
    if not concise and status == "INSUFFICIENT_EVIDENCE":
        critical_gaps = uncertainties

    view = {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "concise": concise,
        "conclusion_items": conclusion_items,
        "conclusion_lines": [line.strip() for line in direct["text"].splitlines() if line.strip()],
        "essential_explanation": essential,
        "critical_gaps": critical_gaps,
        "direct_conclusion": {
            "text": direct["text"],
            "citations": direct_citations,
        },
        "prerequisites": prerequisites,
        "rules": rules,
        "exceptions": exceptions,
        "uncertainties": uncertainties,
        "calculation_explanation": calculation_explanation,
        "calculation": _calculation_view(calculation_snapshot),
        "follow_up_question": follow_up_question,
        "trace": {
            **stored_trace,
            "model_id": generation.get("model_id"),
            "prompt_version": generation.get("prompt_version"),
            "generation_id": generation.get("generation_id"),
            "response_id": generation.get("response_id"),
        },
    }
    visible_citations = (
        [citation for item in conclusion_items for citation in item["citations"]]
        if conclusion_items else direct_citations
    )
    ordered_ids = list(dict.fromkeys(citation["chunk_id"] for citation in visible_citations))
    display_ranks = {chunk_id: index for index, chunk_id in enumerate(ordered_ids, start=1)}
    for citation in [*visible_citations, *direct_citations]:
        citation["rank"] = display_ranks[citation["chunk_id"]]
        citation["label"] = f"证据{citation['rank']}"
    view["cited_evidence"] = [
        {**evidence_by_chunk[chunk_id], "rank": display_ranks[chunk_id]}
        for chunk_id in ordered_ids
    ]
    lines = ["直接结论"]
    if conclusion_items:
        for item in conclusion_items:
            refs = "".join(f"[证据{citation['rank']}]" for citation in item["citations"])
            lines.append(f"- {item['text']}{refs}")
    else:
        lines.extend(view["conclusion_lines"])
        lines.append("".join(f"[证据{citation['rank']}]" for citation in direct_citations))
    for heading, values in (("适用条件", prerequisites), ("本题还缺少的依据", critical_gaps)):
        if values:
            lines.extend([heading, *values])
    if view["calculation"]:
        lines.append(view["calculation"]["title"])
        lines.extend(f"{item['label']} {item['value']}{item['unit']}" for item in view["calculation"]["items"])
    if follow_up_question:
        lines.extend(["请补充一个信息", follow_up_question])
    view["copy_text"] = "\n".join(line for line in lines if line)
    return view


def build_answer_views(store: Any, matter_id: str) -> dict[int, dict[str, Any]]:
    views: dict[int, dict[str, Any]] = {}
    for generation in store.list_successful_generations(matter_id):
        view = build_answer_view(
            generation,
            store.hits_for_retrieval_run(generation["retrieval_run_id"]),
        )
        if view is not None:
            views[int(generation["round_number"])] = view
    return views
