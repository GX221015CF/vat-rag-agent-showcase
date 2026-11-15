from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from test_p2a_routes import FakeLLMService, FakeRetrievalService
from test_p2b_answer_view import answer_payload, generation
from test_p2b_deepseek import FakeClient, FakeResponse
from tax_rag.domain.conversation import consultation_context
from tax_rag.domain.scope_router import route_scope
from tax_rag.models import SearchHit
from tax_rag.services.deepseek_service import DeepSeekError, DeepSeekService, validate_effective_date_claims
from tax_rag.web.answer_view import build_answer_view
from tax_rag.web.app import create_app


def five_hits():
    return [{"chunk_id": f"chunk-{i}", "rank": i, "document_title": "测试法规",
             "anchor": f"第{i}条", "text": f"原文{i}", "source_json": "{}"} for i in range(1, 6)]


class CitationMappingTests(unittest.TestCase):
    def test_only_visible_citations_are_continuous_in_first_use_order(self):
        payload = answer_payload()
        payload["presentation_version"] = "points-v3"
        payload["direct_conclusion"] = {"items": [
            {"text": "第一判断", "citation_chunk_ids": ["chunk-4", "chunk-1"]},
            {"text": "第二判断", "citation_chunk_ids": ["chunk-5", "chunk-4"]},
        ]}
        payload["rules"] = [{"claim": "历史隐藏说明", "citation_chunk_ids": ["chunk-2"]}]
        original = json.dumps(payload, ensure_ascii=False)
        view = build_answer_view(generation(payload), five_hits())
        self.assertEqual([hit["chunk_id"] for hit in view["cited_evidence"]], ["chunk-4", "chunk-1", "chunk-5"])
        self.assertEqual([hit["rank"] for hit in view["cited_evidence"]], [1, 2, 3])
        self.assertEqual([c["rank"] for c in view["conclusion_items"][1]["citations"]], [3, 1])
        self.assertIn("第一判断[证据1][证据2]", view["copy_text"])
        self.assertIn("第二判断[证据3][证据1]", view["copy_text"])
        self.assertNotIn("历史隐藏说明", view["copy_text"])
        self.assertEqual(original, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(five_hits()[3]["rank"], 4)

    def test_legacy_text_answer_uses_same_mapping_and_unreadable_data_falls_back(self):
        payload = answer_payload()
        payload["direct_conclusion"]["citation_chunk_ids"] = ["chunk-5", "chunk-1"]
        view = build_answer_view(generation(payload), five_hits())
        self.assertEqual([c["rank"] for c in view["direct_conclusion"]["citations"]], [1, 2])
        self.assertEqual([h["chunk_id"] for h in view["cited_evidence"]], ["chunk-5", "chunk-1"])
        payload["direct_conclusion"]["citation_chunk_ids"] = ["missing"]
        self.assertIsNone(build_answer_view(generation(payload), five_hits()))

    def test_no_citations_can_keep_structured_question_without_claiming_cited_sources(self):
        payload = answer_payload()
        payload.update(status="NEED_MORE_FACTS", follow_up_question="你关注哪个环节？")
        payload["direct_conclusion"]["citation_chunk_ids"] = []
        view = build_answer_view(generation(payload), five_hits())
        self.assertEqual(view["cited_evidence"], [])


class CitationPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        retrieval = FakeRetrievalService()
        original_search = retrieval.search

        def search(query):
            gen, hits, channels = original_search(query)
            return gen, [replace(hits[0], chunk_id=f"chunk-{i}") for i in range(1, 6)], channels

        retrieval.search = search
        self.llm = FakeLLMService()
        original_generate = self.llm.generate

        def generate(query, hits, calculation=None, **context):
            result = original_generate(query, hits, calculation, **context)
            answer = result.answer
            answer.update(presentation_version="points-v3", rules=[], critical_gaps=[], essential_explanation=[])
            answer["direct_conclusion"] = {"items": [
                {"text": "第一判断", "citation_chunk_ids": ["chunk-4", "chunk-1"]},
                {"text": "第二判断", "citation_chunk_ids": ["chunk-5", "chunk-4"]},
            ]}
            return replace(result, answer=answer)

        self.llm.generate = generate
        self.app = create_app({"TESTING": True, "SECRET_KEY": "citation-tests-with-a-long-secret-value",
            "RUNTIME_DATABASE_PATH": Path(self.temp.name) / "runtime.db",
            "RETRIEVAL_SERVICE": retrieval, "LLM_SERVICE": self.llm,
            "CLEANUP_ON_STARTUP": False, "CLEANUP_BEFORE_REQUEST": False})
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]
        self.client.post("/cloud-notice", data={"csrf_token": self.csrf, "accepted": "yes"})
        self.url = self.client.post("/matters", data={"csrf_token": self.csrf}).location

    def tearDown(self):
        self.temp.cleanup()

    def ask(self):
        return self.client.post(self.url + "/messages", data={"csrf_token": self.csrf,
            "submission_token": secrets.token_urlsafe(24), "content": "增值税如何判断？"},
            headers={"Accept": "application/json"})

    def test_each_round_shows_three_cited_cards_and_five_distinct_technical_cards(self):
        self.ask()
        self.ask()
        page = self.client.get(self.url).get_data(as_text=True)
        for number in (1, 2):
            self.assertIn(f'id="evidence-{number}-chunk-4"', page)
            self.assertNotIn(f'id="evidence-{number}-chunk-2"', page)
            self.assertIn(f'id="retrieved-{number}-chunk-2"', page)
            self.assertEqual(page.count(f'id="evidence-{number}-chunk-4"'), 1)
            self.assertIn(f'href="#evidence-{number}-chunk-4"', page)
        self.assertEqual(page.count("本轮引用原文"), 2)
        self.assertIn("全部检索依据（原检索顺序）", page)
        self.assertIn("第一判断[证据1][证据2]", page)

    def test_model_failure_keeps_explicit_retrieved_fallback(self):
        def fail(*args, **kwargs):
            raise DeepSeekError("TIMEOUT", "test timeout")
        self.llm.generate = fail
        self.ask()
        page = self.client.get(self.url).get_data(as_text=True)
        self.assertIn("已检索依据", page)
        self.assertNotIn("本轮引用原文", page)
        self.assertIn('id="evidence-1-chunk-5"', page)

    def test_success_without_citations_uses_retrieved_fallback(self):
        def clarify(query, hits, calculation=None, **context):
            return replace(FakeLLMService().generate(query, hits, calculation, **context), answer={
                **answer_payload(), "status": "NEED_MORE_FACTS", "rules": [],
                "follow_up_question": "你关注哪个环节？",
                "direct_conclusion": {"text": "需先区分交易环节。", "citation_chunk_ids": []}})
        self.llm.generate = clarify
        self.ask()
        page = self.client.get(self.url).get_data(as_text=True)
        self.assertIn("已检索依据", page)
        self.assertNotIn("本轮引用原文", page)

    def test_unreadable_historical_format_keeps_original_text_and_retrieval_numbers(self):
        self.ask()
        con = self.app.extensions["runtime_store"].connect()
        try:
            con.execute("UPDATE generation_runs SET response_json=?", (json.dumps({"status": "old-format"}),))
            con.commit()
        finally:
            con.close()
        page = self.client.get(self.url).get_data(as_text=True)
        self.assertIn("已检索依据", page)
        self.assertIn("历史文本中的证据编号按原记录核对", page)
        self.assertIn("第一判断", page)


