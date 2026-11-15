from __future__ import annotations

import json
from typing import Any

from tax_rag.domain.calculation import (
    calculate_period_summary,
    calculate_single_transaction,
)
from tax_rag.domain.matter_state import MatterStateError, MatterStatus
from tax_rag.storage.runtime_store import RuntimeStore


class CalculationService:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def _require_active_matter(self, matter_id: str) -> None:
        matter = self.store.get_matter(matter_id)
        if matter["status"] != MatterStatus.ACTIVE:
            raise MatterStateError("只有进行中的事项可以新增计算")

    def single(
        self,
        matter_id: str,
        amount: str,
        amount_type: str,
        tax_rate_percent: str,
    ) -> str:
        self._require_active_matter(matter_id)
        result = calculate_single_transaction(amount, amount_type, tax_rate_percent)
        return self.store.record_calculation(matter_id, result)

    def period(
        self,
        matter_id: str,
        output_tax: str,
        deductible_input_tax: str,
        input_tax_transfer_out: str,
        prior_credit: str,
    ) -> str:
        self._require_active_matter(matter_id)
        result = calculate_period_summary(
            output_tax,
            deductible_input_tax,
            input_tax_transfer_out,
            prior_credit,
        )
        return self.store.record_calculation(matter_id, result)


def calculation_row_to_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "calculation_run_id": row["calculation_run_id"],
        "calculation_type": row["calculation_type"],
        "formula_version": row["formula_version"],
        "rounding_rule_version": row["rounding_rule_version"],
        "inputs": json.loads(row["input_json"]),
        "intermediates": json.loads(row["intermediate_json"]),
        "outputs": json.loads(row["output_json"]),
        "result_hash": row["result_hash"],
        "created_at": row["created_at"],
    }
