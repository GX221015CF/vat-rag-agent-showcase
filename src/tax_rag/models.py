from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    document_id: str
    version_id: str
    document_title: str
    anchor: str
    text: str
    source: dict[str, Any]
    score: float
    channel: str
    ranks: dict[str, int] | None = None

