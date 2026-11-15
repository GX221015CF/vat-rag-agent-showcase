from __future__ import annotations

import unittest

from tax_rag.domain.answer_contract import AnswerValidationError, render_answer, validate_answer_payload
from tax_rag.models import SearchHit


def hit() -> SearchHit:
    return SearchHit(
        "chunk-1",
        "doc-1",
        "version-1",
        "中华人民共和国增值税法",
        "第二十八条",
        "先开具发票的，为开具发票的当日。",
        {"paragraph_start": 1, "paragraph_end": 1},
        1.0,
        "rrf",
        {"exact": 1},
    )


def valid_payload():
    return {
        "status": "ANSWERED",
        "direct_conclusion": {
            "text": "先开具发票的，纳税义务发生时间为开具发票当日。",
            "citation_chunk_ids": ["chunk-1"],
        },
        "prerequisites": ["属于增值税应税交易"],
        "rules": [
            {
                "claim": "先开具发票的，以开具发票当日确定纳税义务发生时间。",
                "citation_chunk_ids": ["chunk-1"],
            }
        ],
        "exceptions": [],
        "uncertainties": [],
        "calculation_explanation": None,
        "follow_up_question": None,
    }


class P2BAnswerContractTests(unittest.TestCase):
    def test_omitted_optional_arrays_default_empty_but_wrong_types_still_fail(self):
        payload = valid_payload()
        payload.pop('prerequisites')
        payload.pop('rules')
        answer = validate_answer_payload(payload, {'chunk-1'}, None)
        self.assertEqual(answer['prerequisites'], [])
        self.assertEqual(answer['rules'], [])
        payload['prerequisites'] = [{'text':'条件'}]
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {'chunk-1'}, None)

    def test_valid_answer_is_rendered_with_evidence_number(self) -> None:
        validated = validate_answer_payload(valid_payload(), {"chunk-1"}, None)
        text = render_answer(validated, [hit()])
        self.assertIn("直接结论", text)
        self.assertIn("[证据1]", text)
        self.assertNotIn("不替代正式申报判断", text)

    def test_points_v3_validates_each_conclusion_citation_and_renders_items(self) -> None:
        from tax_rag.services.deepseek_service import resolve_citation_ids
        payload = valid_payload()
        payload["presentation_version"] = "points-v3"
        payload["essential_explanation"] = []
        payload["critical_gaps"] = []
        payload["direct_conclusion"] = {"items": [
            {"text": "判断甲", "citation_chunk_ids": ["E1"]},
            {"text": "判断乙", "citation_chunk_ids": ["chunk-1"]},
        ]}
        validated = validate_answer_payload(resolve_citation_ids(payload, [hit()]), {"chunk-1"}, None)
        self.assertEqual([item["text"] for item in validated["direct_conclusion"]["items"]], ["判断甲", "判断乙"])
        self.assertIn("- 判断甲[证据1]", render_answer(validated, [hit()]))
        self.assertIn("- 判断乙[证据1]", render_answer(validated, [hit()]))

    def test_points_v3_rejects_unbound_or_multiline_conclusions(self) -> None:
        payload = valid_payload()
        payload["presentation_version"] = "points-v3"
        payload["direct_conclusion"] = {"items": [{"text": "無引用结论", "citation_chunk_ids": []}]}
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {"chunk-1"}, None)
        payload["direct_conclusion"]["items"][0] = {"text": "第一行\n第二行", "citation_chunk_ids": ["chunk-1"]}
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {"chunk-1"}, None)

    def test_invented_citation_is_rejected(self) -> None:
        payload = valid_payload()
        payload["direct_conclusion"]["citation_chunk_ids"] = ["invented"]
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {"chunk-1"}, None)

    def test_answered_with_cited_conclusion_allows_empty_rules(self) -> None:
        payload = valid_payload()
        payload["rules"] = []
        result = validate_answer_payload(payload, {"chunk-1"}, None)
        self.assertEqual(result["status"], "ANSWERED")
        self.assertEqual(result["rules"], [])
        payload["direct_conclusion"]["citation_chunk_ids"] = []
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {"chunk-1"}, None)

    def test_direct_deduction_denial_is_preserved_without_word_filtering(self) -> None:
        payload = valid_payload()
        payload["presentation_version"] = "points-v3"
        text = "不得抵扣。一般纳税人购入货物专用于集体福利，适用增值税法第二十二条第四项。"
        payload["direct_conclusion"] = {"items": [{"text": text, "citation_chunk_ids": ["chunk-1"]}]}
        payload["rules"] = []
        answer = validate_answer_payload(payload, {"chunk-1"}, None)
        self.assertEqual(answer["direct_conclusion"]["items"][0]["text"], text)
        self.assertIn(text, render_answer(answer, [hit()]))

    def test_model_cannot_explain_nonexistent_local_calculation(self) -> None:
        payload = valid_payload()
        payload["calculation_explanation"] = "模型自行计算了金额"
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {"chunk-1"}, None)

    def test_exception_without_citation_is_rejected(self) -> None:
        payload = valid_payload()
        payload["exceptions"] = [{"text": "一条例外", "citation_chunk_ids": []}]
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {"chunk-1"}, None)

    def test_calculation_explanation_must_match_all_decimal_outputs(self) -> None:
        payload = valid_payload()
        payload["calculation_explanation"] = "不含税金额1000000.00，税额130000.00"
        calculation = {
            "outputs": {
                "net_amount": "1000000.00",
                "tax_amount": "130000.00",
                "gross_amount": "1130000.00",
                "currency": "CNY",
            }
        }
        with self.assertRaises(AnswerValidationError):
            validate_answer_payload(payload, {"chunk-1"}, calculation)


if __name__ == "__main__":
    unittest.main()
