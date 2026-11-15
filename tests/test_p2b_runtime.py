from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tax_rag.domain.answer_contract import AnswerValidationError
from tax_rag.domain.calculation import calculate_single_transaction
from tax_rag.domain.matter_state import MatterStateError
from tax_rag.models import SearchHit
from tax_rag.services.deepseek_service import DeepSeekError
from tax_rag.services.matter_service import MatterService
from tax_rag.storage.runtime_store import RuntimeStore


class FakeRetrieval:
    def search(self, _query):
        hit = SearchHit(
            "chunk-1",
            "doc-1",
            "version-1",
            "测试法规",
            "第一条",
            "测试证据",
            {"paragraph_start": 1, "paragraph_end": 1},
            1.0,
            "rrf",
            {"exact": 1},
        )
        return {"generation_id": "gen-test"}, [hit], {"exact": [hit]}


class BrokenLLM:
    model_id = "deepseek-flash"
    prompt_version = "vat-answer-v1"

    def generate(self, *_args, **_kwargs):
        raise DeepSeekError("TIMEOUT", "timeout")


class InvalidLLM(BrokenLLM):
    def generate(self, *_args, **_kwargs):
        raise AnswerValidationError("invented citation")


class P2BRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        schema = Path(__file__).parents[1] / "src" / "tax_rag" / "storage" / "runtime_schema.sql"
        self.store = RuntimeStore(root / "runtime.db", schema, retention_days=7)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_old_notice_cannot_start_cloud_enabled_round(self) -> None:
        matter = self.store.create_matter("cloud-transfer-v1")
        self.store.accept_notice(matter["matter_id"], "cloud-transfer-v1")
        service = MatterService(
            self.store, FakeRetrieval(), 8, 7, "cloud-transfer-v2", BrokenLLM()
        )
        with self.assertRaises(MatterStateError):
            service.submit(matter["matter_id"], "一般纳税人增值税问题")
        self.assertEqual(self.store.get_matter(matter["matter_id"])["round_count"], 0)

    def test_deepseek_timeout_preserves_evidence_and_records_failure(self) -> None:
        service = MatterService(
            self.store, FakeRetrieval(), 8, 7, "cloud-transfer-v2", BrokenLLM()
        )
        matter = service.create_matter()
        service.accept_notice(matter["matter_id"])
        result = service.submit(matter["matter_id"], "一般纳税人增值税问题")
        generation = self.store.latest_generation(matter["matter_id"])
        self.assertEqual(result.deepseek_status, "FAILED")
        self.assertEqual(len(result.hits), 1)
        self.assertIn("TIMEOUT", result.assistant_message["content"])
        self.assertEqual(generation["status"], "FAILED")
        self.assertEqual(generation["error_code"], "TIMEOUT")

    def test_invalid_model_output_is_not_shown_as_answer(self) -> None:
        service = MatterService(
            self.store, FakeRetrieval(), 8, 7, "cloud-transfer-v2", InvalidLLM()
        )
        matter = service.create_matter()
        service.accept_notice(matter["matter_id"])
        result = service.submit(matter["matter_id"], "一般纳税人增值税问题")
        self.assertEqual(result.deepseek_status, "VALIDATION_FAILED")
        self.assertIn("未通过引用与结构校验", result.assistant_message["content"])

    def test_cleanup_removes_old_calculation_and_generation_payloads(self) -> None:
        now = datetime(2026, 8, 31, 4, 0, tzinfo=UTC)
        old = now - timedelta(days=8)
        matter = self.store.create_matter("cloud-transfer-v2", now=old)
        self.store.accept_notice(matter["matter_id"], "cloud-transfer-v2", now=old)
        matter, message = self.store.begin_round(
            matter["matter_id"], "一般纳税人增值税问题", 8, now=old
        )
        retrieval_run_id = self.store.record_retrieval(
            matter["matter_id"],
            message["message_id"],
            "IN_SCOPE",
            "测试",
            "gen-test",
            [],
            now=old,
        )
        self.store.record_calculation(
            matter["matter_id"],
            calculate_single_transaction("113", "TAX_INCLUDED", "13"),
            now=old,
        )
        self.store.record_generation(
            matter["matter_id"],
            message["message_id"],
            retrieval_run_id,
            "deepseek-flash",
            "vat-answer-v1",
            "SUCCESS",
            "a" * 64,
            "response-old",
            {"raw": "old generated answer"},
            None,
            now=old,
        )

        result = self.store.cleanup_expired(now=now)
        self.assertEqual(result["calculation_runs_deleted"], 1)
        self.assertEqual(result["generation_runs_deleted"], 1)
        connection = self.store.connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM calculation_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM generation_runs").fetchone()[0], 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
