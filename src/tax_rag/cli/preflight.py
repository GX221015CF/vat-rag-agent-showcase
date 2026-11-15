from __future__ import annotations

import json
import sqlite3

from tax_rag.config import load_json
from tax_rag.ingest.source_copy import ensure_verified_copies


def main() -> None:
    manifest = load_json("approved_sources.json")
    if len(manifest["sources"]) != 5:
        raise RuntimeError("P1正式资料必须恰好为5份")
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(text)")
    result = ensure_verified_copies()
    print(json.dumps({"status": "PASS", **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

