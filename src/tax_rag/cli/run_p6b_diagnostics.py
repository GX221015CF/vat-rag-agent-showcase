from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from tax_rag.config import PROJECT_ROOT
from tax_rag.retrieval.query_planner import plan_query
from tax_rag.services.retrieval_service import ActiveRetrievalService


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def hit_payload(hit: Any, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "document_title": hit.document_title,
        "anchor": hit.anchor,
        "chunk_id": hit.chunk_id,
        "channel_ranks": hit.ranks or {},
    }


def check_plan(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures = []
    for field in ("resolved_document_title", "article_number", "exact_locator"):
        if field in expected and actual.get(field) != expected[field]:
            failures.append(
                f"{field}: expected={expected[field]!r}, actual={actual.get(field)!r}"
            )
    for field in ("expansion_terms_contains", "applied_rules_contains"):
        actual_field = field.removesuffix("_contains")
        missing = [
            value for value in expected.get(field, []) if value not in actual[actual_field]
        ]
        if missing:
            failures.append(f"{actual_field}: missing={missing!r}")
    if "expansion_terms_contains" in expected and not expected["expansion_terms_contains"]:
        if actual["expansion_terms"]:
            failures.append(
                f"expansion_terms: expected empty, actual={actual['expansion_terms']!r}"
            )
    if "applied_rules_contains" in expected and not expected["applied_rules_contains"]:
        if actual["applied_rules"]:
            failures.append(
                f"applied_rules: expected empty, actual={actual['applied_rules']!r}"
            )
    return failures


def evaluate_case(
    service: ActiveRetrievalService,
    case: dict[str, Any],
) -> dict[str, Any]:
    planner_started = time.perf_counter()
    planned = plan_query(case["query"])
    planner_latency_ms = round((time.perf_counter() - planner_started) * 1000, 3)
    started = time.perf_counter()
    if case.get("planner_only", False):
        plan = planned
        hits = []
        channels = {"exact": [], "fts": [], "vector": []}
        generation_id = None
        retrieval_skipped = True
    else:
        generation, hits, channels, plan = service.search_with_trace(case["query"])
        generation_id = generation["generation_id"]
        retrieval_skipped = False
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    plan_payload = plan.as_dict()
    plan_failures = check_plan(plan_payload, case["expected_plan"])
    if plan_payload != planned.as_dict():
        plan_failures.append("service query plan differs from standalone planner")

    expected_results = []
    for expected in case.get("expected_evidence", []):
        key = (expected["document_title"], expected["anchor"])
        final_rank = next(
            (
                rank
                for rank, hit in enumerate(hits, start=1)
                if (hit.document_title, hit.anchor) == key
            ),
            None,
        )
        exact_rank = next(
            (
                rank
                for rank, hit in enumerate(channels["exact"], start=1)
                if (hit.document_title, hit.anchor) == key
            ),
            None,
        )
        expected_results.append(
            {**expected, "final_rank": final_rank, "exact_rank": exact_rank}
        )

    exact_top1_pass = True
    if case.get("expect_exact_top1", False):
        exact_top1_pass = bool(expected_results) and expected_results[0]["exact_rank"] == 1
    evidence_pass = all(item["final_rank"] is not None for item in expected_results)
    passed = not plan_failures and exact_top1_pass and evidence_pass
    return {
        **case,
        "generation_id": generation_id,
        "retrieval_skipped": retrieval_skipped,
        "latency_ms": latency_ms,
        "planner_latency_ms": planner_latency_ms,
        "query_plan": plan_payload,
        "plan_failures": plan_failures,
        "expected_evidence_results": expected_results,
        "exact_top1_pass": exact_top1_pass,
        "evidence_complete_at_5": evidence_pass,
        "passed": passed,
        "top5": [hit_payload(hit, rank) for rank, hit in enumerate(hits, start=1)],
        "channel_counts": {name: len(values) for name, values in channels.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="运行P6-B查询规划只读诊断")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "p6b" / "P6B_评测明细.json",
    )
    args = parser.parse_args()
    cases = load_jsonl(PROJECT_ROOT / "evals" / "p6b_query_cases.jsonl")
    service = ActiveRetrievalService()
    try:
        results = [evaluate_case(service, case) for case in cases]
    finally:
        service.close()

    retrieval_latencies = [
        item["latency_ms"] for item in results if not item["retrieval_skipped"]
    ]
    planner_latencies = sorted(item["planner_latency_ms"] for item in results)
    evidence = [
        expected
        for item in results
        for expected in item["expected_evidence_results"]
    ]
    metrics = {
        "case_count": len(results),
        "case_pass_count": sum(item["passed"] for item in results),
        "planner_pass_count": sum(not item["plan_failures"] for item in results),
        "retrieval_case_count": sum(not item["retrieval_skipped"] for item in results),
        "exact_top1_required_count": sum(
            item.get("expect_exact_top1", False) for item in results
        ),
        "exact_top1_pass_count": sum(
            item.get("expect_exact_top1", False) and item["exact_top1_pass"]
            for item in results
        ),
        "expected_evidence_count": len(evidence),
        "expected_evidence_hits_at_5": sum(
            item["final_rank"] is not None for item in evidence
        ),
        "retrieval_latency_ms_mean": (
            round(mean(retrieval_latencies), 3) if retrieval_latencies else None
        ),
        "planner_latency_ms_mean": round(mean(planner_latencies), 3),
        "planner_latency_ms_p95": planner_latencies[
            min(len(planner_latencies) - 1, round((len(planner_latencies) - 1) * 0.95))
        ],
        "all_passed": all(item["passed"] for item in results),
    }
    payload = {
        "schema_version": "p6b-query-planner-diagnostic-v1",
        "evaluated_at": datetime.now().astimezone().isoformat(),
        "safety": {
            "formal_chroma_opened_by_query_client": False,
            "query_chroma_mode": "process_temp_isolated_copy",
            "knowledge_db_mode": "read_only_query_only",
            "index_build_attempted": False,
            "deepseek_called": False,
        },
        "metrics": metrics,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if not metrics["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
