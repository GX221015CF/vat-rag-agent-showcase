from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_p2a_routes import FakeLLMService, FakeRetrievalService
from tax_rag.domain.conversation import consultation_context, answer_task_for_question
from tax_rag.services.deepseek_service import DeepSeekError
from tax_rag.services.evidence_service import consultation_date
from tax_rag.web.app import create_app


class WebRagTests(unittest.TestCase):
    def test_answer_task_describes_question_without_inventing_facts(self):
        task = answer_task_for_question("视同交易的纳税义务何时发生？")
        self.assertEqual(task["conclusion_item_limit"], 1)
        self.assertIn("具体日期", task["requested_result"][0])
        self.assertEqual(answer_task_for_question("能抵扣吗？")["conclusion_item_limit"], 1)
        self.assertEqual(answer_task_for_question("何时发生纳税义务，税率是多少？")["conclusion_item_limit"], 2)
        self.assertEqual(answer_task_for_question("视同销售有哪些？")["conclusion_item_limit"], 6)
        self.assertEqual(answer_task_for_question("发票用途错误，如何更正？")["conclusion_item_limit"], 6)
        self.assertNotIn("无偿", str(answer_task_for_question("我买来的产品给员工发福利，要交增值税吗")))
        task = answer_task_for_question("小规模卖货收入9万元，能直接免税吗？")
        self.assertEqual(len(task["required_fact_checks"]), 3)
        self.assertIn("专票", task["required_fact_checks"][-1])
        self.assertNotIn("required_fact_checks", answer_task_for_question("一般纳税人能抵扣吗？"))
        task = answer_task_for_question("一般纳税人公司给员工发货物福利，要缴增值税吗？")
        self.assertIn("扣款", task["required_fact_checks"][-1])
        self.assertNotIn("required_fact_checks", answer_task_for_question("福利进项能抵扣吗？"))
        self.assertNotIn("required_fact_checks", answer_task_for_question("公司自产货物发给员工作福利，要缴税吗？"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.retrieval = FakeRetrievalService()
        self.llm = FakeLLMService()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-web-rag-with-a-long-secret-value",
            "RUNTIME_DATABASE_PATH": Path(self.temp.name) / "runtime.db",
            "RETRIEVAL_SERVICE": self.retrieval,
            "LLM_SERVICE": self.llm,
            "CLEANUP_ON_STARTUP": False,
            "CLEANUP_BEFORE_REQUEST": False,
        })
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]
        self.matter_id = self.new_matter()

    def tearDown(self):
        self.temp.cleanup()

    def new_matter(self):
        response = self.client.post("/matters", data={"csrf_token": self.csrf})
        matter_id = response.location.split("/")[2]
        self.client.post(f"/matters/{matter_id}/notice", data={"csrf_token": self.csrf, "accepted": "yes"})
        return matter_id

    def ask(self, question, token=None):
        return self.client.post(f"/matters/{self.matter_id}/messages", data={
            "csrf_token": self.csrf,
            "submission_token": token or secrets.token_urlsafe(24),
            "content": question,
        }, headers={"Accept": "application/json"})

    def test_original_and_natural_questions_reach_generation(self):
        for question in ("视同销售的行为有哪几种？", "哪些情况虽然没卖货，也需要交增值税？"):
            result = self.ask(question).get_json()
            self.assertEqual(result["deepseek_status"], "SUCCESS")
            self.assertEqual(result["generation_id"], self.retrieval.generation["generation_id"])
        self.assertEqual(len(self.llm.calls), 2)

    def test_original_purchased_question_reaches_model_without_assumed_facts(self):
        question = "我买来的产品给员工发福利，要交增值税吗"
        result = self.ask(question).get_json()
        self.assertEqual(result["deepseek_status"], "SUCCESS")
        self.assertEqual(self.llm.calls[-1]["query"], question)
        self.assertEqual(self.llm.calls[-1]["conversation_context"], [])
        self.ask("是一般纳税人公司免费发放，员工不付钱，也不扣工资。")
        self.ask("改成从员工工资扣款呢？")
        history = self.llm.calls[-1]["conversation_context"]
        self.assertTrue(any("不扣工资" in row["content"] for row in history if row["role"] == "user"))
        self.assertEqual(self.llm.calls[-1]["query"], "改成从员工工资扣款呢？")

    def test_required_law_is_prioritized_without_dropping_candidates(self):
        _, hits, _ = self.retrieval.search("增值税")
        required = replace(hits[0], chunk_id="required-law-3", anchor="第三条")
        with patch("tax_rag.services.matter_service.required_policy_evidence", return_value=[required, hits[0]]):
            self.ask("增值税员工支付价款取得货物，如何处理？")
        evidence = self.llm.calls[-1]["hits"]
        self.assertEqual([hit.chunk_id for hit in evidence], ["required-law-3", hits[0].chunk_id])

    def test_follow_up_preserves_facts_and_new_matter_is_isolated(self):
        self.ask("公司把自产产品发给员工，要交税吗？")
        result = self.ask("那外购的呢？").get_json()
        self.assertIn("发给员工", result["retrieval_query"])
        self.assertIn("外购", result["retrieval_query"])
        self.assertNotIn("进项", result["retrieval_query"])
        self.assertTrue(self.llm.calls[-1]["conversation_context"])
        self.matter_id = self.new_matter()
        result = self.ask("那外购的呢？").get_json()
        self.assertEqual(result["error_code"], "NEED_SCOPE_CONFIRMATION")

    def test_citations_bind_to_each_round_and_can_be_read_back(self):
        self.ask("视同销售的行为有哪几种？")
        self.ask("增值税纳税义务何时发生？")
        page = self.client.get(f"/matters/{self.matter_id}").get_data(as_text=True)
        for number in (1, 2):
            self.assertIn(f'href="#evidence-{number}-chunk-1"', page)
            self.assertIn(f'id="evidence-{number}-chunk-1"', page)
            evidence = self.client.get(f"/matters/{self.matter_id}/evidence/{number}/chunk-1").get_json()
            self.assertEqual(evidence["text"], "销售货物的测试证据原文")
        self.assertEqual(self.client.get(f"/matters/{self.matter_id}/evidence/1/invented").status_code, 404)

    def test_failed_generation_can_retry_without_adding_round(self):
        original = self.llm.generate

        def fail(*args, **kwargs):
            raise DeepSeekError("TIMEOUT", "test timeout")

        self.llm.generate = fail
        result = self.ask("视同销售的行为有哪几种？").get_json()
        self.assertEqual(result["error_code"], "TIMEOUT")
        store = self.app.extensions["runtime_store"]
        user = next(row for row in store.list_messages(self.matter_id) if row["role"] == "USER")
        self.llm.generate = original
        response = self.client.post(f"/matters/{self.matter_id}/messages/{user['message_id']}/retry",
                                    data={"csrf_token": self.csrf}, headers={"Accept": "application/json"})
        self.assertEqual(response.get_json()["deepseek_status"], "SUCCESS")
        self.assertEqual(store.get_matter(self.matter_id)["round_count"], 1)
        self.assertEqual(len(store.list_messages(self.matter_id)), 2)
        repeated = self.client.post(f"/matters/{self.matter_id}/messages/{user['message_id']}/retry",
                                   data={"csrf_token": self.csrf}, headers={"Accept": "application/json"})
        self.assertEqual(repeated.status_code, 409)

    def test_duplicate_and_in_progress_submissions_do_not_call_model(self):
        token = secrets.token_urlsafe(24)
        self.ask("视同销售的行为有哪几种？", token)
        self.assertEqual(self.ask("视同销售的行为有哪几种？", token).status_code, 409)
        self.assertEqual(len(self.llm.calls), 1)
        service = self.app.extensions["matter_service"]
        with service._processing(self.matter_id):
            response = self.ask("增值税发票如何处理？")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.llm.calls), 1)

    def test_history_date_and_reset_are_explicit(self):
        history = [{"role": "USER", "content": "公司把自产产品发给员工，要交税吗？"}]
        query, context = consultation_context("换个问题，销售额如何确定？", history)
        self.assertEqual(context, [])
        self.assertNotIn("员工", query)
        self.assertEqual(consultation_date("2026年8月发生交易").isoformat(), "2026-08-01")
        result = self.ask("2025年发生的增值税交易如何处理？").get_json()
        self.assertEqual(result["error_code"], "EVIDENCE_PERIOD_UNSUPPORTED")
        self.assertEqual(self.llm.calls, [])

    def test_adapter_receives_metadata_and_context_without_extra_call(self):
        from tax_rag.services.deepseek_service import DeepSeekService
        from tax_rag.models import SearchHit
        hit = SearchHit("c", "d", "v", "法规", "第五条", "原文", {"version": {"document_number": "法律文号"}}, 1, "rrf")
        payload = DeepSeekService().build_context("那外购的呢？", [hit], None, [{"role": "user", "content": "用于员工福利"}], "2026-11-14")
        self.assertEqual(payload["approved_evidence"][0]["citation_number"], 1)
        self.assertEqual(payload["approved_evidence"][0]["version"]["document_number"], "法律文号")
        self.assertEqual(payload["conversation_history"][0]["content"], "用于员工福利")
        self.assertEqual(payload["answer_task"]["conclusion_item_limit"], 1)

    def test_purchased_welfare_adds_law_input_deduction_evidence(self):
        original = self.retrieval.search

        def search(query):
            generation, hits, channels = original(query)
            if "第二十二条" in query:
                hits = [replace(hits[0], chunk_id="law-22", anchor="第二十二条")]
            return generation, hits, channels

        self.retrieval.search = search
        self.ask("公司把自产产品发给员工，要交税吗？")
        response = self.ask("那外购的呢？").get_json()
        self.assertFalse(any(item["evidence_id"] == "law-22" for item in response["evidence"]))
        response = self.ask("那外购的进项能抵扣吗？").get_json()
        self.assertTrue(any(item["evidence_id"] == "law-22" for item in response["evidence"]))
        self.assertTrue(any(hit.chunk_id == "law-22" for hit in self.llm.calls[-1]["hits"]))
        for source in ("购进", "购入", "买来", "购买"):
            with self.subTest(source=source):
                self.matter_id = self.new_matter()
                response = self.ask(f"公司{source}一批商品发给员工作福利，增值税怎么处理？").get_json()
                self.assertTrue(any(item["evidence_id"] == "law-22" for item in response["evidence"]))

    def test_short_citation_ids_resolve_only_to_provided_evidence(self):
        from tax_rag.services.deepseek_service import resolve_citation_ids
        from tax_rag.domain.answer_contract import AnswerValidationError, validate_answer_payload
        _generation, hits, _channels = self.retrieval.search("视同销售")
        answer = self.llm.generate("视同销售", hits).answer
        answer["direct_conclusion"]["citation_chunk_ids"] = ["E1"]
        answer["rules"][0]["citation_chunk_ids"] = ["E1"]
        resolved = resolve_citation_ids(answer, hits)
        self.assertEqual(validate_answer_payload(resolved, {"chunk-1"}, None)["direct_conclusion"]["citation_chunk_ids"], ["chunk-1"])
        answer["rules"][0]["citation_chunk_ids"] = ["E999"]
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(resolve_citation_ids(answer, hits), {"chunk-1"}, None)

    def test_fact_answer_retains_the_prior_purchased_follow_up(self):
        history = [
            {"role": "USER", "content": "公司把自产产品发给员工，要交税吗？"},
            {"role": "ASSISTANT", "content": "按福利用途核对。"},
            {"role": "USER", "content": "那外购的呢？"},
            {"role": "ASSISTANT", "content": "需要你确认\\n员工是否支付价款？"},
        ]
        query, dialogue = consultation_context("员工不付钱，直接发给他们作为福利", history)
        self.assertIn("外购", query)
        self.assertIn("不付钱", query)
        self.assertEqual(sum(item["role"] == "user" for item in dialogue), 2)
        history[-1]["content"] = "外购货物无偿转让需要核对销项。"
        query, dialogue = consultation_context("员工不付钱，也不从工资中扣减，直接发给他们作为福利", history)
        self.assertIn("外购", query)
        self.assertIn("不从工资中扣减", query)
        history.extend([
            {"role": "USER", "content": "员工不付钱，直接发给他们作为福利"},
            {"role": "ASSISTANT", "content": "请补充是否有扣款。"},
            {"role": "USER", "content": "不扣工资"},
            {"role": "ASSISTANT", "content": "请确认交付。"},
        ])
        query, dialogue = consultation_context("员工不付钱，也不从工资中扣减，直接发给他们作为福利", history)
        self.assertIn("外购", query)
        self.assertIn("公司把自产产品发给员工", query)
        self.assertIn("外购", str(dialogue))
        from tax_rag.domain.scope_router import route_scope
        self.assertEqual(route_scope(query).status, "IN_SCOPE")

    def test_question_about_missing_facts_carries_in_scope_context(self):
        history = [
            {"role": "USER", "content": "公司给员工发放一批商品，要判断增值税吗？"},
            {"role": "ASSISTANT", "content": "适用规则取决于商品来源和员工是否支付价款。"},
        ]
        query, dialogue = consultation_context(
            "你刚才说要看来源和是否支付，我还不清楚，具体要补充哪些事实？",
            history,
        )
        from tax_rag.domain.scope_router import route_scope
        self.assertIn("公司给员工发放一批商品", query)
        self.assertEqual(route_scope(query).status, "IN_SCOPE")
        self.assertEqual(dialogue[0]["content"], history[0]["content"])


if __name__ == "__main__":
    unittest.main()
