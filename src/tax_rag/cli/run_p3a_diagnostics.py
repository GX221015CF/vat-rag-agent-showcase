from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from tax_rag.config import PROJECT_ROOT, load_toml
from tax_rag.domain.scope_router import route_scope
from tax_rag.retrieval.hybrid import retrieve_with_plan
from tax_rag.services.retrieval_service import ActiveRetrievalService


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def normalized_golden_cases(path: Path) -> list[dict[str, Any]]:
    normalized = []
    for case in load_jsonl(path):
        normalized.append(
            {
                **case,
                "dimension": "existing_golden",
                "expected_route_status": None,
                "answerability": "ANSWERABLE",
                "coverage_policy": "all",
                "expected_evidence": [
                    {
                        "document_title": case["expected_document_title"],
                        "anchor": case["expected_anchor"],
                        "role": "既有黄金证据",
                    }
                ],
            }
        )
    return normalized


def hit_payload(hit: Any, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "document_title": hit.document_title,
        "anchor": hit.anchor,
        "chunk_id": hit.chunk_id,
        "rrf_score": hit.score,
        "channel_ranks": hit.ranks or {},
        "text_preview": hit.text.replace("\n", " ")[:240],
    }


def expected_result(
    expected: dict[str, Any],
    hits: list[Any],
    channels: dict[str, list[Any]],
) -> dict[str, Any]:
    key = (expected["document_title"], expected["anchor"])
    final_rank = next(
        (
            rank
            for rank, hit in enumerate(hits, start=1)
            if (hit.document_title, hit.anchor) == key
        ),
        None,
    )
    channel_ranks = {
        channel: next(
            (
                rank
                for rank, hit in enumerate(channel_hits, start=1)
                if (hit.document_title, hit.anchor) == key
            ),
            None,
        )
        for channel, channel_hits in channels.items()
    }
    return {
        **expected,
        "final_rank": final_rank,
        "channel_ranks": channel_ranks,
        "found_in_any_channel": any(rank is not None for rank in channel_ranks.values()),
    }


