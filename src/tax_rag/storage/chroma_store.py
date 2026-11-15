from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import chromadb
import ollama
from llama_index.core.schema import TextNode

from tax_rag.llamaindex.node_adapter import embedding_text


def generation_id(nodes: list[TextNode], model: str, rule_version: str) -> str:
    payload = {
        "chunk_ids": sorted(node.node_id for node in nodes),
        "model": model,
        "rule_version": rule_version,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return "gen_" + digest[:20]


def _embeddings(response: Any) -> list[list[float]]:
    if hasattr(response, "embeddings"):
        return response.embeddings
    if isinstance(response, dict) and "embeddings" in response:
        return response["embeddings"]
    raise RuntimeError("Ollama embedding响应中没有embeddings")


def embed_batches(
    nodes: list[TextNode], model: str, base_url: str, batch_size: int
) -> tuple[list[list[float]], int]:
    all_vectors: list[list[float]] = []
    dimension: int | None = None
    with ollama.Client(host=base_url) as client:
        for start in range(0, len(nodes), batch_size):
            batch = nodes[start : start + batch_size]
            response = client.embed(model=model, input=[embedding_text(node) for node in batch])
            vectors = _embeddings(response)
            if len(vectors) != len(batch):
                raise RuntimeError("Ollama返回向量数量与请求不一致")
            for vector in vectors:
                if not vector:
                    raise RuntimeError("Ollama返回空向量")
                if dimension is None:
                    dimension = len(vector)
                elif len(vector) != dimension:
                    raise RuntimeError("Ollama向量维度不一致")
            all_vectors.extend(vectors)
    if dimension is None:
        raise RuntimeError("没有可写入向量")
    return all_vectors, dimension


def build_candidate_collection(
    path: Path,
    collection_prefix: str,
    nodes: list[TextNode],
    model: str,
    base_url: str,
    batch_size: int,
    rule_version: str,
) -> dict[str, Any]:
    gen_id = generation_id(nodes, model, rule_version)
    collection_name = f"{collection_prefix}_{gen_id[4:]}"
    vectors, dimension = embed_batches(nodes, model, base_url, batch_size)
    path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(path))
    existing = {item.name for item in client.list_collections()}
    if collection_name in existing:
        client.delete_collection(collection_name)
    collection = client.create_collection(
        collection_name,
        metadata={"hnsw:space": "cosine", "status": "CANDIDATE", "model": model},
    )
    metadatas = []
    for node in nodes:
        metadata = dict(node.metadata)
        metadata["generation_id"] = gen_id
        metadatas.append(metadata)
    collection.add(
        ids=[node.node_id for node in nodes],
        embeddings=vectors,
        documents=[node.text for node in nodes],
        metadatas=metadatas,
    )
    count = collection.count()
    if count != len(nodes):
        raise RuntimeError(f"Chroma数量不一致：{count} != {len(nodes)}")
    return {
        "generation_id": gen_id,
        "collection_name": collection_name,
        "embedding_model": model,
        "embedding_dimension": dimension,
        "chunk_count": count,
        "status": "CANDIDATE",
        "created_at": datetime.now().astimezone().isoformat(),
    }
