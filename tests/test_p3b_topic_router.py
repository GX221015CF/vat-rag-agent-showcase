from __future__ import annotations

import json
import unittest

from tax_rag.config import PROJECT_ROOT
from tax_rag.domain.scope_router import TOPIC_ROUTER_VERSION, route_scope


class P3BTopicRouterTests(unittest.TestCase):
    def test_all_router_acceptance_cases(self) -> None:
        path = PROJECT_ROOT / "evals" / "p3b_router_cases.jsonl"
        cases = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(cases), 10)
        for case in cases:
            with self.subTest(case_id=case["case_id"]):
                decision = route_scope(case["query"])
                # Preserve the historical fixture and make the scope change explicit here.
                expected = "IN_SCOPE" if case["case_id"] == "P3B-R07" else case["expected_status"]
                self.assertEqual(decision.status, expected)
                self.assertEqual(
                    decision.matched_topic_code,
                    case["expected_topic_code"],
                )
                self.assertEqual(decision.router_version, TOPIC_ROUTER_VERSION)

    def test_target_failures_have_auditable_topic_trace(self) -> None:
        welfare = route_scope(
            "厂里把自己生产的一批礼品发给员工过节，要不要按卖出去交税？"
        )
        registration = route_scope(
            "刚拿到营业执照，最晚多久要去税务那边办登记？"
        )

        self.assertEqual(
            welfare.matched_topic_code,
            "VAT_SELF_PRODUCED_STAFF_WELFARE",
        )
        self.assertEqual(len(welfare.matched_terms), 3)
        self.assertIn("方式=专题规则", welfare.reason)
        self.assertEqual(
            registration.matched_topic_code,
            "TAX_REGISTRATION_AFTER_LICENSE_DEADLINE",
        )
        self.assertEqual(len(registration.matched_terms), 4)
        self.assertIn("方式=专题规则", registration.reason)

    def test_explicit_exclusion_precedes_topic_match(self) -> None:
        decision = route_scope(
            "出口货物退税怎么申报？"
        )
        self.assertEqual(decision.status, "OUT_OF_SCOPE")
        self.assertEqual(decision.matched_keyword, "出口")
        self.assertIsNone(decision.matched_topic_code)
        self.assertIn("方式=明确排除词", decision.reason)


if __name__ == "__main__":
    unittest.main()
