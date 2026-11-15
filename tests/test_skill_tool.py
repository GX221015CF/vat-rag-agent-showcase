from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from tax_rag.cli.skill_tool import execute, period_status
from datetime import date


class SkillToolTests(unittest.TestCase):
    def test_explicit_confirmation_and_input_are_required(self):
        self.assertEqual(execute({"action": {}})["error"]["code"], "INPUT_ERROR")
        self.assertEqual(execute({"action": "single", "amount": "113000"})["error"]["code"], "INPUT_ERROR")
        self.assertEqual(execute({"action": "single", "confirmed": True, "amount": True})["error"]["code"], "INPUT_ERROR")

    def test_decimal_calculation_and_changed_amount(self):
        for gross, net, tax in [("113000", "100000.00", "13000.00"), ("226000", "200000.00", "26000.00")]:
            result = execute({"action": "single", "amount": gross, "amount_type": "TAX_INCLUDED", "tax_rate_percent": "13", "confirmed": True})
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["outputs"]["net_amount"], net)
            self.assertEqual(result["data"]["outputs"]["tax_amount"], tax)

    def test_period_error_and_scope_are_typed(self):
        result = execute({"action": "period", "output_tax": "130000", "deductible_input_tax": "80000", "input_tax_transfer_out": "0", "prior_credit": "10000", "confirmed": True})
        self.assertEqual(result["data"]["outputs"]["tax_payable"], "40000.00")
        self.assertEqual(execute({"action": "search", "query": "政策", "scope": {"method": "simplified"}})["error"]["code"], "OUT_OF_SCOPE")

    def test_original_concept_query_does_not_use_legacy_scope_gate(self):
        with patch("tax_rag.cli.skill_tool.service_for", side_effect=ToolFailure()) as factory:
            result = execute({"action": "search", "query": "视同销售的行为有哪几种？"})
        factory.assert_called_once()
        self.assertEqual(result["error"]["code"], "DEPENDENCY_UNAVAILABLE")

    def test_historical_and_transition_period(self):
        self.assertEqual(execute({"action": "search", "query": "视同销售", "as_of": "2025-08-01"})["error"]["code"], "EVIDENCE_PERIOD_UNSUPPORTED")
        source = {"effective_from": "2026-09-01", "conditional_retroactive_from": "2026-01-01"}
        self.assertEqual(period_status(source, date(2026, 8, 1), None), "requires_unresolved_status")
        self.assertEqual(period_status(source, date(2026, 8, 1), True), "conditional_unresolved_matters")
        self.assertEqual(period_status(source, date(2026, 8, 1), False), "outside_effective_period")

    def test_failed_retrieval_closes_temporary_resources(self):
        with patch("tax_rag.cli.skill_tool.service_for") as factory:
            service = factory.return_value
            service.search_with_trace.side_effect = httpx.ConnectError("offline")
            result = execute({"action": "search", "query": "那外购的呢？", "context": "员工福利"})
            service.close.assert_called_once()
        self.assertEqual(result["error"]["code"], "DEPENDENCY_UNAVAILABLE")


class ToolFailure(RuntimeError):
    pass
