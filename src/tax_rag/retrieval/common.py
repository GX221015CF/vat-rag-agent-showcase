from __future__ import annotations

import json
from typing import Any

from tax_rag.models import SearchHit


def row_to_hit(row: Any, score: float, channel: str) -> SearchHit:
    return SearchHit(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        document_title=row["document_title"],
        anchor=row["anchor"],
        text=row["text"],
        source=json.loads(row["source_locator_json"]),
        score=float(score),
        channel=channel,
    )

