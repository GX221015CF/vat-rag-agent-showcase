from __future__ import annotations

import json

from tax_rag.config import PROJECT_ROOT, load_toml, project_path
from tax_rag.storage.runtime_store import RuntimeStore


def main() -> None:
    runtime = load_toml("runtime.toml")
    store = RuntimeStore(
        project_path(runtime["runtime"]["database_path"]),
        PROJECT_ROOT / "src" / "tax_rag" / "storage" / "runtime_schema.sql",
        int(runtime["retention"]["raw_data_days"]),
    )
    print(json.dumps(store.cleanup_expired(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

