from __future__ import annotations

import json
import unittest

from tax_rag.config import PROJECT_ROOT
from tax_rag.domain.answer_contract import AnswerValidationError, validate_answer_payload
from tax_rag.domain.calculation import calculate_period_summary, calculate_single_transaction
from tax_rag.domain.scope_router import route_scope


def answer_payload():
    return {
        "status": "ANSWERED",
        "direct_conclusion": {"text": "测试结论", "citation_chunk_ids": ["chunk-1"]},
        "prerequisites": [],
        "rules": [{"claim": "测试规则", "citation_chunk_ids": ["chunk-1"]}],
        "exceptions": [],
        "uncertainties": [],
        "calculation_explanation": None,
        "follow_up_question": None,
    }


class P2BGoldenCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = PROJECT_ROOT / "evals" / "p2b_golden_cases.jsonl"
        cls.cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_combined_golden_case_count_is_about_thirty(self) -> None:
        p1_path = PROJECT_ROOT / "evals" / "golden_cases.jsonl"
        p1_count = len([line for line in p1_path.read_text(encoding="utf-8").splitlines() if line])
        self.assertGreaterEqual(p1_count + len(self.cases), 29)
        self.assertLessEqual(p1_count + len(self.cases), 32)

    def test_all_p2b_golden_cases(self) -> None:
        for case in self.cases:
            with self.subTest(case_id=case["case_id"]):
                if case["case_type"] == "single_calculation":
                    result = calculate_single_transaction(
                        case["amount"], case["amount_type"], case["tax_rate_percent"]
                    )
                    self.assertEqual(result.outputs, {**case["expected"], "currency": "CNY"})
                elif case["case_type"] == "period_calculation":
                    result = calculate_period_summary(
                        case["output_tax"],
                        case["deductible_input_tax"],
                        case["input_tax_transfer_out"],
                        case["prior_credit"],
                    )
                    self.assertEqual(result.outputs, {**case["expected"], "currency": "CNY"})
                elif case["case_type"] == "route":
                    # The original fixture predates the explicitly authorized small-taxpayer expansion.
                    expected = "IN_SCOPE" if case["case_id"] == "P2B-R02" else case["expected_status"]
                    self.assertEqual(route_scope(case["query"]).status, expected)
                else:
                    payload = answer_payload()
                    if case["variant"] == "invented_citation":
                        payload["direct_conclusion"]["citation_chunk_ids"] = ["invented"]
                    elif case["variant"] == "model_calculation_without_local_result":
                        payload["calculation_explanation"] = "模型自行计算"
                    if case["expected_valid"]:
                        validate_answer_payload(payload, {"chunk-1"}, None)
                    else:
                        with self.assertRaises(AnswerValidationError):
                            validate_answer_payload(payload, {"chunk-1"}, None)


if __name__ == "__main__":
    unittest.main()
