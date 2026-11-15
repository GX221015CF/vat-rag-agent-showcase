from __future__ import annotations

import json
import unittest

from tax_rag.web.answer_view import build_answer_view


def hit_rows():
    return [
        {
            "rank": 1,
            "chunk_id": "chunk-1",
            "document_title": "中华人民共和国增值税法",
            "anchor": "第二十八条",
        }
    ]


def answer_payload():
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
        "exceptions": [
            {"text": "特殊情形需另行判断。", "citation_chunk_ids": ["chunk-1"]}
        ],
        "uncertainties": ["仅覆盖当前证据范围"],
        "calculation_explanation": None,
        "follow_up_question": None,
    }


def generation(response):
    return {
        "status": "SUCCESS",
        "response_json": json.dumps(response, ensure_ascii=False),
        "model_id": "deepseek-flash",
        "prompt_version": "vat-answer-v2",
        "generation_id": "gen-test",
        "response_id": "response-test",
    }


class P2BAnswerViewTests(unittest.TestCase):
    def test_direct_v2_payload_is_supported_for_existing_records(self) -> None:
        view = build_answer_view(generation(answer_payload()), hit_rows())
        self.assertIsNotNone(view)
        self.assertEqual(view["status_label"], "已回答")
        self.assertEqual(view["direct_conclusion"]["citations"][0]["label"], "证据1")
        self.assertIsNone(view["calculation"])

    def test_points_v3_builds_separate_conclusion_items(self) -> None:
        answer = answer_payload()
        answer["presentation_version"] = "points-v3"
        answer["direct_conclusion"] = {"items": [{"text": "逐项结论", "citation_chunk_ids": ["chunk-1"]}]}
        view = build_answer_view(generation(answer), hit_rows())
        self.assertEqual(view["conclusion_items"][0]["text"], "逐项结论")
        self.assertEqual(view["conclusion_items"][0]["citations"][0]["chunk_id"], "chunk-1")

    def test_server_calculation_snapshot_becomes_amount_tiles(self) -> None:
        response = {
            "schema_version": "answer-record-v1",
            "answer": answer_payload(),
            "calculation_snapshot": {
                "calculation_run_id": "calc-1",
                "calculation_type": "SINGLE_TRANSACTION",
                "formula_version": "vat-decimal-v1",
                "rounding_rule_version": "cny-cent-half-up-v1",
                "outputs": {
                    "currency": "CNY",
                    "net_amount": "1000000.00",
                    "tax_amount": "130000.00",
                    "gross_amount": "1130000.00",
                },
            },
        }
        view = build_answer_view(generation(response), hit_rows())
        self.assertEqual(
            [item["value"] for item in view["calculation"]["items"]],
            ["1000000.00", "130000.00", "1130000.00"],
        )
        self.assertEqual(view["calculation"]["calculation_run_id"], "calc-1")

    def test_missing_retrieval_citation_falls_back_to_plain_text(self) -> None:
        payload = answer_payload()
        payload["rules"][0]["citation_chunk_ids"] = ["not-retrieved"]
        self.assertIsNone(build_answer_view(generation(payload), hit_rows()))

    def test_legacy_v1_exception_shape_falls_back_to_plain_text(self) -> None:
        payload = answer_payload()
        payload["exceptions"] = ["旧版没有逐条例外引用"]
        self.assertIsNone(build_answer_view(generation(payload), hit_rows()))

    def test_failed_generation_is_not_structured(self) -> None:
        row = generation(answer_payload())
        row["status"] = "FAILED"
        self.assertIsNone(build_answer_view(row, hit_rows()))


if __name__ == "__main__":
    unittest.main()
