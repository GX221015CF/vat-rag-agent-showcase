from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from tax_rag.config import PROJECT_ROOT, load_json, load_toml, project_path
from tax_rag.ingest.docx_parser import load_rules, parse_document
from tax_rag.ingest.source_copy import sha256_file, write_json
from tax_rag.llamaindex.node_adapter import to_text_node
from tax_rag.storage.chroma_store import build_candidate_collection
from tax_rag.storage.fts_text import fts_document_text
from tax_rag.storage.sqlite_store import approved_chunk_rows, article_number, connect


ARTIFACTS = PROJECT_ROOT / "artifacts/skill_v1"


def add_source(connection: sqlite3.Connection, source: dict, rules: dict) -> int:
    path = PROJECT_ROOT / source["normalized_path"]
    if sha256_file(path) != source["normalized_sha256"]:
        raise ValueError(f"工作副本哈希不符 {source['source_id']}")
    result = parse_document(path, rules)
    chunks = result["chunks"]
    if not chunks or any(not chunk["text"].strip() for chunk in chunks):
        raise ValueError(f"正文分块为空 {source['source_id']}")
    document_id, version_id = chunks[0]["document_id"], chunks[0]["version_id"]
    now = datetime.now().astimezone().isoformat()
    connection.execute(
        "INSERT INTO documents VALUES(?,?,?,?,?,?)",
        (document_id, source["title"], source["legal_level"], "APPROVED_ACTIVE", version_id, now),
    )
    connection.execute(
        "INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (version_id, document_id, path.name, source["original_path"], source["normalized_sha256"], source["normalized_sha256"], source["issued_on"], "skill-normalized-legacy-v1", "VALIDATED", now),
    )
    for chunk in chunks:
        raw_anchor = chunk["anchor"]
        page = re.search(r"原件第(\d+)页", raw_anchor)
        section = re.match(r"([一二三四五六七八九十]+)、", raw_anchor)
        anchor = f"原件第{page.group(1)}页" if page else (f"第{section.group(1)}项" if section else raw_anchor)
        if raw_anchor == "一、正文":
            anchor = "正文概述"
        locator = {**chunk["source"], "official_url": source["official_url"], "original_path": source["original_path"], "original_sha256": source["original_sha256"], "normalized_path": source["normalized_path"]}
        if page:
            locator["page"] = int(page.group(1))
        connection.execute(
            "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (chunk["chunk_id"], document_id, version_id, chunk["ordinal"], anchor, chunk["anchor_role"], article_number(anchor), json.dumps([source["title"], anchor], ensure_ascii=False), chunk["text"], chunk["text_sha256"], json.dumps(locator, ensure_ascii=False)),
        )
        connection.execute(
            "INSERT INTO chunks_fts VALUES(?,?,?,?)",
            (chunk["chunk_id"], document_id, version_id, fts_document_text(source["title"], anchor, chunk["text"])),
        )
    write_json(ARTIFACTS / "parsed" / (source["source_id"] + ".json"), result)
    return len(chunks)


