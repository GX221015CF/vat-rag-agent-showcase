from __future__ import annotations

import sqlite3

from tax_rag.models import SearchHit
from tax_rag.retrieval.common import row_to_hit
from tax_rag.storage.fts_text import fts_query


def retrieve(connection: sqlite3.Connection, query: str, top_k: int) -> list[SearchHit]:
    rows = connection.execute(
        """
        SELECT c.*, d.title AS document_title, bm25(chunks_fts) AS rank
        FROM chunks_fts
        JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
        JOIN documents d ON d.document_id=c.document_id
        WHERE chunks_fts MATCH ?
          AND c.retrieval_enabled=1
          AND d.approval_status='APPROVED_ACTIVE'
          AND d.current_version_id=c.version_id
        ORDER BY rank ASC
        LIMIT ?
        """,
        (fts_query(query), top_k),
    ).fetchall()
    return [row_to_hit(row, -float(row["rank"]), "fts") for row in rows]

