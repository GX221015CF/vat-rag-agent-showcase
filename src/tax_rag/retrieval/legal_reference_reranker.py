from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from tax_rag.ingest.docx_parser import chinese_number_to_int
from tax_rag.models import SearchHit


REFERENCE_PATTERN = re.compile(
    r"(?P<prefix>本法|本条例|中华人民共和国[^，。；：\s]{1,24}法|"
    r"中华人民共和国[^，。；：\s]{1,24}条例|增值税法|税收征收管理法)"
    r"\s*第(?P<number>[零〇一二三四五六七八九十百千万两\d]+)条"
)
ANCHOR_PATTERN = re.compile(r"第([零〇一二三四五六七八九十百千万两\d]+)条")


def _number(value: str) -> int:
    return int(value) if value.isdigit() else chinese_number_to_int(value)


def _anchor_number(anchor: str) -> int | None:
    match = ANCHOR_PATTERN.fullmatch(anchor)
    return _number(match.group(1)) if match else None


def _same_document_title(prefix: str, referrer_title: str) -> str | None:
    if prefix in {"本法", "本条例"}:
        return referrer_title
    compact_prefix = prefix.removeprefix("中华人民共和国")
    compact_title = referrer_title.removeprefix("中华人民共和国")
    return referrer_title if compact_prefix == compact_title else None


def _annotate_ranks(
    hits: list[SearchHit],
    pool_ranks: dict[str, int],
    rescued_ids: set[str],
) -> list[SearchHit]:
    annotated = []
    for final_rank, hit in enumerate(hits, start=1):
        ranks = dict(hit.ranks or {})
        ranks["rrf_pool"] = pool_ranks[hit.chunk_id]
        if hit.chunk_id in rescued_ids:
            ranks["reference_rescue"] = 1
        ranks["final"] = final_rank
        annotated.append(replace(hit, ranks=ranks))
    return annotated


def rerank_legal_references(
    pool: list[SearchHit],
    final_top_k: int,
    referrer_top_n: int = 1,
) -> tuple[list[SearchHit], list[dict[str, Any]]]:
    """Rescue one explicitly referenced same-document article from the RRF pool."""
    if final_top_k <= 0 or not pool:
        return [], []
    baseline = list(pool[:final_top_k])
    baseline_ids = {hit.chunk_id for hit in baseline}
    pool_ranks = {hit.chunk_id: rank for rank, hit in enumerate(pool, start=1)}
    referrers = pool[: max(0, min(referrer_top_n, len(pool)))]

    for referrer in referrers:
        for match in REFERENCE_PATTERN.finditer(referrer.text):
            target_title = _same_document_title(
                match.group("prefix"), referrer.document_title
            )
            if target_title is None:
                continue
            target_number = _number(match.group("number"))
            target = next(
                (
                    hit
                    for hit in pool
                    if hit.document_title == target_title
                    and _anchor_number(hit.anchor) == target_number
                ),
                None,
            )
            if target is None or target.chunk_id in baseline_ids:
                continue

            insertion_index = baseline.index(referrer) + 1
            reranked = [
                *baseline[:insertion_index],
                target,
                *baseline[insertion_index:],
            ][:final_top_k]
            trace = {
                "strategy": "LEGAL_CROSS_REFERENCE_RESCUE_V1",
                "referrer_chunk_id": referrer.chunk_id,
                "referrer_document_title": referrer.document_title,
                "referrer_anchor": referrer.anchor,
                "reference_text": match.group(0),
                "target_chunk_id": target.chunk_id,
                "target_document_title": target.document_title,
                "target_anchor": target.anchor,
                "target_pool_rank": pool_ranks[target.chunk_id],
                "target_final_rank": insertion_index + 1,
            }
            return (
                _annotate_ranks(reranked, pool_ranks, {target.chunk_id}),
                [trace],
            )

    return _annotate_ranks(baseline, pool_ranks, set()), []
