from __future__ import annotations

import json
from datetime import datetime

from tax_rag.config import PROJECT_ROOT, load_json, load_toml, project_path
from tax_rag.ingest.docx_parser import load_rules, parse_document, validate_result
from tax_rag.ingest.source_copy import ensure_verified_copies, write_json
from tax_rag.llamaindex.node_adapter import to_text_node
from tax_rag.storage.chroma_store import build_candidate_collection
from tax_rag.storage.sqlite_store import (
    approved_chunk_rows,
    connect,
    initialize,
    integrity_summary,
    replace_knowledge,
)


def main() -> None:
    started = datetime.now().astimezone().isoformat()
    copy_result = ensure_verified_copies()
    app = load_toml("app.toml")
    manifest = load_json("approved_sources.json")
    expectations = load_json("expectations.json")
    rules = load_rules(PROJECT_ROOT / "config" / "rules_v1.json")
    copy_root = project_path(app["project"]["source_copy_root"])
    artifacts = project_path(app["project"]["artifacts_root"])
    parsed_dir = artifacts / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)

    parsed_results = []
    validation_files = {}
    for source in manifest["sources"]:
        path = copy_root / source["filename"]
        result = parse_document(path, rules)
        checks = validate_result(result, expectations[source["filename"]])
        failures = [check for check in checks if not check["passed"]]
        validation_files[source["filename"]] = {
            "passed": not failures,
            "checks_passed": len(checks) - len(failures),
            "checks_total": len(checks),
            "failures": failures,
        }
        write_json(parsed_dir / f"{path.stem}.json", result)
        if failures:
            write_json(artifacts / "parser_validation.json", validation_files)
            raise RuntimeError(f"解析门禁失败：{source['filename']}")
        parsed_results.append(result)
    write_json(artifacts / "parser_validation.json", validation_files)

    sqlite_path = project_path(app["project"]["sqlite_path"])
    connection = connect(sqlite_path)
    try:
        initialize(connection, PROJECT_ROOT / "src" / "tax_rag" / "storage" / "schema.sql")
        active_pointer = connection.execute(
            "SELECT value FROM runtime_state WHERE key='active_generation_id' AND value IS NOT NULL"
        ).fetchone()
        if active_pointer is not None:
            raise RuntimeError(
                "正式索引已激活，P1全量候选构建器拒绝覆盖；后续更新必须使用审批受控的增量流程"
            )
        counts = replace_knowledge(connection, manifest, parsed_results)
        rows = approved_chunk_rows(connection)
        nodes = [to_text_node(row) for row in rows]
        with (artifacts / "nodes.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for node in nodes:
                handle.write(
                    json.dumps(
                        {"node_id": node.node_id, "text": node.text, "metadata": node.metadata},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        embedding = app["embedding"]
        index = app["index"]
        candidate = build_candidate_collection(
            project_path(app["project"]["chroma_path"]),
            index["collection_prefix"],
            nodes,
            embedding["model"],
            embedding["base_url"],
            int(embedding["batch_size"]),
            rules["rule_version"],
        )
        with connection:
            connection.execute(
                "INSERT INTO index_generations(generation_id,collection_name,embedding_model,embedding_dimension,chunk_count,status,created_at,activated_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    candidate["generation_id"],
                    candidate["collection_name"],
                    candidate["embedding_model"],
                    candidate["embedding_dimension"],
                    candidate["chunk_count"],
                    "CANDIDATE",
                    candidate["created_at"],
                ),
            )
            connection.executemany(
                "INSERT INTO index_members(generation_id,chunk_id) VALUES(?,?)",
                [(candidate["generation_id"], node.node_id) for node in nodes],
            )
        integrity = integrity_summary(connection)
        if not (counts["chunks"] == counts["fts_rows"] == candidate["chunk_count"]):
            raise RuntimeError("SQLite、FTS5与Chroma数量不一致")
        if integrity["active_generation_pointers"] != 0:
            raise RuntimeError("P1验收前不得存在激活索引指针")
        summary = {
            "status": "PASS_CANDIDATE_NOT_ACTIVE",
            "started_at": started,
            "completed_at": datetime.now().astimezone().isoformat(),
            "original_docx_count": copy_result["original_docx_count"],
            "copy_count": len(copy_result["copies"]),
            "parser_rule_version": rules["rule_version"],
            "parser_validation": validation_files,
            "sqlite_counts": counts,
            "integrity": integrity,
            "candidate_index": candidate,
            "deepseek_called": False,
            "active_index_changed": False,
        }
        write_json(artifacts / "build_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
