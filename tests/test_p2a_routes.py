from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from pathlib import Path

from tax_rag.models import SearchHit
from tax_rag.retrieval.query_planner import plan_query
from tax_rag.services.deepseek_service import GenerationResult
from tax_rag.web.app import create_app


class FakeRetrievalService:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.generation = {
            "generation_id": "gen_ce8f73e6c522c1c54b26",
            "chunk_count": 342,
            "status": "ACTIVE",
        }
        counts = [100, 80, 70, 50, 42]
        self.documents = [
            {
                "title": f"测试资料{i}",
                "legal_level": "法律",
                "approval_status": "APPROVED_ACTIVE",
                "chunk_count": count,
                "source_filename": f"测试资料{i}.docx",
                "source_sha256": f"{i}" * 64,
            }
            for i, count in enumerate(counts, start=1)
        ]

    def active_generation(self):
        return self.generation

    def knowledge_overview(self):
        return {"generation": self.generation, "documents": self.documents}

    def search(self, query: str):
        self.search_calls.append(query)
        hit = SearchHit(
            "chunk-1",
            "doc-1",
            "version-1",
            "中华人民共和国增值税法",
            "第十条",
            "销售货物的测试证据原文",
            {"paragraph_start": 10, "paragraph_end": 10},
            0.1,
            "rrf",
            {"exact": 1, "fts": 2},
        )
        return self.generation, [hit], {"exact": [hit]}

    def search_with_trace(self, query: str):
        generation, hits, channels = self.search(query)
        return generation, hits, channels, plan_query(query)


class FakeLLMService:
    model_id = "deepseek-flash"
    prompt_version = "vat-answer-v1"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, query, hits, calculation=None, **context):
        self.calls.append({"query": query, "hits": hits, "calculation": calculation, **context})
        return GenerationResult(
            answer={
                "status": "ANSWERED",
                "direct_conclusion": {
                    "text": "这是测试结论",
                    "citation_chunk_ids": [hits[0].chunk_id],
                },
                "prerequisites": [],
                "rules": [
                    {
                        "claim": "这是测试规则",
                        "citation_chunk_ids": [hits[0].chunk_id],
                    }
                ],
                "exceptions": [],
                "uncertainties": [],
                "calculation_explanation": None,
                "follow_up_question": None,
            },
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            response_id="response-test",
            request_context_hash="a" * 64,
        )


