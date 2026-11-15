from __future__ import annotations

import json
from datetime import datetime

from tax_rag.config import PROJECT_ROOT, load_toml, project_path
from tax_rag.ingest.source_copy import write_json
from tax_rag.retrieval.hybrid import retrieve
from tax_rag.storage.sqlite_store import connect


def load_cases() -> list[dict]:
    path = PROJECT_ROOT / "evals" / "golden_cases.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    app = load_toml("app.toml")
    retrieval_config = load_toml("retrieval.toml")
    connection = connect(project_path(app["project"]["sqlite_path"]))
    try:
        generation = connection.execute(
            "SELECT * FROM index_generations WHERE status='CANDIDATE' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if generation is None:
            raise RuntimeError("没有可评测的候选索引")
        cases = load_cases()
        results = []
        hit_count = 0
        exact_total = 0
        exact_hits = 0
        for case in cases:
            hits, channels = retrieve(
                connection,
                project_path(app["project"]["chroma_path"]),
                generation["collection_name"],
                generation["embedding_model"],
                app["embedding"]["base_url"],
                case["query"],
                retrieval_config,
            )
            expected = (case["expected_document_title"], case["expected_anchor"])
            rank = next(
                (
                    index
                    for index, hit in enumerate(hits, start=1)
                    if (hit.document_title, hit.anchor) == expected
                ),
                None,
            )
            passed = rank is not None
            hit_count += int(passed)
            if case["case_type"] == "exact":
                exact_total += 1
                exact_hits += int(rank == 1)
            results.append(
                {
                    **case,
                    "passed": passed,
                    "expected_rank": rank,
                    "top5": [
                        {
                            "document_title": hit.document_title,
                            "anchor": hit.anchor,
                            "chunk_id": hit.chunk_id,
                            "rrf_score": hit.score,
                            "channel_ranks": hit.ranks,
                        }
                        for hit in hits
                    ],
                    "channel_counts": {name: len(value) for name, value in channels.items()},
                }
            )
        recall = hit_count / len(cases) if cases else 0.0
        exact_top1 = exact_hits / exact_total if exact_total else 0.0
        status = "PASS" if recall >= 0.90 and exact_top1 == 1.0 else "FAIL"
        payload = {
            "status": status,
            "evaluated_at": datetime.now().astimezone().isoformat(),
            "generation_id": generation["generation_id"],
            "case_count": len(cases),
            "hits_at_5": hit_count,
            "recall_at_5": recall,
            "exact_case_count": exact_total,
            "exact_top1_hits": exact_hits,
            "exact_top1_accuracy": exact_top1,
            "results": results,
        }
        artifacts = project_path(app["project"]["artifacts_root"])
        write_json(artifacts / "retrieval_eval.json", payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if status != "PASS":
            raise SystemExit(2)
    finally:
        connection.close()


if __name__ == "__main__":
    main()

