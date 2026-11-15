from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import chromadb
import ollama

from tax_rag.models import SearchHit


def _embedding(response: object) -> list[float]:
    vectors = response.embeddings if hasattr(response, "embeddings") else response["embeddings"]
    if not vectors or not vectors[0]:
        raise RuntimeError("查询向量为空")
    return vectors[0]


def retrieve(
    connection: sqlite3.Connection,
    chroma_path: Path,
    collection_name: str,
    model: str,
    base_url: str,
    query: str,
    top_k: int,
) -> list[SearchHit]:
    with ollama.Client(host=base_url) as client:
        vector = _embedding(client.embed(model=model, input=[query]))
    with chromadb.PersistentClient(path=str(chroma_path)) as chroma_client:
        collection = chroma_client.get_collection(collection_name)
        result = collection.query(
            query_embeddings=[vector], n_results=top_k, include=["distances"]
        )
    ids = result["ids"][0]
    distances = result["distances"][0]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT c.*, d.title AS document_title
        FROM chunks c JOIN documents d ON d.document_id=c.document_id
        WHERE c.chunk_id IN ({placeholders})
          AND c.retrieval_enabled=1
          AND d.approval_status='APPROVED_ACTIVE'
          AND d.current_version_id=c.version_id
        """,
        ids,
    ).fetchall()
    by_id = {row["chunk_id"]: row for row in rows}
    hits = []
    for chunk_id, distance in zip(ids, distances):
        row = by_id.get(chunk_id)
        if row is None:
            continue
        hits.append(
            SearchHit(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                version_id=row["version_id"],
                document_title=row["document_title"],
                anchor=row["anchor"],
                text=row["text"],
                source=json.loads(row["source_locator_json"]),
                score=1.0 - float(distance),
                channel="vector",
            )
        )
    return hits
