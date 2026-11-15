from __future__ import annotations

import hashlib
import unittest

from tax_rag.services.retrieval_service import ActiveRetrievalService


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(path) -> list[tuple[str, str]]:
    return [
        (item.relative_to(path).as_posix(), sha256_file(item))
        for item in sorted(path.rglob("*"))
        if item.is_file()
    ]


class P2AReadOnlyIntegrationTests(unittest.TestCase):
    def test_real_active_p1_retrieval_is_read_only(self) -> None:
        service = ActiveRetrievalService()
        self.addCleanup(service.close)
        active_before = service.active_generation()
        runtime_snapshot = service.chroma_snapshot_root / active_before["generation_id"]
        self.assertTrue(runtime_snapshot.exists())
        before = sha256_file(service.knowledge_path)
        chroma_before = sha256_tree(service.chroma_source_path)
        runtime_chroma_before = sha256_tree(runtime_snapshot)
        generation, hits, channels = service.search(
            "一般纳税人销售货物开具发票，增值税纳税义务何时发生"
        )
        after = sha256_file(service.knowledge_path)
        chroma_after = sha256_tree(service.chroma_source_path)
        runtime_chroma_after = sha256_tree(runtime_snapshot)
        isolated_copy = service.isolated_chroma_copy(generation["generation_id"])

        self.assertEqual(before, after)
        self.assertEqual(chroma_before, chroma_after)
        self.assertEqual(runtime_chroma_before, runtime_chroma_after)
        self.assertTrue((service.chroma_snapshot_root / generation["generation_id"]).exists())
        self.assertTrue(isolated_copy.exists())
        self.assertFalse(isolated_copy.is_relative_to(service.chroma_snapshot_root))
        self.assertFalse(isolated_copy.is_relative_to(service.chroma_source_path))
        self.assertEqual(generation["generation_id"], active_before["generation_id"])
        self.assertEqual(generation["chunk_count"], active_before["chunk_count"])
        self.assertGreaterEqual(generation["chunk_count"], 342)
        self.assertGreater(len(hits), 0)
        self.assertEqual(set(channels), {"exact", "fts", "vector"})
        isolated_root = isolated_copy.parent
        service.close()
        self.assertFalse(isolated_root.exists())


if __name__ == "__main__":
    unittest.main()
