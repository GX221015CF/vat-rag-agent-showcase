from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent


def load_toml(name: str) -> dict[str, Any]:
    with (PROJECT_ROOT / "config" / name).open("rb") as handle:
        return tomllib.load(handle)


def load_json(name: str) -> Any:
    return json.loads((PROJECT_ROOT / "config" / name).read_text(encoding="utf-8"))


def project_path(value: str) -> Path:
    return (PROJECT_ROOT / value).resolve()


def workspace_path(value: str) -> Path:
    return (PROJECT_ROOT / value).resolve()