def build(manifest_name: str = "skill_sources.json") -> None:
    app = load_toml("app.toml")
    manifest = load_json(manifest_name)
    rules = load_rules(PROJECT_ROOT / "config/rules_v1.json")
    # Supplemental documents quote laws, so quoted article labels remain in their section body.
    rules["compiled_patterns"]["article"] = re.compile(r"(?!)")
    release = PROJECT_ROOT / "data/skill_v1/releases" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    release.mkdir(parents=True)
    knowledge = release / "knowledge.db"
    old_knowledge = project_path(app["project"]["sqlite_path"])
    with closing(sqlite3.connect(old_knowledge.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(knowledge)) as target:
            original.backup(target)
    connection = connect(knowledge)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS source_metadata(version_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)")
        added = []
        with connection:
            for source in manifest["sources"]:
                version = connection.execute("SELECT version_id FROM document_versions WHERE copy_sha256=?", (source["normalized_sha256"],)).fetchone()
                if version is None:
                    count = add_source(connection, source, rules)
                    added.append({"source_id": source["source_id"], "chunks": count})
                    version = connection.execute("SELECT version_id FROM document_versions WHERE copy_sha256=?", (source["normalized_sha256"],)).fetchone()
                connection.execute("INSERT OR REPLACE INTO source_metadata VALUES(?,?)", (version[0], json.dumps(source, ensure_ascii=False)))
        rows = approved_chunk_rows(connection)
        nodes = [to_text_node(row) for row in rows]
        candidate = build_candidate_collection(release / "chroma", "vat_skill", nodes, app["embedding"]["model"], app["embedding"]["base_url"], int(app["embedding"]["batch_size"]), "skill-sources-v1")
        with connection:
            connection.execute("UPDATE index_generations SET status='SUPERSEDED' WHERE status='ACTIVE'")
            connection.execute("INSERT INTO index_generations VALUES(?,?,?,?,?,'ACTIVE',?,?)", (candidate["generation_id"], candidate["collection_name"], candidate["embedding_model"], candidate["embedding_dimension"], candidate["chunk_count"], candidate["created_at"], candidate["created_at"]))
            connection.executemany("INSERT INTO index_members VALUES(?,?)", [(candidate["generation_id"], row["chunk_id"]) for row in rows])
            connection.execute("UPDATE runtime_state SET value=?,updated_at=? WHERE key='active_generation_id'", (candidate["generation_id"], candidate["created_at"]))
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("候选SQLite完整性失败")
    finally:
        connection.close()
    report = {"status": "STAGED_FOR_VALIDATION", "note": "候选库内部指针自洽，项目app.toml仍指向原库", "release": str(release), "knowledge_path": str(knowledge), "chroma_path": str(release / "chroma"), "old_knowledge_path": str(old_knowledge), "old_app_sha256": sha256_file(PROJECT_ROOT / "config/app.toml"), "sources_manifest": manifest_name, "sources_sha256": sha256_file(PROJECT_ROOT / "config" / manifest_name), "candidate": candidate, "added": added, "knowledge_sha256": sha256_file(knowledge)}
    write_json(ARTIFACTS / "candidate_build.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def activate(validation_path: Path) -> None:
    report = json.loads((ARTIFACTS / "candidate_build.json").read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("status") != "passed" or validation.get("generation_id") != report["candidate"]["generation_id"]:
        raise ValueError("候选未通过对应generation验收")
    if validation.get("knowledge_sha256") != sha256_file(Path(report["knowledge_path"])):
        raise ValueError("候选验收后知识库发生变化")
    if report["sources_sha256"] != sha256_file(PROJECT_ROOT / "config" / report.get("sources_manifest", "skill_sources.json")):
        raise ValueError("候选来源清单已变化")
    config = PROJECT_ROOT / "config/app.toml"
    if sha256_file(config) != report["old_app_sha256"]:
        raise ValueError("项目配置已变化，请重新核对")
    before = config.read_text(encoding="utf-8")
    backup = ARTIFACTS / "rollback/app_before_skill.toml"
    backup.parent.mkdir(exist_ok=True)
    if backup.exists():
        raise ValueError("回退配置已存在，请核对当前激活记录")
    backup.write_text(before, encoding="utf-8")
    updated = re.sub(r'(?m)^sqlite_path = ".*"$', f'sqlite_path = "{Path(report["knowledge_path"]).relative_to(PROJECT_ROOT).as_posix()}"', before)
    updated = re.sub(r'(?m)^chroma_path = ".*"$', f'chroma_path = "{Path(report["chroma_path"]).relative_to(PROJECT_ROOT).as_posix()}"', updated)
    runtime = load_toml("runtime.toml")
    snapshot = project_path(runtime["runtime"]["chroma_snapshot_root"]) / report["candidate"]["generation_id"]
    if snapshot.exists():
        raise ValueError("目标generation快照已存在，请核对后激活")
    temporary_snapshot = snapshot.with_name("." + snapshot.name + ".candidate")
    shutil.copytree(Path(report["chroma_path"]), temporary_snapshot)
    temporary_snapshot.replace(snapshot)
    temporary = config.with_suffix(".toml.skill-tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(config)
    write_json(ARTIFACTS / "activation.json", {"status": "ACTIVATED", "generation_id": report["candidate"]["generation_id"], "old_knowledge_retained": report["old_knowledge_path"], "rollback_config": str(backup), "activated_at": datetime.now().astimezone().isoformat(), "validation": str(validation_path)})
    print("ACTIVATED", report["candidate"]["generation_id"])


def main() -> None:
    global ARTIFACTS
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", type=Path)
    parser.add_argument("--manifest", default="skill_sources.json")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args()
    if args.artifacts:
        ARTIFACTS = args.artifacts.resolve()
    (ARTIFACTS / "parsed").mkdir(parents=True, exist_ok=True)
    if args.activate:
        activate(args.activate)
    else:
        build(args.manifest)


if __name__ == "__main__":
    main()
