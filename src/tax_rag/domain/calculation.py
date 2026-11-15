from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any


MONEY_QUANTUM = Decimal("0.01")
HUNDRED = Decimal("100")
FORMULA_VERSION = "vat-decimal-v1"
ROUNDING_RULE_VERSION = "cny-cent-half-up-v1"


class CalculationInputError(ValueError):
    pass


def parse_nonnegative_decimal(value: str, field_name: str) -> Decimal:
    text = value.strip()
    if not text or len(text) > 50:
        raise CalculationInputError(f"{field_name}不是有效金额")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise CalculationInputError(f"{field_name}不是有效数字") from exc
    if not number.is_finite() or number < 0:
        raise CalculationInputError(f"{field_name}必须是非负有限数字")
    if number.adjusted() > 15:
        raise CalculationInputError(f"{field_name}超过当前演示允许的金额范围")
    return number


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class CalculationResult:
    calculation_type: str
    formula_version: str
    rounding_rule_version: str
    inputs: dict[str, str]
    intermediates: dict[str, str]
    outputs: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_single_transaction(
    amount_text: str,
    amount_type: str,
    tax_rate_percent_text: str,
) -> CalculationResult:
    amount = parse_nonnegative_decimal(amount_text, "金额")
    rate_percent = parse_nonnegative_decimal(tax_rate_percent_text, "税率")
    if rate_percent > HUNDRED:
        raise CalculationInputError("税率百分比不能超过100")
    if amount_type not in {"TAX_INCLUDED", "TAX_EXCLUDED"}:
        raise CalculationInputError("金额类型无效")

    rate = rate_percent / HUNDRED
    with localcontext() as context:
        context.prec = 50
        if amount_type == "TAX_INCLUDED":
            gross_exact = amount
            net_exact = gross_exact / (Decimal("1") + rate)
            tax_exact = gross_exact - net_exact
            gross_output = money(gross_exact)
            net_output = money(net_exact)
            tax_output = gross_output - net_output
            formula = "不含税销售额=含税金额÷(1+税率)；税额=舍入后含税金额-舍入后不含税销售额"
        else:
            net_exact = amount
            tax_exact = net_exact * rate
            gross_exact = net_exact + tax_exact
            net_output = money(net_exact)
            tax_output = money(tax_exact)
            gross_output = net_output + tax_output
            formula = "税额=不含税销售额×税率；含税金额=舍入后不含税销售额+舍入后税额"

    return CalculationResult(
        calculation_type="SINGLE_TRANSACTION",
        formula_version=FORMULA_VERSION,
        rounding_rule_version=ROUNDING_RULE_VERSION,
        inputs={
            "amount": decimal_text(amount),
            "amount_type": amount_type,
            "tax_rate_percent": decimal_text(rate_percent),
            "tax_rate_fraction": decimal_text(rate),
        },
        intermediates={
            "formula": formula,
            "net_amount_exact": decimal_text(net_exact),
            "tax_amount_exact": decimal_text(tax_exact),
            "gross_amount_exact": decimal_text(gross_exact),
        },
        outputs={
            "net_amount": decimal_text(net_output),
            "tax_amount": decimal_text(tax_output),
            "gross_amount": decimal_text(gross_output),
            "currency": "CNY",
        },
    )


def calculate_period_summary(
    output_tax_text: str,
    deductible_input_tax_text: str,
    input_tax_transfer_out_text: str,
    prior_credit_text: str,
) -> CalculationResult:
    original_values = {
        "output_tax": parse_nonnegative_decimal(output_tax_text, "销项税额"),
        "deductible_input_tax": parse_nonnegative_decimal(
            deductible_input_tax_text, "允许抵扣进项税额"
        ),
        "input_tax_transfer_out": parse_nonnegative_decimal(
            input_tax_transfer_out_text, "进项税额转出"
        ),
        "prior_credit": parse_nonnegative_decimal(prior_credit_text, "上期留抵"),
    }
    normalized = {name: money(value) for name, value in original_values.items()}
    net_exact = (
        normalized["output_tax"]
        - normalized["deductible_input_tax"]
        + normalized["input_tax_transfer_out"]
        - normalized["prior_credit"]
    )
    if net_exact >= 0:
        tax_payable = money(net_exact)
        ending_credit = Decimal("0.00")
    else:
        tax_payable = Decimal("0.00")
        ending_credit = money(-net_exact)

    return CalculationResult(
        calculation_type="PERIOD_SUMMARY",
        formula_version=FORMULA_VERSION,
        rounding_rule_version=ROUNDING_RULE_VERSION,
        inputs={name: decimal_text(value) for name, value in original_values.items()},
        intermediates={
            "formula": "本期净额=销项税额-允许抵扣进项税额+进项税额转出-上期留抵",
            **{
                f"{name}_normalized": decimal_text(value)
                for name, value in normalized.items()
            },
            "net_tax_exact": decimal_text(net_exact),
        },
        outputs={
            "tax_payable": decimal_text(tax_payable),
            "ending_credit": decimal_text(ending_credit),
            "currency": "CNY",
        },
    )
