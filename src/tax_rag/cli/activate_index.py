from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import chromadb

from tax_rag.config import PROJECT_ROOT, load_json, load_toml, project_path
from tax_rag.ingest.source_copy import ensure_verified_copies, write_json
from tax_rag.storage.sqlite_store import connect


def main() -> None:
    app = load_toml("app.toml")
    approval = load_json("approvals/p1_acceptance.json")
    if approval["p1_technical_acceptance"] != "APPROVED":
        raise RuntimeError("P1技术验收尚未批准")
    if approval["golden_cases_acceptance"] != "APPROVED_ALL":
        raise RuntimeError("黄金用例尚未全部批准")
    if len(approval["approved_case_ids"]) != 15:
        raise RuntimeError("黄金用例批准数量异常")

    ensure_verified_copies()
    artifacts = project_path(app["project"]["artifacts_root"])
    final_summary = json.loads((artifacts / "p1_final_summary.json").read_text(encoding="utf-8"))
    evaluation = json.loads((artifacts / "retrieval_eval.json").read_text(encoding="utf-8"))
    if final_summary["status"] != "PASS_CANDIDATE_NOT_ACTIVE":
        raise RuntimeError("P1候选质量报告不是可激活状态")
    if evaluation["status"] != "PASS" or evaluation["case_count"] != 15:
        raise RuntimeError("黄金用例评测未通过")

    sqlite_path = project_path(app["project"]["sqlite_path"])
    connection = connect(sqlite_path)
    try:
        generations = connection.execute("SELECT * FROM index_generations").fetchall()
        if len(generations) != 1:
            raise RuntimeError(f"索引generation数量异常：{len(generations)}")
        generation = generations[0]
        existing_pointer = connection.execute(
            "SELECT value FROM runtime_state WHERE key='active_generation_id'"
        ).fetchone()
        if generation["status"] == "ACTIVE" and existing_pointer is not None:
            if existing_pointer["value"] != generation["generation_id"]:
                raise RuntimeError("活动指针与ACTIVE generation不一致")
            print(json.dumps({"status": "ALREADY_ACTIVE", "generation_id": generation["generation_id"]}, ensure_ascii=False, indent=2))
            return
        if generation["status"] != "CANDIDATE" or existing_pointer is not None:
            raise RuntimeError("候选状态或活动指针不符合首次激活条件")

        member_count = connection.execute(
            "SELECT COUNT(*) FROM index_members WHERE generation_id=?",
            (generation["generation_id"],),
        ).fetchone()[0]
        chroma_count = (
            chromadb.PersistentClient(path=str(project_path(app["project"]["chroma_path"])))
            .get_collection(generation["collection_name"])
            .count()
        )
        if member_count != generation["chunk_count"] or chroma_count != generation["chunk_count"]:
            raise RuntimeError("激活前SQLite成员与Chroma数量不一致")

        activated_at = datetime.now().astimezone().isoformat()
        backup_dir = artifacts / "activation_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"knowledge_before_activation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        connection.commit()
        backup_connection = sqlite3.connect(backup_path)
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE index_generations SET status='ACTIVE', activated_at=? WHERE generation_id=? AND status='CANDIDATE'",
            (activated_at, generation["generation_id"]),
        )
        if connection.total_changes != 1:
            connection.rollback()
            raise RuntimeError("候选generation原子更新失败")
        connection.execute(
            "INSERT INTO runtime_state(key,value,updated_at) VALUES('active_generation_id',?,?)",
            (generation["generation_id"], activated_at),
        )
        connection.commit()

        verified = connection.execute(
            """
            SELECT g.generation_id,g.collection_name,g.status,g.chunk_count,g.embedding_dimension,
                   r.value AS active_pointer,g.activated_at
            FROM index_generations g
            JOIN runtime_state r ON r.key='active_generation_id' AND r.value=g.generation_id
            WHERE g.status='ACTIVE'
            """
        ).fetchone()
        if verified is None:
            raise RuntimeError("激活后验证失败")
        record = {
            "status": "ACTIVATED",
            "activated_at": activated_at,
            "generation_id": verified["generation_id"],
            "collection_name": verified["collection_name"],
            "chunk_count": verified["chunk_count"],
            "embedding_dimension": verified["embedding_dimension"],
            "active_pointer": verified["active_pointer"],
            "p1_technical_acceptance": approval["p1_technical_acceptance"],
            "golden_cases_acceptance": approval["golden_cases_acceptance"],
            "approved_case_count": len(approval["approved_case_ids"]),
            "user_approval_words": approval["user_approval_words"],
            "backup_path": str(backup_path),
            "deepseek_called": False,
            "p2_started": False,
        }
        write_json(artifacts / "P1_验收与索引激活记录.json", record)
        print(json.dumps(record, ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
