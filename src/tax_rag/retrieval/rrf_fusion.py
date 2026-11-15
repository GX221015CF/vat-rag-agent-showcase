from __future__ import annotations

from collections import defaultdict

from tax_rag.models import SearchHit


def fuse(
    channels: dict[str, list[SearchHit]],
    weights: dict[str, float],
    rrf_k: int,
    top_k: int,
) -> list[SearchHit]:
    scores: dict[str, float] = defaultdict(float)
    ranks: dict[str, dict[str, int]] = defaultdict(dict)
    exemplars: dict[str, SearchHit] = {}
    for channel, hits in channels.items():
        weight = weights.get(channel, 1.0)
        for rank, hit in enumerate(hits, start=1):
            exemplars.setdefault(hit.chunk_id, hit)
            ranks[hit.chunk_id][channel] = rank
            scores[hit.chunk_id] += weight / (rrf_k + rank)
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:top_k]
    return [
        SearchHit(
            chunk_id=exemplars[chunk_id].chunk_id,
            document_id=exemplars[chunk_id].document_id,
            version_id=exemplars[chunk_id].version_id,
            document_title=exemplars[chunk_id].document_title,
            anchor=exemplars[chunk_id].anchor,
            text=exemplars[chunk_id].text,
            source=exemplars[chunk_id].source,
            score=scores[chunk_id],
            channel="rrf",
            ranks=ranks[chunk_id],
        )
        for chunk_id in ordered
    ]