def diagnose_case(
    case: dict[str, Any],
    hits: list[Any],
    channels: dict[str, list[Any]],
    actual_route_status: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    expected = [expected_result(item, hits, channels) for item in case["expected_evidence"]]
    expected_route = case.get("expected_route_status")
    if expected_route and actual_route_status != expected_route:
        return "ROUTE_ERROR", expected
    if case["answerability"] == "OUT_OF_SCOPE":
        return "OUT_OF_SCOPE_CONTROL_PASS", expected
    if case["answerability"] == "INSUFFICIENT_KNOWLEDGE":
        return "KNOWLEDGE_GAP", expected
    missing = [item for item in expected if item["final_rank"] is None]
    if not missing:
        return "PASS", expected
    if len(expected) > 1:
        return "MULTI_EVIDENCE_INCOMPLETE", expected
    if any(item["found_in_any_channel"] for item in missing):
        return "RANKED_BELOW_TOP5", expected
    return "NOT_RECALLED", expected


def evaluate_cases(
    label: str,
    cases: list[dict[str, Any]],
    connection: sqlite3.Connection,
    generation: sqlite3.Row,
    chroma_path: Path,
    app: dict[str, Any],
    retrieval_config: dict[str, Any],
) -> list[dict[str, Any]]:
    results = []
    for case in cases:
        decision = route_scope(case["query"])
        started = time.perf_counter()
        if case["answerability"] == "OUT_OF_SCOPE":
            hits: list[Any] = []
            channels: dict[str, list[Any]] = {"exact": [], "fts": [], "vector": []}
            query_plan = None
            retrieval_skipped = True
        else:
            hits, channels, query_plan = retrieve_with_plan(
                connection,
                chroma_path,
                generation["collection_name"],
                generation["embedding_model"],
                app["embedding"]["base_url"],
                case["query"],
                retrieval_config,
            )
            retrieval_skipped = False
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        diagnosis, evidence = diagnose_case(
            case, hits, channels, decision.status.value
        )
        results.append(
            {
                **case,
                "suite": label,
                "actual_route_status": decision.status.value,
                "route_reason": decision.reason,
                "matched_keyword": decision.matched_keyword,
                "router_version": decision.router_version,
                "matched_topic_code": decision.matched_topic_code,
                "matched_terms": list(decision.matched_terms),
                "route_pass": (
                    case.get("expected_route_status") is None
                    or decision.status.value == case["expected_route_status"]
                ),
                "retrieval_skipped": retrieval_skipped,
                "query_plan": query_plan.as_dict() if query_plan is not None else None,
                "latency_ms": latency_ms,
                "diagnosis": diagnosis,
                "expected_evidence_results": evidence,
                "expected_complete_at_5": bool(evidence)
                and all(item["final_rank"] is not None for item in evidence),
                "top5": [
                    hit_payload(hit, rank)
                    for rank, hit in enumerate(hits, start=1)
                ],
                "channel_counts": {
                    name: len(channel_hits)
                    for name, channel_hits in channels.items()
                },
            }
        )
    return results


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [item for item in results if item["answerability"] == "ANSWERABLE"]
    exact_cases = [item for item in answerable if item.get("case_type") == "exact"]
    all_evidence = [
        evidence
        for item in answerable
        for evidence in item["expected_evidence_results"]
    ]
    reciprocal_ranks = [
        1 / evidence["final_rank"] if evidence["final_rank"] else 0.0
        for evidence in all_evidence
    ]
    latencies = [
        item["latency_ms"] for item in results if not item["retrieval_skipped"]
    ]
    return {
        "case_count": len(results),
        "answerable_case_count": len(answerable),
        "answerable_complete_at_5": sum(
            item["expected_complete_at_5"] for item in answerable
        ),
        "answerable_complete_at_5_rate": (
            sum(item["expected_complete_at_5"] for item in answerable)
            / len(answerable)
            if answerable
            else None
        ),
        "expected_evidence_count": len(all_evidence),
        "expected_evidence_hits_at_5": sum(
            evidence["final_rank"] is not None for evidence in all_evidence
        ),
        "expected_evidence_recall_at_5": (
            sum(evidence["final_rank"] is not None for evidence in all_evidence)
            / len(all_evidence)
            if all_evidence
            else None
        ),
        "expected_evidence_mrr": mean(reciprocal_ranks) if reciprocal_ranks else None,
        "exact_case_count": len(exact_cases),
        "exact_top1_count": sum(
            bool(item["expected_evidence_results"])
            and item["expected_evidence_results"][0]["final_rank"] == 1
            for item in exact_cases
        ),
        "exact_top1_rate": (
            sum(
                bool(item["expected_evidence_results"])
                and item["expected_evidence_results"][0]["final_rank"] == 1
                for item in exact_cases
            )
            / len(exact_cases)
            if exact_cases
            else None
        ),
        "route_checked_case_count": sum(
            item.get("expected_route_status") is not None for item in results
        ),
        "route_pass_count": sum(
            item.get("expected_route_status") is not None and item["route_pass"]
            for item in results
        ),
        "diagnosis_counts": dict(Counter(item["diagnosis"] for item in results)),
        "latency_ms": {
            "mean": round(mean(latencies), 2) if latencies else None,
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行P3-A只读失败基线诊断")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "p3a" / "P3A_评测明细.json",
    )
    args = parser.parse_args()

    app = load_toml("app.toml")
    retrieval_config = load_toml("retrieval.toml")
    service = ActiveRetrievalService()
    connection = service.connect_readonly()
    try:
        generation = service.active_generation(connection)
        formal_chroma_path = (
            service.chroma_snapshot_root / generation["generation_id"]
        )
        if not formal_chroma_path.exists():
            raise RuntimeError("活动索引的运行时Chroma快照不存在；诊断拒绝创建或复制索引")
        chroma_path = service.isolated_chroma_copy(generation["generation_id"])
        golden_results = evaluate_cases(
            "existing_golden",
            normalized_golden_cases(PROJECT_ROOT / "evals" / "golden_cases.jsonl"),
            connection,
            generation,
            chroma_path,
            app,
            retrieval_config,
        )
        difficult_results = evaluate_cases(
            "p3a_difficult",
            load_jsonl(PROJECT_ROOT / "evals" / "p3a_difficult_cases.jsonl"),
            connection,
            generation,
            chroma_path,
            app,
            retrieval_config,
        )
        payload = {
            "schema_version": "p3a-diagnostic-v2",
            "evaluated_at": datetime.now().astimezone().isoformat(),
            "safety": {
                "knowledge_db_mode": "read_only_query_only",
                "index_activation_attempted": False,
                "index_build_attempted": False,
                "retrieval_algorithm_modified_by_diagnostic": False,
                "formal_chroma_opened_by_query_client": False,
                "query_chroma_mode": "process_temp_isolated_copy",
                "evaluated_retrieval_strategy": "LEGAL_CROSS_REFERENCE_RESCUE_V1",
                "evaluated_query_planner_version": "query-planner-v1",
            },
            "generation": {
                "generation_id": generation["generation_id"],
                "status": generation["status"],
                "collection_name": generation["collection_name"],
                "embedding_model": generation["embedding_model"],
            },
            "existing_evaluator_audit": {
                "has_expected_document_and_anchor": True,
                "has_recall_at_5": True,
                "has_final_expected_rank": True,
                "has_channel_ranks": True,
                "has_mrr": False,
                "has_route_status": False,
                "has_latency": False,
                "supports_multi_evidence_completeness": False,
                "selection_boundary": "旧入口选择CANDIDATE；本入口只选择正式ACTIVE索引",
            },
            "metrics": {
                "existing_golden": metrics(golden_results),
                "p3a_difficult": metrics(difficult_results),
            },
            "results": golden_results + difficult_results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))
    finally:
        connection.close()
        service.close()


if __name__ == "__main__":
    main()
