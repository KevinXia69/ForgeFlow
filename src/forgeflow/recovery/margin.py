"""Exact new-spend arithmetic, separate from inventory-inclusive profit.

Callers establish quantity, price and artifact provenance independently. This
module supplies only the shared definition, never an attestation from a solver.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

MARGIN_BASIS = "new_spend_coverage_v2"


def decimal_amount(value, *, cents=False):
    """Reject missing, nonfinite, negative and unbounded monetary evidence."""
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 100:
        raise ValueError("invalid_margin_amount")
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or number < 0 or abs(number.adjusted()) > 100
                or abs(number.as_tuple().exponent) > 100):
            raise ValueError("invalid_margin_amount")
        if cents:
            with localcontext() as context:
                context.prec = 420
                if number * 100 != (number * 100).to_integral_value():
                    raise ValueError("margin_money_precision_mismatch")
        return number
    except InvalidOperation:
        raise ValueError("invalid_margin_amount") from None


def decimal_text(number):
    if number is None:
        return None
    if number == 0:
        return "0"
    value = format(number, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def margin_ratio(revenue, cost):
    """Return a fraction; no spend and no backed sales has no percentage."""
    revenue, cost = decimal_amount(revenue), decimal_amount(cost)
    if revenue == 0:
        if cost != 0:
            raise ValueError("margin_spend_without_coverage")
        return None
    with localcontext() as context:
        context.prec = 420
        return (revenue - cost) / revenue


def margin_satisfied(cost, revenue, floor):
    """Compare the unrounded inequality, including the valid zero/zero case."""
    try:
        cost, revenue, floor = map(decimal_amount, (cost, revenue, floor))
        if not 0 <= floor < 1:
            return False
        with localcontext() as context:
            context.prec = 420
            return cost <= revenue * (1 - floor)
    except (TypeError, ValueError, InvalidOperation):
        return False
