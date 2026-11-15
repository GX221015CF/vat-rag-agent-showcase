from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tax_rag.models import SearchHit
from tax_rag.storage.runtime_store import RuntimeStore


class P2ARetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        schema = Path(__file__).parents[1] / "src" / "tax_rag" / "storage" / "runtime_schema.sql"
        self.store = RuntimeStore(root / "runtime.db", schema, retention_days=7)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def hit() -> SearchHit:
        return SearchHit(
            "chunk-1",
            "doc-1",
            "version-1",
            "测试法规",
            "第一条",
            "事实与回答证据",
            {"paragraph_start": 1, "paragraph_end": 1},
            0.1,
            "rrf",
            {"exact": 1},
        )

    def add_round(self, now: datetime, content: str):
        matter = self.store.create_matter("notice-v1", now=now)
        self.store.accept_notice(matter["matter_id"], "notice-v1", now=now)
        matter, message = self.store.begin_round(matter["matter_id"], content, 8, now=now)
        self.store.record_retrieval(
            matter["matter_id"],
            message["message_id"],
            "IN_SCOPE",
            "测试",
            "gen-test",
            [self.hit()],
            now=now,
        )
        self.store.add_assistant_message(matter["matter_id"], 1, "测试回答", now=now)
        return matter

    def test_cleanup_deletes_only_data_older_than_seven_days(self) -> None:
        now = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
        old = self.add_round(now - timedelta(days=8), "过期用户原文")
        fresh = self.add_round(now - timedelta(days=6), "保留用户原文")

        result = self.store.cleanup_expired(now=now)

        self.assertEqual(result["messages_deleted"], 2)
        self.assertEqual(result["retrieval_runs_deleted"], 1)
        self.assertEqual(result["retrieval_hits_deleted"], 1)
        self.assertEqual(result["matters_expired"], 1)
        self.assertEqual(self.store.get_matter(old["matter_id"])["status"], "EXPIRED")
        self.assertEqual(self.store.get_matter(fresh["matter_id"])["status"], "ACTIVE")
        self.assertEqual(len(self.store.list_messages(old["matter_id"])), 0)
        self.assertEqual(len(self.store.list_messages(fresh["matter_id"])), 2)

    def test_audit_rows_do_not_store_user_original_text(self) -> None:
        marker = "敏感原文-身份证号-测试"
        now = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
        self.add_round(now, marker)
        connection = self.store.connect()
        try:
            rows = connection.execute("SELECT * FROM audit_events").fetchall()
        finally:
            connection.close()

        self.assertGreater(len(rows), 0)
        self.assertNotIn(marker, "\n".join(str(dict(row)) for row in rows))


if __name__ == "__main__":
    unittest.main()
