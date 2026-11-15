from __future__ import annotations

import sqlite3

from tax_rag.models import SearchHit
from tax_rag.retrieval.common import row_to_hit
from tax_rag.retrieval.query_planner import QueryPlan, plan_query


def retrieve(
    connection: sqlite3.Connection,
    query: str | QueryPlan,
    top_k: int,
) -> list[SearchHit]:
    plan = query if isinstance(query, QueryPlan) else plan_query(query)
    matched_title = plan.resolved_document_title
    article = plan.article_number
    if not plan.exact_locator:
        return []
    clauses = [
        "c.retrieval_enabled=1",
        "d.approval_status='APPROVED_ACTIVE'",
        "d.current_version_id=c.version_id",
    ]
    params: list[object] = []
    if matched_title:
        clauses.append("d.title=?")
        params.append(matched_title)
    if article is not None:
        clauses.append("c.article_number=?")
        params.append(article)
    params.append(top_k)
    rows = connection.execute(
        f"""
        SELECT c.*, d.title AS document_title
        FROM chunks c JOIN documents d ON d.document_id=c.document_id
        WHERE {' AND '.join(clauses)}
        ORDER BY d.title, c.ordinal
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [row_to_hit(row, 1.0 / (index + 1), "exact") for index, row in enumerate(rows)]
