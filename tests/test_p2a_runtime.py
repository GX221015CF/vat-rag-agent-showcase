from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tax_rag.domain.matter_state import MatterStateError, MatterStatus
from tax_rag.models import SearchHit
from tax_rag.services.matter_service import MatterService
from tax_rag.storage.runtime_store import RuntimeStore


class FakeRetrievalService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str):
        self.calls.append(query)
        hit = SearchHit(
            chunk_id="chunk-1",
            document_id="doc-1",
            version_id="version-1",
            document_title="中华人民共和国增值税法",
            anchor="第十条",
            text="测试证据原文",
            source={"paragraph_start": 1, "paragraph_end": 1},
            score=0.1,
            channel="rrf",
            ranks={"exact": 1},
        )
        return {"generation_id": "gen-test"}, [hit], {"exact": [hit]}


class P2ARuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        schema = Path(__file__).parents[1] / "src" / "tax_rag" / "storage" / "runtime_schema.sql"
        self.store = RuntimeStore(root / "runtime.db", schema, retention_days=7)
        self.retrieval = FakeRetrievalService()
        self.service = MatterService(
            self.store,
            self.retrieval,
            max_rounds=8,
            warning_round=7,
            notice_version="notice-v1",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_matter_is_draft_and_requires_notice(self) -> None:
        matter = self.service.create_matter()
        self.assertEqual(matter["status"], MatterStatus.DRAFT)
        with self.assertRaises(MatterStateError):
            self.service.submit(matter["matter_id"], "一般纳税人销售货物如何计算增值税")

        accepted = self.service.accept_notice(matter["matter_id"])
        self.assertEqual(accepted["status"], MatterStatus.ACTIVE)
        self.assertIsNotNone(accepted["notice_accepted_at"])

    def test_scope_routes_only_in_scope_query_to_retrieval(self) -> None:
        matter = self.service.create_matter()
        self.service.accept_notice(matter["matter_id"])

        out_result = self.service.submit(matter["matter_id"], "出口退税怎样申报")
        unknown_result = self.service.submit(matter["matter_id"], "我有一个交易问题")
        in_result = self.service.submit(matter["matter_id"], "一般纳税人销售货物的增值税纳税义务何时发生")

        self.assertEqual(out_result.decision.status, "OUT_OF_SCOPE")
        self.assertEqual(unknown_result.decision.status, "NEED_SCOPE_CONFIRMATION")
        self.assertEqual(in_result.decision.status, "IN_SCOPE")
        self.assertEqual(self.retrieval.calls, ["一般纳税人销售货物的增值税纳税义务何时发生"])
        self.assertEqual(in_result.generation_id, "gen-test")
        self.assertEqual(len(in_result.hits), 1)

    def test_topic_router_sends_p3a_false_negatives_to_retrieval(self) -> None:
        matter = self.service.create_matter()
        self.service.accept_notice(matter["matter_id"])
        queries = [
            "厂里把自己生产的一批礼品发给员工过节，要不要按卖出去交税？",
            "刚拿到营业执照，最晚多久要去税务那边办登记？",
        ]

        results = [
            self.service.submit(matter["matter_id"], query)
            for query in queries
        ]

        self.assertEqual([result.decision.status for result in results], ["IN_SCOPE"] * 2)
        self.assertEqual(self.retrieval.calls, queries)
        self.assertEqual([result.generation_id for result in results], ["gen-test"] * 2)

    def test_round_seven_warns_round_eight_closes_and_round_nine_is_rejected(self) -> None:
        matter = self.service.create_matter()
        self.service.accept_notice(matter["matter_id"])
        results = [
            self.service.submit(matter["matter_id"], "一般纳税人增值税发票问题")
            for _ in range(8)
        ]

        self.assertFalse(results[5].warning)
        self.assertTrue(results[6].warning)
        self.assertFalse(results[6].closed)
        self.assertTrue(results[7].closed)
        self.assertEqual(results[7].matter["status"], MatterStatus.CLOSED)
        with self.assertRaises(MatterStateError):
            self.service.submit(matter["matter_id"], "一般纳税人增值税发票问题")

    def test_retrieval_failure_still_returns_local_failure_message(self) -> None:
        class BrokenRetrieval:
            def search(self, _query: str):
                raise ConnectionError("local vector service unavailable")

        service = MatterService(self.store, BrokenRetrieval(), 8, 7, "notice-v1")
        matter = service.create_matter()
        service.accept_notice(matter["matter_id"])
        result = service.submit(matter["matter_id"], "一般纳税人增值税发票问题")

        self.assertIsNone(result.generation_id)
        self.assertEqual(result.hits, [])
        self.assertIn("正式活动索引本轮暂时不可用", result.assistant_message["content"])


if __name__ == "__main__":
    unittest.main()
