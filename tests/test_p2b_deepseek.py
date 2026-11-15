from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from tax_rag.models import SearchHit
from tax_rag.services.deepseek_service import DeepSeekError, DeepSeekService, parse_answer_json, restore_answer_field_levels, restore_points_version


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    last_request = None
    last_options = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        FakeClient.last_options = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, headers, json):
        FakeClient.last_request = {"url": url, "headers": headers, "json": json}
        answer = {
            "status": "ANSWERED",
            "direct_conclusion": {"text": "测试结论", "citation_chunk_ids": ["chunk-1"]},
            "prerequisites": [],
            "rules": [{"claim": "测试规则", "citation_chunk_ids": ["chunk-1"]}],
            "exceptions": [],
            "uncertainties": [],
            "calculation_explanation": None,
            "follow_up_question": None,
        }
        return FakeResponse(
            {
                "id": "response-1",
                "choices": [{"message": {"content": json_module.dumps(answer, ensure_ascii=False)}}],
            }
        )


class TruncatedClient(FakeClient):
    def post(self, url, headers, json):
        return FakeResponse(
            {
                "id": "response-truncated",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"status":"ANSWERED"'},
                    }
                ],
            }
        )


json_module = json


class P2BDeepSeekTests(unittest.TestCase):
    def test_omitted_points_version_recovery_keeps_strict_validation(self):
        from tax_rag.domain.answer_contract import validate_answer_payload, AnswerValidationError
        payload = {"status": "ANSWERED", "direct_conclusion": {
            "items": [{"text": "结论", "citation_chunk_ids": ["chunk-1"]}]}}
        restored, recovery = restore_points_version(payload)
        self.assertEqual(recovery["type"], "omitted_points_version")
        self.assertEqual(validate_answer_payload(restored, {"chunk-1"}, None)["presentation_version"], "points-v3")
        self.assertNotIn("presentation_version", payload)
        restored["direct_conclusion"]["items"][0]["citation_chunk_ids"] = ["invented"]
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(restored, {"chunk-1"}, None)
        for direct in [{"text": "旧格式", "citation_chunk_ids": []}, {"items": [], "text": "冲突"}, {"items": "错误类型"}]:
            self.assertIsNone(restore_points_version({"direct_conclusion": direct})[1])
        self.assertIsNone(restore_points_version({**payload, "presentation_version": "unknown"})[1])

    def test_eof_delimiter_recovery_is_narrow_and_does_not_change_fields(self):
        data = {"status":"ANSWERED", "direct_conclusion":{"items":[{"text":"结论", "citation_chunk_ids":["E1"]}]}, "rules":[]}
        text = json.dumps(data, ensure_ascii=False)
        parsed, recovery = parse_answer_json(text[:-1])
        self.assertEqual(parsed, data)
        self.assertEqual(recovery["appended"], "}")
        for invalid in ['{"x":"未完', '{"x":}', '{"x":1,}', '{"x":tru', '{"x":[1}']:
            with self.subTest(invalid=invalid), self.assertRaises(json.JSONDecodeError):
                parse_answer_json(invalid)

    def test_syntax_recovery_does_not_bypass_reference_or_field_validation(self):
        from tax_rag.domain.answer_contract import validate_answer_payload, AnswerValidationError
        raw = '{"status":"ANSWERED","direct_conclusion":{"text":"测试","citation_chunk_ids":["invented"]},"rules":[],"prerequisites":[],"exceptions":[],"uncertainties":[],"calculation_explanation":null,"follow_up_question":null'
        parsed, recovery = parse_answer_json(raw)
        self.assertIsNotNone(recovery)
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(parsed, {"chunk-1"}, None)

    def test_observed_misnested_sibling_fields_are_moved_without_invention(self):
        from tax_rag.domain.answer_contract import validate_answer_payload, AnswerValidationError
        nested = {"items":[{"text":"结论", "citation_chunk_ids":["chunk-1"]}],
                  "prerequisites":[], "essential_explanation":[], "critical_gaps":[], "rules":[],
                  "exceptions":[], "uncertainties":[], "calculation_explanation":None, "follow_up_question":None}
        payload={"presentation_version":"points-v3","status":"ANSWERED","direct_conclusion":nested}
        restored,recovery=restore_answer_field_levels(payload)
        self.assertEqual(recovery['type'],'misnested_answer_fields')
        self.assertEqual(validate_answer_payload(restored,{'chunk-1'},None)['status'],'ANSWERED')
        self.assertIn('rules',nested)
        nested['items'][0]['citation_chunk_ids']=['invented']
        restored,_=restore_answer_field_levels(payload)
        with self.assertRaises(AnswerValidationError):validate_answer_payload(restored,{'chunk-1'},None)
        payload['rules'] = []
        self.assertIsNone(restore_answer_field_levels(payload)[1])

    def test_request_uses_verified_model_and_only_current_context(self) -> None:
        service = DeepSeekService()
        evidence = SearchHit(
            "chunk-1",
            "doc-1",
            "version-1",
            "测试法规",
            "第一条",
            "测试原文",
            {"paragraph_start": 1, "paragraph_end": 1},
            1.0,
            "rrf",
            {"exact": 1},
        )
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-key-not-real",
            "HTTP_PROXY": "http://127.0.0.1:12450",
            "HTTPS_PROXY": "http://127.0.0.1:12450",
        }), patch(
            "tax_rag.services.deepseek_service.httpx.Client", FakeClient
        ):
            result = service.generate("当前问题", [evidence])

        self.assertIs(FakeClient.last_options.get("trust_env"), False)
        request = FakeClient.last_request
        self.assertEqual(request["json"]["model"], "deepseek-flash")
        self.assertEqual(request["json"]["thinking"], {"type": "enabled"})
        self.assertEqual(result.thinking_mode, "enabled")
        self.assertNotIn("test-key-not-real", json.dumps(request["json"], ensure_ascii=False))
        context = json.loads(request["json"]["messages"][1]["content"])
        self.assertEqual(context["question"], "当前问题")
        self.assertEqual(len(context["approved_evidence"]), 1)
        self.assertNotIn("conversation_history", context)
        self.assertEqual(result.answer["status"], "ANSWERED")
        self.assertEqual(len(result.request_context_hash), 64)

    def test_truncated_response_has_specific_error_code(self) -> None:
        service = DeepSeekService()
        evidence = SearchHit(
            "chunk-1",
            "doc-1",
            "version-1",
            "测试法规",
            "第一条",
            "测试原文",
            {"paragraph_start": 1, "paragraph_end": 1},
            1.0,
            "rrf",
            {"exact": 1},
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}), patch(
            "tax_rag.services.deepseek_service.httpx.Client", TruncatedClient
        ):
            with self.assertRaises(DeepSeekError) as captured:
                service.generate("当前问题", [evidence])
        self.assertEqual(captured.exception.code, "TRUNCATED_RESPONSE")

    def test_environment_proxy_use_requires_explicit_configuration(self) -> None:
        config = DeepSeekService().config
        config["deepseek"].pop("trust_env", None)
        self.assertFalse(DeepSeekService(config).trust_env)
        config["deepseek"]["trust_env"] = True
        self.assertTrue(DeepSeekService(config).trust_env)

    def test_generation_mode_is_explicit_and_validated(self) -> None:
        config = DeepSeekService().config
        config["deepseek"].pop("thinking_mode", None)
        self.assertIsNone(DeepSeekService(config).thinking_mode)
        config["deepseek"]["thinking_mode"] = "enabled"
        self.assertEqual(DeepSeekService(config).thinking_mode, "enabled")
        config["deepseek"]["thinking_mode"] = "invalid"
        with self.assertRaises(ValueError):
            DeepSeekService(config)


if __name__ == "__main__":
    unittest.main()
