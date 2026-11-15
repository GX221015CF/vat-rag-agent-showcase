from __future__ import annotations

import sqlite3
from pathlib import Path

from tax_rag.models import SearchHit
from tax_rag.retrieval import exact_retriever, fts_retriever, vector_retriever
from tax_rag.retrieval.legal_reference_reranker import rerank_legal_references
from tax_rag.retrieval.query_planner import QueryPlan, plan_query
from tax_rag.retrieval.rrf_fusion import fuse


LEGAL_CROSS_REFERENCE_RESCUE_V1 = "LEGAL_CROSS_REFERENCE_RESCUE_V1"


def validate_reranker_config(config: dict) -> str | None:
    reranker = config.get("reranker", {})
    if not bool(reranker.get("enabled", False)):
        return None
    strategy = str(reranker.get("strategy", "")).strip()
    if strategy != LEGAL_CROSS_REFERENCE_RESCUE_V1:
        raise ValueError(f"不支持的重排策略：{strategy or '<empty>'}")
    return strategy


def retrieve(
    connection: sqlite3.Connection,
    chroma_path: Path,
    collection_name: str,
    embedding_model: str,
    ollama_base_url: str,
    query: str,
    config: dict,
) -> tuple[list[SearchHit], dict[str, list[SearchHit]]]:
    hits, channels, _plan = retrieve_with_plan(
        connection,
        chroma_path,
        collection_name,
        embedding_model,
        ollama_base_url,
        query,
        config,
    )
    return hits, channels


def retrieve_with_plan(
    connection: sqlite3.Connection,
    chroma_path: Path,
    collection_name: str,
    embedding_model: str,
    ollama_base_url: str,
    query: str,
    config: dict,
) -> tuple[list[SearchHit], dict[str, list[SearchHit]], QueryPlan]:
    retrieval = config["retrieval"]
    reranker_strategy = validate_reranker_config(config)
    query_plan = plan_query(query)
    channels = {
        "exact": exact_retriever.retrieve(
            connection, query_plan, int(retrieval["exact_top_k"])
        ),
        "fts": fts_retriever.retrieve(
            connection, query_plan.fts_query_text, int(retrieval["fts_top_k"])
        ),
        "vector": vector_retriever.retrieve(
            connection,
            chroma_path,
            collection_name,
            embedding_model,
            ollama_base_url,
            query,
            int(retrieval["vector_top_k"]),
        ),
    }
    rrf = config["rrf"]
    weights = {name: float(rrf[f"{name}_weight"]) for name in channels}
    final_top_k = int(retrieval["final_top_k"])
    candidate_pool_top_k = max(
        final_top_k, int(retrieval.get("candidate_pool_top_k", final_top_k))
    )
    pool = fuse(channels, weights, int(rrf["k"]), candidate_pool_top_k)
    reranker = config.get("reranker", {})
    if reranker_strategy == LEGAL_CROSS_REFERENCE_RESCUE_V1:
        hits, _trace = rerank_legal_references(
            pool,
            final_top_k,
            int(reranker.get("referrer_top_n", 1)),
        )
        return hits, channels, query_plan
    return pool[:final_top_k], channels, query_plan