class ProcessingAndDateTests(unittest.TestCase):
    def test_generic_processing_and_role_followups_do_not_invent_welfare(self):
        question = "委托加工怎么缴纳增值税"
        self.assertEqual(route_scope(question).status, "IN_SCOPE")
        query, context = consultation_context(question, [])
        self.assertEqual(query, question)
        self.assertEqual(context, [])
        history = [{"role": "USER", "content": question},
                   {"role": "ASSISTANT", "content": "需要你确认\n受托加工费还是委托方收回？"}]
        query, context = consultation_context("我们是受托方，一般纳税人收取加工费，适用什么税率？", history)
        self.assertTrue(context)
        self.assertNotIn("福利", query)

    def test_welfare_use_and_identity_survive_short_processing_followup(self):
        history = [{"role": "USER", "content": "一般纳税人公司自产货物用于员工福利"}]
        query, context = consultation_context("那委托加工的呢？", history)
        self.assertIn("员工福利", query)
        self.assertIn("一般纳税人", query)

    def test_wrong_explicit_effective_date_is_rejected_but_transaction_dates_are_allowed(self):
        hit = SearchHit("law", "d", "v", "中华人民共和国增值税法", "第三十八条", "2026年1月1日起施行",
            {"version": {"effective_from": "2026-01-01"}}, 1, "exact")
        def answer(text):
            return {"direct_conclusion": {"items": [{"text": text, "citation_chunk_ids": ["law"]}]}}
        for text in ("该法自2026年11月1日起施行。", "《增值税法》于2026年11月1日生效。"):
            with self.subTest(text=text), self.assertRaises(DeepSeekError) as error:
                validate_effective_date_claims(answer(text), [hit])
            self.assertEqual(error.exception.code, "EFFECTIVE_DATE_MISMATCH")
        for text in ("该法自2026年1月1日起施行。", "2026年11月1日发生该笔交易。", "该法适用于2026年11月1日的交易。"):
            validate_effective_date_claims(answer(text), [hit])
        context = DeepSeekService().build_context("施行日？", [hit], None, as_of="2026-11-15")
        self.assertEqual(context["document_effective_dates"], [("中华人民共和国增值税法", "2026-01-01")])
        self.assertEqual(context["policy_as_of"], "2026-11-15")

    def test_actual_adapter_rejects_wrong_effective_date_after_contract_validation(self):
        class WrongDateClient(FakeClient):
            def post(self, url, headers, json):
                payload = answer_payload()
                payload["direct_conclusion"] = {"text": "该法自2026年11月1日起施行。", "citation_chunk_ids": ["chunk-1"]}
                return FakeResponse({"choices": [{"message": {"content": json_module.dumps(payload)}}]})

        json_module = json
        hit = SearchHit("chunk-1", "d", "v", "中华人民共和国增值税法", "第三十八条", "2026年1月1日起施行",
            {"version": {"effective_from": "2026-01-01"}}, 1, "exact")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}), patch(
            "tax_rag.services.deepseek_service.httpx.Client", WrongDateClient
        ), self.assertRaises(DeepSeekError) as error:
            DeepSeekService().generate("何时施行？", [hit])
        self.assertEqual(error.exception.code, "EFFECTIVE_DATE_MISMATCH")