class P2ARouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.retrieval = FakeRetrievalService()
        self.llm = FakeLLMService()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret-key-with-at-least-32-characters",
                "RUNTIME_DATABASE_PATH": Path(self.temp.name) / "runtime.db",
                "RETRIEVAL_SERVICE": self.retrieval,
                "LLM_SERVICE": self.llm,
                "CLEANUP_ON_STARTUP": False,
                "CLEANUP_BEFORE_REQUEST": False,
            }
        )
        self.client = self.app.test_client()
        self.client.get("/")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def csrf(self) -> str:
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def create_and_accept(self) -> str:
        response = self.client.post("/matters", data={"csrf_token": self.csrf()})
        matter_id = response.headers["Location"].split("/")[2]
        response = self.client.post(
            f"/matters/{matter_id}/notice",
            data={"csrf_token": self.csrf(), "accepted": "yes"},
        )
        self.assertEqual(response.status_code, 302)
        return matter_id

    def submit(self, matter_id: str, content: str, submission_token: str | None = None):
        return self.client.post(
            f"/matters/{matter_id}/messages",
            data={
                "csrf_token": self.csrf(),
                "submission_token": submission_token or secrets.token_urlsafe(24),
                "content": content,
            },
            follow_redirects=True,
        )

    def test_unconfirmed_json_submit_on_draft_never_calls_model(self) -> None:
        response = self.client.post("/matters", data={"csrf_token": self.csrf()})
        matter_id = response.headers["Location"].split("/")[2]
        result = self.client.post(
            f"/matters/{matter_id}/messages",
            data={"csrf_token": self.csrf(), "submission_token": secrets.token_urlsafe(24), "content": "公开法规问题"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.llm.calls, [])
        self.assertIn("data-cloud-dialog", self.client.get("/").get_data(as_text=True))

    def test_confirmed_create_accepts_notice_before_redirecting_to_chat(self) -> None:
        response = self.client.post("/matters", data={"csrf_token": self.csrf(), "accepted": "yes", "content": "公开法规问题"})
        matter_id = response.headers["Location"].rsplit("/", 1)[-1]
        self.assertTrue(response.headers["Location"].endswith(f"/matters/{matter_id}"))
        self.assertEqual(self.app.extensions["runtime_store"].get_matter(matter_id)["status"], "ACTIVE")
        self.assertIn("公开法规问题", self.client.get(response.headers["Location"]).get_data(as_text=True))

    def test_missing_or_wrong_csrf_returns_400(self) -> None:
        self.assertEqual(self.client.post("/matters", data={}).status_code, 400)
        self.assertEqual(
            self.client.post("/matters", data={"csrf_token": "wrong"}).status_code,
            400,
        )

    def test_security_headers_and_nonlocal_request_is_forbidden(self) -> None:
        local = self.client.get("/")
        self.assertEqual(local.headers["X-Frame-Options"], "DENY")
        self.assertEqual(local.headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", local.headers["Content-Security-Policy"])

        self.app.testing = False
        try:
            remote = self.client.get("/", environ_base={"REMOTE_ADDR": "192.0.2.1"})
        finally:
            self.app.testing = True
        self.assertEqual(remote.status_code, 403)

    def test_notice_is_required_once_per_browser_session_and_version(self) -> None:
        response = self.client.post("/matters", data={"csrf_token": self.csrf()})
        first_id = response.headers["Location"].split("/")[2]
        notice = self.client.get(response.headers["Location"])
        self.assertIn('data-consent-required="true"', notice.get_data(as_text=True))
        rejected = self.client.post(
            f"/matters/{first_id}/notice",
            data={"csrf_token": self.csrf()},
        )
        self.assertEqual(rejected.status_code, 400)

        accepted = self.client.post(
            f"/matters/{first_id}/notice",
            data={"csrf_token": self.csrf(), "accepted": "yes"},
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertTrue(accepted.headers["Location"].endswith(f"/matters/{first_id}"))
        repeated = self.client.get(f"/matters/{first_id}/notice")
        self.assertEqual(repeated.status_code, 302)

        second = self.client.post("/matters", data={"csrf_token": self.csrf()})
        self.assertNotIn(first_id, second.headers["Location"])
        self.assertIn('data-consent-required="false"', self.client.get(second.location).get_data(as_text=True))

    def test_out_of_scope_and_unclear_queries_do_not_retrieve(self) -> None:
        matter_id = self.create_and_accept()
        self.submit(matter_id, "出口退税怎样申报")
        self.submit(matter_id, "我有一个交易问题")
        self.assertEqual(self.retrieval.search_calls, [])
        self.assertEqual(self.llm.calls, [])

    def test_in_scope_query_records_generation_and_evidence(self) -> None:
        matter_id = self.create_and_accept()
        page = self.submit(matter_id, "一般纳税人销售货物的增值税纳税义务何时发生")
        text = page.get_data(as_text=True)
        store = self.app.extensions["runtime_store"]
        route = store.latest_route(matter_id)

        self.assertEqual(self.retrieval.search_calls, ["一般纳税人销售货物的增值税纳税义务何时发生"])
        self.assertEqual(route["generation_id"], "gen_ce8f73e6c522c1c54b26")
        self.assertIn("查询规划=query-planner-v1", route["route_reason"])
        self.assertIn("中华人民共和国增值税法", text)
        self.assertIn("销售货物的测试证据原文", text)
        self.assertIn("这是测试结论", text)
        self.assertIn('class="structured-answer structured-answer-answered"', text)
        self.assertIn('class="answer-section answer-conclusion"', text)
        self.assertIn('class="answer-more"', text)
        self.assertIn("本轮引用原文", text)
        self.assertIn("证据1", text)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(store.latest_generation(matter_id)["status"], "SUCCESS")

    def test_duplicate_submission_token_does_not_add_round_or_call_services_twice(self) -> None:
        matter_id = self.create_and_accept()
        token = secrets.token_urlsafe(24)
        self.submit(matter_id, "一般纳税人销售货物的增值税问题", token)
        repeated = self.submit(matter_id, "一般纳税人销售货物的增值税问题", token)
        store = self.app.extensions["runtime_store"]

        self.assertEqual(store.get_matter(matter_id)["round_count"], 1)
        self.assertEqual(len(store.list_messages(matter_id)), 2)
        self.assertEqual(len(self.retrieval.search_calls), 1)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("这个问题已经提交", repeated.get_data(as_text=True))

    def test_missing_or_invalid_submission_token_returns_400(self) -> None:
        matter_id = self.create_and_accept()
        missing = self.client.post(
            f"/matters/{matter_id}/messages",
            data={"csrf_token": self.csrf(), "content": "一般纳税人增值税问题"},
        )
        invalid = self.client.post(
            f"/matters/{matter_id}/messages",
            data={
                "csrf_token": self.csrf(),
                "submission_token": "too-short",
                "content": "一般纳税人增值税问题",
            },
        )
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(invalid.status_code, 400)

    def test_non_generated_message_uses_plain_text_fallback(self) -> None:
        matter_id = self.create_and_accept()
        page = self.submit(matter_id, "出口退税怎样申报")
        text = page.get_data(as_text=True)
        self.assertIn("当前范围", text)
        self.assertIn('class="message-content"', text)
        self.assertNotIn('class="structured-answer ', text)

    def test_structured_model_fields_are_html_escaped(self) -> None:
        def malicious_generate(query, hits, calculation=None, **context):
            return GenerationResult(
                answer={
                    "status": "ANSWERED",
                    "direct_conclusion": {
                        "text": '<img src=x onerror="alert(1)">',
                        "citation_chunk_ids": [hits[0].chunk_id],
                    },
                    "prerequisites": ["<script>bad()</script>"],
                    "rules": [
                        {
                            "claim": "<b>规则</b>",
                            "citation_chunk_ids": [hits[0].chunk_id],
                        }
                    ],
                    "exceptions": [],
                    "uncertainties": [],
                    "calculation_explanation": None,
                    "follow_up_question": None,
                },
                model_id=self.llm.model_id,
                prompt_version=self.llm.prompt_version,
                response_id="response-malicious",
                request_context_hash="b" * 64,
            )

        self.llm.generate = malicious_generate
        matter_id = self.create_and_accept()
        page = self.submit(matter_id, "一般纳税人销售货物的增值税问题")
        text = page.get_data(as_text=True)
        self.assertNotIn('<img src=x onerror="alert(1)">', text)
        self.assertNotIn("<script>bad()</script>", text)
        self.assertNotIn("<b>规则</b>", text)
        self.assertIn("&lt;img src=x onerror=", text)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", text)
        self.assertNotIn("&lt;b&gt;规则&lt;/b&gt;", text)

    def test_user_html_is_escaped(self) -> None:
        matter_id = self.create_and_accept()
        page = self.submit(matter_id, "<script>alert('x')</script>")
        text = page.get_data(as_text=True)
        self.assertNotIn("<script>alert('x')</script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_matter_page_loads_latest_message_scroll_behavior(self) -> None:
        matter_id = self.create_and_accept()
        page = self.submit(matter_id, "一般纳税人增值税发票问题")
        text = page.get_data(as_text=True)
        self.assertIn("data-scroll-latest", text)
        self.assertIn('/static/app.js', text)

        script = self.client.get("/static/app.js")
        try:
            self.assertEqual(script.status_code, 200)
            self.assertIn('latest.scrollIntoView({ block: "start"', script.get_data(as_text=True))
            self.assertNotIn("conversation.scrollTop", script.get_data(as_text=True))
        finally:
            script.close()

    def test_answer_form_shows_waiting_feedback_and_short_button_label(self) -> None:
        matter_id = self.create_and_accept()
        page = self.client.get(f"/matters/{matter_id}")
        text = page.get_data(as_text=True)
        self.assertIn("data-answer-form", text)
        self.assertIn('name="submission_token"', text)
        self.assertIn("生成回答", text)
        self.assertNotIn("检索并生成证据化回答", text)
        self.assertIn("正在处理，请稍候", text)
        self.assertIn("通常需要20～40秒", text)

        script = self.client.get("/static/app.js")
        try:
            javascript = script.get_data(as_text=True)
            self.assertIn('label.textContent = form.matches("[data-retry-form]")', javascript)
            self.assertIn('form.matches("[data-calculation-form]") ? "计算中…"', javascript)
            self.assertIn('"正在生成…"', javascript)
            self.assertIn('button.setAttribute("aria-busy", String(busy))', javascript)
            self.assertIn("button.disabled = busy", javascript)
            self.assertIn("textarea.readOnly = true", javascript)
            self.assertIn("seconds >= 45", javascript)
            self.assertIn("seconds >= 90", javascript)
            self.assertIn('window.addEventListener("pageshow", (event) => { if (event.persisted) activate(); })', javascript)
        finally:
            script.close()

    def test_confirmed_single_calculation_is_saved_and_sent_to_llm_on_next_question(self) -> None:
        matter_id = self.create_and_accept()
        page = self.client.post(
            f"/matters/{matter_id}/calculations/single",
            data={
                "csrf_token": self.csrf(),
                "amount": "1130000.00",
                "amount_type": "TAX_INCLUDED",
                "tax_rate_percent": "13",
                "confirmed": "yes",
            },
            follow_redirects=True,
        )
        text = page.get_data(as_text=True)
        self.assertIn("¥1000000.00", text)
        self.assertIn("¥130000.00", text)
        self.submit(matter_id, "一般纳税人销售货物的增值税如何处理")
        self.assertIsNotNone(self.llm.calls[-1]["calculation"])
        self.assertEqual(
            self.llm.calls[-1]["calculation"]["outputs"]["tax_amount"],
            "130000.00",
        )
        rendered = self.client.get(f"/matters/{matter_id}").get_data(as_text=True)
        self.assertIn('class="answer-calculation-grid"', rendered)
        self.assertIn("本地确定性结果", rendered)
        self.assertIn("1000000.00", rendered)
        self.assertIn("130000.00", rendered)
        stored = json.loads(
            self.app.extensions["runtime_store"].latest_generation(matter_id)[
                "response_json"
            ]
        )
        self.assertEqual(stored["schema_version"], "answer-record-v1")
        self.assertEqual(
            stored["calculation_snapshot"]["calculation_run_id"],
            self.llm.calls[-1]["calculation"]["calculation_run_id"],
        )

    def test_knowledge_page_shows_five_documents_and_342_chunks(self) -> None:
        page = self.client.get("/knowledge")
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(text.count("APPROVED_ACTIVE"), 5)
        self.assertEqual(sum(item["chunk_count"] for item in self.retrieval.documents), 342)
        self.assertIn("gen_ce8f73e6c522c1c54b26", text)


if __name__ == "__main__":
    unittest.main()
