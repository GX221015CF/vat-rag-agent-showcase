from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from tax_rag.config import PROJECT_ROOT, load_json, load_toml, project_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_originals(source_root: Path) -> list[dict[str, Any]]:
    return [
        {
            "relative_path": path.relative_to(source_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(source_root.rglob("*.docx"), key=lambda p: p.as_posix())
    ]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_verified_copies() -> dict[str, Any]:
    app = load_toml("app.toml")
    manifest = load_json("approved_sources.json")
    source_root = project_path(app["project"]["source_root"])
    copy_root = project_path(app["project"]["source_copy_root"])
    artifacts = project_path(app["project"]["artifacts_root"])
    expected_count = int(app["project"]["expected_original_docx_count"])
    copy_root.mkdir(parents=True, exist_ok=True)

    before = snapshot_originals(source_root)
    if len(before) != expected_count:
        raise RuntimeError(f"原始DOCX数量异常：{len(before)}，期望{expected_count}")

    baseline_path = artifacts / "source_baseline.json"
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline["files"] != before:
            raise RuntimeError("原始资料区与P1基线不一致，拒绝继续")
    else:
        write_json(
            baseline_path,
            {"captured_at": datetime.now().astimezone().isoformat(), "files": before},
        )

    results = []
    for source in manifest["sources"]:
        if source["approval_status"] != "APPROVED_ACTIVE":
            raise RuntimeError(f"未批准资料出现在P1清单：{source['filename']}")
        original = source_root / Path(source["source_relpath"])
        if not original.is_file():
            raise FileNotFoundError(original)
        original_hash = sha256_file(original)
        if original_hash != source["expected_sha256"]:
            raise RuntimeError(f"源文件哈希不符：{source['filename']}")
        copied = copy_root / source["filename"]
        if copied.exists() and sha256_file(copied) != original_hash:
            raise RuntimeError(f"已有工作副本哈希不符：{copied}")
        if not copied.exists():
            shutil.copy2(original, copied)
        copied_hash = sha256_file(copied)
        if copied_hash != original_hash:
            raise RuntimeError(f"复制后哈希不符：{source['filename']}")
        results.append(
            {
                "filename": source["filename"],
                "source_relative_path": source["source_relpath"],
                "source_sha256": original_hash,
                "copy_sha256": copied_hash,
                "identical": True,
            }
        )

    after = snapshot_originals(source_root)
    if after != before:
        raise RuntimeError("复制前后原始资料区发生变化")
    write_json(
        artifacts / "copy_verification.json",
        {
            "verified_at": datetime.now().astimezone().isoformat(),
            "original_docx_count": len(after),
            "original_unchanged": True,
            "copies": results,
        },
    )
    write_json(
        artifacts / "source_integrity_current.json",
        {"captured_at": datetime.now().astimezone().isoformat(), "files": after},
    )
    return {"original_docx_count": len(after), "copies": results}

