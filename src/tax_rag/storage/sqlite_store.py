from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from tax_rag.ingest.docx_parser import chinese_number_to_int
from tax_rag.storage.fts_text import fts_document_text


ARTICLE = re.compile(r"^第([零〇一二三四五六七八九十百千万两]+)条")
SOURCE_DATE = re.compile(r"_(\d{8})\.docx$")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection, schema_path: Path) -> None:
    connection.executescript(schema_path.read_text(encoding="utf-8"))


def article_number(anchor: str) -> int | None:
    match = ARTICLE.match(anchor)
    return chinese_number_to_int(match.group(1)) if match else None


def replace_knowledge(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
    parsed_results: list[dict[str, Any]],
) -> dict[str, int]:
    source_by_name = {item["filename"]: item for item in manifest["sources"]}
    now = datetime.now().astimezone().isoformat()
    with connection:
        connection.execute("DELETE FROM index_members")
        connection.execute("DELETE FROM index_generations")
        connection.execute("DELETE FROM chunks_fts")
        connection.execute("DELETE FROM chunk_topics")
        connection.execute("DELETE FROM chunks")
        connection.execute("DELETE FROM document_versions")
        connection.execute("DELETE FROM documents")

        for result in parsed_results:
            profile = result["profile"]
            source = source_by_name[profile["file"]]
            chunks = result["chunks"]
            if not chunks:
                raise RuntimeError(f"没有可入库Chunk：{profile['file']}")
            document_id = chunks[0]["document_id"]
            version_id = chunks[0]["version_id"]
            date_match = SOURCE_DATE.search(profile["file"])
            source_date = date_match.group(1) if date_match else None
            connection.execute(
                "INSERT INTO documents(document_id,title,legal_level,approval_status,current_version_id,created_at) VALUES(?,?,?,?,?,?)",
                (
                    document_id,
                    profile["title"],
                    source["legal_level"],
                    source["approval_status"],
                    version_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO document_versions(version_id,document_id,source_filename,source_relative_path,source_sha256,copy_sha256,source_date,parser_rule_version,parse_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    document_id,
                    source["filename"],
                    source["source_relpath"],
                    profile["sha256"],
                    profile["sha256"],
                    source_date,
                    profile["rule_version"],
                    "VALIDATED",
                    now,
                ),
            )
            for chunk in chunks:
                enabled = 1 if source["approval_status"] == "APPROVED_ACTIVE" else 0
                connection.execute(
                    "INSERT INTO chunks(chunk_id,document_id,version_id,ordinal,anchor,anchor_role,article_number,path_json,text,text_sha256,source_locator_json,retrieval_enabled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        chunk["chunk_id"],
                        document_id,
                        version_id,
                        chunk["ordinal"],
                        chunk["anchor"],
                        chunk["anchor_role"],
                        article_number(chunk["anchor"]),
                        json.dumps(chunk["path"], ensure_ascii=False),
                        chunk["text"],
                        chunk["text_sha256"],
                        json.dumps(chunk["source"], ensure_ascii=False),
                        enabled,
                    ),
                )
                connection.execute(
                    "INSERT INTO chunks_fts(chunk_id,document_id,version_id,search_text) VALUES(?,?,?,?)",
                    (
                        chunk["chunk_id"],
                        document_id,
                        version_id,
                        fts_document_text(profile["title"], chunk["anchor"], chunk["text"]),
                    ),
                )

    return {
        "documents": connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        "versions": connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0],
        "chunks": connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "fts_rows": connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0],
    }


def approved_chunk_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT c.*, d.title AS document_title, d.approval_status
        FROM chunks c
        JOIN documents d ON d.document_id = c.document_id
        JOIN document_versions v ON v.version_id = c.version_id
        WHERE c.retrieval_enabled = 1
          AND d.approval_status = 'APPROVED_ACTIVE'
          AND d.current_version_id = c.version_id
          AND v.parse_status = 'VALIDATED'
        ORDER BY d.title, c.ordinal
        """
    ).fetchall()


def integrity_summary(connection: sqlite3.Connection) -> dict[str, int]:
    queries = {
        "documents": "SELECT COUNT(*) FROM documents",
        "approved_documents": "SELECT COUNT(*) FROM documents WHERE approval_status='APPROVED_ACTIVE'",
        "versions": "SELECT COUNT(*) FROM document_versions",
        "chunks": "SELECT COUNT(*) FROM chunks",
        "fts_rows": "SELECT COUNT(*) FROM chunks_fts",
        "orphan_chunks": "SELECT COUNT(*) FROM chunks c LEFT JOIN document_versions v ON v.version_id=c.version_id WHERE v.version_id IS NULL",
        "disabled_chunks": "SELECT COUNT(*) FROM chunks WHERE retrieval_enabled=0",
        "active_generation_pointers": "SELECT COUNT(*) FROM runtime_state WHERE key='active_generation_id' AND value IS NOT NULL",
    }
    return {name: connection.execute(sql).fetchone()[0] for name, sql in queries.items()}

