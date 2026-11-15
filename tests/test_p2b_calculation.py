from __future__ import annotations

import unittest

from tax_rag.domain.calculation import (
    CalculationInputError,
    calculate_period_summary,
    calculate_single_transaction,
)


class P2BCalculationTests(unittest.TestCase):
    def test_tax_included_price_separation_uses_decimal(self) -> None:
        result = calculate_single_transaction("1130000.00", "TAX_INCLUDED", "13")
        self.assertEqual(result.outputs["net_amount"], "1000000.00")
        self.assertEqual(result.outputs["tax_amount"], "130000.00")
        self.assertEqual(result.outputs["gross_amount"], "1130000.00")
        self.assertTrue(result.intermediates["net_amount_exact"].startswith("1000000"))

    def test_tax_included_rounds_only_formal_output_to_cent(self) -> None:
        result = calculate_single_transaction("100", "TAX_INCLUDED", "13")
        self.assertEqual(result.outputs["net_amount"], "88.50")
        self.assertEqual(result.outputs["tax_amount"], "11.50")
        self.assertGreater(len(result.intermediates["net_amount_exact"]), 10)

    def test_tax_excluded_calculation_reconciles_displayed_total(self) -> None:
        result = calculate_single_transaction("100.005", "TAX_EXCLUDED", "13")
        self.assertEqual(result.outputs["net_amount"], "100.01")
        self.assertEqual(result.outputs["tax_amount"], "13.00")
        self.assertEqual(result.outputs["gross_amount"], "113.01")

    def test_period_summary_outputs_payable(self) -> None:
        result = calculate_period_summary("130", "80", "5", "10")
        self.assertEqual(result.outputs["tax_payable"], "45.00")
        self.assertEqual(result.outputs["ending_credit"], "0.00")

    def test_period_summary_outputs_ending_credit(self) -> None:
        result = calculate_period_summary("80", "100", "0", "0")
        self.assertEqual(result.outputs["tax_payable"], "0.00")
        self.assertEqual(result.outputs["ending_credit"], "20.00")

    def test_invalid_numbers_are_rejected(self) -> None:
        for value in ("", "NaN", "Infinity", "-1", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaises(CalculationInputError):
                    calculate_single_transaction(value, "TAX_INCLUDED", "13")

    def test_tax_rate_above_100_is_rejected(self) -> None:
        with self.assertRaises(CalculationInputError):
            calculate_single_transaction("100", "TAX_INCLUDED", "101")


if __name__ == "__main__":
    unittest.main()
