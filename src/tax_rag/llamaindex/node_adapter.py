from __future__ import annotations

import json
from typing import Any

from llama_index.core.schema import TextNode


def to_text_node(row: Any) -> TextNode:
    source = json.loads(row["source_locator_json"])
    path = json.loads(row["path_json"])
    metadata = {
        "document_id": row["document_id"],
        "version_id": row["version_id"],
        "document_title": row["document_title"],
        "anchor": row["anchor"],
        "article_number": row["article_number"] if row["article_number"] is not None else -1,
        "path": " > ".join(path),
        "paragraph_start": source.get("paragraph_start", -1),
        "paragraph_end": source.get("paragraph_end", -1),
        "approval_status": row["approval_status"],
    }
    return TextNode(
        id_=row["chunk_id"],
        text=row["text"],
        metadata=metadata,
        excluded_llm_metadata_keys=["document_id", "version_id", "approval_status"],
    )


def embedding_text(node: TextNode) -> str:
    return f"{node.metadata['document_title']}\n{node.metadata['anchor']}\n{node.text}"

