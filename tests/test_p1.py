from __future__ import annotations

import json
import sqlite3
import unittest

from tax_rag.config import PROJECT_ROOT, load_json, load_toml, project_path
from tax_rag.ingest.docx_parser import load_rules, parse_document, validate_result
from tax_rag.ingest.source_copy import sha256_file, snapshot_originals
from tax_rag.retrieval.rrf_fusion import fuse
from tax_rag.models import SearchHit
from tax_rag.storage.sqlite_store import connect, integrity_summary


class P1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = load_toml("app.toml")
        cls.manifest = load_json("approved_sources.json")
        cls.expectations = load_json("expectations.json")
        cls.rules = load_rules(PROJECT_ROOT / "config" / "rules_v1.json")

    def test_original_zone_has_55_documents(self) -> None:
        source_root = project_path(self.app["project"]["source_root"])
        self.assertEqual(len(snapshot_originals(source_root)), 55)

    def test_all_five_copies_match_allowlist(self) -> None:
        copy_root = project_path(self.app["project"]["source_copy_root"])
        self.assertEqual(len(list(copy_root.glob("*.docx"))), 5)
        for source in self.manifest["sources"]:
            self.assertEqual(sha256_file(copy_root / source["filename"]), source["expected_sha256"])

    def test_parser_gate_all_five(self) -> None:
        copy_root = project_path(self.app["project"]["source_copy_root"])
        for source in self.manifest["sources"]:
            with self.subTest(source=source["filename"]):
                result = parse_document(copy_root / source["filename"], self.rules)
                failures = [
                    check
                    for check in validate_result(result, self.expectations[source["filename"]])
                    if not check["passed"]
                ]
                self.assertEqual(failures, [])

    def test_sqlite_and_active_index_contract(self) -> None:
        connection = connect(project_path(self.app["project"]["sqlite_path"]))
        try:
            summary = integrity_summary(connection)
            supplemental = connection.execute("SELECT 1 FROM sqlite_master WHERE name='source_metadata'").fetchone()
            expected_documents = len(load_json("skill_sources.json")["sources"]) if supplemental else 5
            small_sources = PROJECT_ROOT / "config/small_taxpayer_sources.json"
            if supplemental and small_sources.exists():
                expected_documents += sum(
                    connection.execute("SELECT 1 FROM document_versions WHERE copy_sha256=?", (source["normalized_sha256"],)).fetchone() is not None
                    for source in json.loads(small_sources.read_text(encoding="utf-8"))["sources"]
                )
            self.assertEqual(summary["documents"], expected_documents)
            self.assertEqual(summary["approved_documents"], expected_documents)
            self.assertEqual(summary["versions"], expected_documents)
            self.assertGreaterEqual(summary["chunks"], 342)
            self.assertEqual(summary["fts_rows"], summary["chunks"])
            self.assertEqual(summary["orphan_chunks"], 0)
            self.assertEqual(summary["active_generation_pointers"], 1)
            generation = connection.execute("SELECT g.* FROM index_generations g JOIN runtime_state r ON r.key='active_generation_id' AND r.value=g.generation_id").fetchone()
            self.assertEqual(generation["status"], "ACTIVE")
            self.assertEqual(generation["chunk_count"], summary["chunks"])
            self.assertEqual(generation["embedding_dimension"], 1024)
            members = connection.execute("SELECT COUNT(*) FROM index_members WHERE generation_id=?", (generation["generation_id"],)).fetchone()[0]
            self.assertEqual(members, generation["chunk_count"])
            pointer = connection.execute(
                "SELECT value FROM runtime_state WHERE key='active_generation_id'"
            ).fetchone()[0]
            self.assertEqual(pointer, generation["generation_id"])
        finally:
            connection.close()

    def test_legacy_notice_is_not_indexed(self) -> None:
        connection = connect(project_path(self.app["project"]["sqlite_path"]))
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM document_versions WHERE source_filename LIKE '%外商投资企业和外国企业%'"
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            connection.close()

    def test_rrf_is_deterministic(self) -> None:
        hit = SearchHit("c1", "d1", "v1", "doc", "第一条", "text", {}, 1.0, "exact")
        result = fuse({"exact": [hit], "fts": [hit]}, {"exact": 2.0, "fts": 1.0}, 60, 5)
        self.assertEqual([item.chunk_id for item in result], ["c1"])
        self.assertEqual(result[0].ranks, {"exact": 1, "fts": 1})


if __name__ == "__main__":
    unittest.main()
