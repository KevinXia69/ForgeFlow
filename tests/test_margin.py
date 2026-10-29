"""Boundary math for the independently sourced new-spend policy."""
from decimal import Decimal

import pytest

from forgeflow.recovery.margin import decimal_amount, margin_ratio, margin_satisfied


@pytest.mark.parametrize("revenue,cost,ratio,passes", [
    ("0", "0", None, True),
    ("100", "0", Decimal(1), True),
    ("500", "400", Decimal("0.2"), False),
    ("100", "50", Decimal("0.5"), True),
    ("100", "50.0000000001", Decimal("0.499999999999"), False),
])
def test_exact_ratio_and_unrounded_policy(revenue, cost, ratio, passes):
    assert margin_ratio(revenue, cost) == ratio
    assert margin_satisfied(cost, revenue, "0.5") is passes


def test_positive_spend_without_backed_sales_is_invalid():
    assert not margin_satisfied("0.01", "0", "0.2")
    with pytest.raises(ValueError, match="margin_spend_without_coverage"):
        margin_ratio("0", "0.01")


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity", "-1", "1e99999", "1e-99999"])
def test_missing_or_invalid_amount_cannot_look_like_free_supply(value):
    assert not margin_satisfied(value, 100, "0.2")
    assert not margin_satisfied(0, value, "0.2")
    with pytest.raises(ValueError):
        decimal_amount(value)


def test_unit_money_must_match_the_solvers_cent_precision():
    assert decimal_amount("479.610", cents=True) == Decimal("479.61")
    with pytest.raises(ValueError, match="margin_money_precision_mismatch"):
        decimal_amount("479.611", cents=True)
