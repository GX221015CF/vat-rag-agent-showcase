from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_p2a_routes import FakeLLMService, FakeRetrievalService
from tax_rag.domain.answer_contract import AnswerValidationError, validate_answer_payload
from tax_rag.web.app import create_app


class WebSimplificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.llm = FakeLLMService()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-web-simplification-with-a-long-secret",
            "RUNTIME_DATABASE_PATH": Path(self.temp.name) / "runtime.db",
            "RETRIEVAL_SERVICE": FakeRetrievalService(),
            "LLM_SERVICE": self.llm,
            "CLEANUP_ON_STARTUP": False,
            "CLEANUP_BEFORE_REQUEST": False,
        })
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **fields):
        response = self.client.post("/matters", data={"csrf_token": self.csrf, **fields})
        return response.location.split("/")[2]

    def ask(self, matter_id, question):
        return self.client.post(f"/matters/{matter_id}/messages", data={
            "csrf_token": self.csrf, "content": question,
            "submission_token": secrets.token_urlsafe(24),
        }, follow_redirects=True)

    def test_compact_composers_keep_notice_confirmation_on_normal_submission(self):
        matter_id = self.create(content="保留草稿问题")
        for url in ("/", f"/matters/{matter_id}"):
            page = self.client.get(url).get_data(as_text=True)
            self.assertNotIn("Enter 发送", page)
            self.assertNotIn("data-open-consent", page)
            self.assertIn("data-question-input", page)
            self.assertIn("data-cloud-dialog", page)
        response = self.ask(matter_id, "视同销售有哪些？")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.llm.calls, [])
        confirmed = self.client.post("/cloud-notice", data={"csrf_token": self.csrf, "accepted": "yes"}, headers={"Accept": "application/json"})
        self.assertTrue(confirmed.get_json()["ok"])
        self.assertEqual(self.llm.calls, [])

    def test_history_is_collapsed_and_uses_first_question_with_draft_fallback(self):
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="recent-consultations" data-accordion="340">', home)
        self.assertIn("0条", home)
        draft = self.create(content="尚未提交的草稿")
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('title="尚未提交的草稿"', home)
        self.assertIn(f'/matters/{draft}/notice', home)
        self.assertIn("data-resume-notice", home)
        matter_id = self.create(accepted="yes")
        first = "公司把自产产品发给员工，作为集体福利" + "测试长问题" * 40
        self.ask(matter_id, first)
        self.ask(matter_id, "那外购的呢？")
        with self.client.session_transaction() as session:
            session.pop("draft_question", None)
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn(f'title="{first}"', home)
        self.assertIn('class="matter-title"', home)
        self.assertIn("未开始的咨询", home)
        self.assertIn("2条", home)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.client.get(f"/matters/{matter_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/matters/{draft}/notice", follow_redirects=True).status_code, 200)

    def test_empty_page_and_history_answer_omit_redundant_sections(self):
        matter_id = self.create(accepted="yes")
        page = self.client.get(f"/matters/{matter_id}").get_data(as_text=True)
        self.assertIn('placeholder="请输入你的问题"', page)
        self.assertNotIn("chat-welcome", page)
        self.assertNotIn('class="messages"', page)
        page = self.ask(matter_id, "视同销售的行为有哪几种？").get_data(as_text=True)
        self.assertIn('placeholder="继续提问"', page)
        self.assertIn("这是测试结论", page)
        self.assertIn("本轮引用原文", page)
        self.assertIn("销售货物的测试证据原文", page)
        for redundant in ("更多说明与依据", "法条说明", "范围与使用说明", "answer-essential", "这是测试规则"):
            self.assertNotIn(redundant, page)
        stored = self.app.extensions["runtime_store"].latest_generation(matter_id)
        self.assertEqual(json.loads(stored["response_json"])["answer"]["rules"][0]["claim"], "这是测试规则")

    def test_points_with_no_rules_render_bound_citations_and_follow_up(self):
        original = self.llm.generate

        def generate(*args, **kwargs):
            result = original(*args, **kwargs)
            answer = result.answer
            answer.update(presentation_version="points-v3", rules=[], essential_explanation=[], critical_gaps=[])
            answer["direct_conclusion"] = {"items": [{"text": "需要计算销项税额。适用第五条第一项。", "citation_chunk_ids": ["chunk-1"]}]}
            answer["status"] = "NEED_MORE_FACTS"
            answer["follow_up_question"] = "货物是自产还是外购？"
            answer = validate_answer_payload(answer, {"chunk-1"}, None)
            return replace(result, answer=answer)

        self.llm.generate = generate
        matter_id = self.create(accepted="yes")
        page = self.ask(matter_id, "公司发给员工一批货物要缴增值税吗？").get_data(as_text=True)
        self.assertIn("需要计算销项税额。适用第五条第一项。", page)
        self.assertIn("货物是自产还是外购？", page)
        self.assertIn('id="cite-1-point-1-1"', page)
        self.assertIn('href="#evidence-1-chunk-1"', page)
        self.assertNotIn("法条说明", page)

    def test_status_requirements_still_reject_missing_question_or_gap(self):
        answer = self.llm.generate("test", self.app.extensions["retrieval_service"].search("test")[1]).answer
        answer.update(presentation_version="points-v3", essential_explanation=[], rules=[], critical_gaps=[])
        answer["direct_conclusion"] = {"items": [{"text": "请补充事实", "citation_chunk_ids": []}]}
        for status in ("NEED_MORE_FACTS", "INSUFFICIENT_EVIDENCE", "ANSWERED"):
            answer["status"] = status
            with self.subTest(status=status), self.assertRaises(AnswerValidationError):
                validate_answer_payload(answer, {"chunk-1"}, None)

    def test_historical_concise_and_points_explanations_remain_stored_without_repetition(self):
        original = self.llm.generate
        for presentation in ("concise-v2", "points-v3"):
            def generate(*args, **kwargs):
                result = original(*args, **kwargs)
                answer = result.answer
                answer.update(presentation_version=presentation, critical_gaps=[])
                answer["essential_explanation"] = [{"claim": "历史重复解释", "citation_chunk_ids": ["chunk-1"]}]
                if presentation == "points-v3":
                    answer["direct_conclusion"] = {"items": [{"text": "历史逐项结论", "citation_chunk_ids": ["chunk-1"]}]}
                return replace(result, answer=validate_answer_payload(answer, {"chunk-1"}, None))

            self.llm.generate = generate
            matter_id = self.create(accepted="yes")
            page = self.ask(matter_id, "视同销售有哪些？").get_data(as_text=True)
            self.assertIn("复制回答", page)
            self.assertNotIn("历史重复解释", page)
            self.assertIn('href="#evidence-1-chunk-1"', page)
            record = self.app.extensions["runtime_store"].latest_generation(matter_id)
            self.assertEqual(json.loads(record["response_json"])["answer"]["essential_explanation"][0]["claim"], "历史重复解释")


if __name__ == "__main__":
    unittest.main()
