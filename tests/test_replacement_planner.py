"""A replacement plan is a solved decision, not a hand-written action list."""
from __future__ import annotations

from datetime import date

import pytest

from forgeflow.planning.actions import ActionCompileError
from forgeflow.planning.replacement import (
    OpenCommitment,
    PurchaseOffer,
    ReplacementDemand,
    ReplacementError,
    ReplacementPlanner,
    periods_until,
    plan_to_dict,
    replacement_actions,
)

TODAY = date(2026, 11, 8)
ORIGIN = ({"model": "purchase.order", "id": 228},)


def _periods(days: int = 2):
    return periods_until(TODAY, date.fromordinal(TODAY.toordinal() + days))


def _demand(quantity: int = 20, due: int = 2):
    return ReplacementDemand(119, "FFV3-SHARED-RM", quantity, due, ORIGIN)


def _solve(**kwargs):
    values = {
        "periods": _periods(),
        "demand": _demand(),
        "stock": 0,
        "commitments": [],
        "offers": [PurchaseOffer(117, "partner:117", 5.0)],
    }
    values.update(kwargs)
    return ReplacementPlanner(**values).solve()


def test_cheapest_offer_that_meets_the_due_date_wins() -> None:
    plan = _solve(offers=[
        PurchaseOffer(117, "partner:117", 5.0),
        PurchaseOffer(130, "partner:130", 3.0, lead_periods=10),
    ])
    assert plan.solved and plan.executable
    assert plan.buy_by_supplier() == {"partner:117": 20}
    assert plan.cost == pytest.approx(100.0)
    assert plan.unmet == 0
    assert any(item["reason"] == "lead_time_misses_due_date"
               for item in plan.infeasible_offers)


def test_stock_and_open_commitments_reduce_the_purchase() -> None:
    plan = _solve(stock=4, commitments=[
        OpenCommitment(line_id=227, partner_id=117, quantity=6, available_period=1)])
    assert plan.stock_use == 4
    assert plan.committed_use == 6
    assert plan.buy_by_supplier() == {"partner:117": 10}
    assert plan.cost == pytest.approx(50.0)
    assert "stock" in plan.binding_constraints


def test_a_late_commitment_is_not_counted_as_supply() -> None:
    plan = _solve(commitments=[
        OpenCommitment(line_id=227, partner_id=117, quantity=6, available_period=9)])
    assert plan.committed_use == 0
    assert plan.buy_by_supplier() == {"partner:117": 20}


def test_minimum_order_quantity_is_respected() -> None:
    plan = _solve(demand=_demand(quantity=4), offers=[
        PurchaseOffer(117, "partner:117", 7.0, min_order_quantity=5)])
    assert plan.buy_by_supplier() == {"partner:117": 5}
    assert plan.unmet == 0
    assert any(item.startswith("min_order_quantity")
               for item in plan.binding_constraints)


def test_capacity_is_split_across_periods() -> None:
    plan = _solve(offers=[
        PurchaseOffer(117, "partner:117", 5.0, capacity_per_period=12)])
    assert plan.buy_by_supplier() == {"partner:117": 20}
    assert sorted(plan.buy["partner:117"]) == [1, 2]
    assert any(item.startswith("supplier_capacity")
               for item in plan.binding_constraints)


def test_a_shortfall_is_reported_and_never_compiled() -> None:
    plan = _solve(offers=[])
    assert plan.solved
    assert plan.unmet == 20
    assert not plan.executable
    assert "unmet:FFV3-SHARED-RM" in plan.binding_constraints
    with pytest.raises(ActionCompileError) as raised:
        replacement_actions(plan, origin_ref="purchase.order:228")
    assert raised.value.code == "plan_has_unmet_demand"


def test_actions_carry_the_lost_commitment_as_their_origin() -> None:
    compiled = replacement_actions(_solve(), origin_ref="purchase.order:228")
    assert len(compiled["actions"]) == 1
    action = compiled["actions"][0]
    assert action["type"] == "create_purchase_order"
    assert action["quantity"] == 20
    assert action["params"]["partner_id"] == 117
    trace = compiled["action_trace"][action["action_id"]]
    assert trace["origin_refs"] == [{"model": "purchase.order", "id": 228}]


def test_compilation_is_deterministic() -> None:
    first = replacement_actions(_solve(), origin_ref="purchase.order:228")
    second = replacement_actions(_solve(), origin_ref="purchase.order:228")
    assert first["actions"] == second["actions"]


def test_the_plan_document_is_json_ready() -> None:
    document = plan_to_dict(_solve())
    assert document["required_quantity"] == 20
    assert document["buy"] == {"partner:117": {"2": 20}}
    assert document["executable"] is True


def test_invalid_inputs_are_refused_with_a_code() -> None:
    with pytest.raises(ReplacementError):
        ReplacementDemand(0, "RM", 1, 0)
    with pytest.raises(ReplacementError):
        PurchaseOffer(1, "s", -1.0)
    with pytest.raises(ReplacementError):
        ReplacementPlanner(periods=_periods(1), demand=_demand(due=5))


def test_the_solver_is_deterministic() -> None:
    values = [plan_to_dict(_solve()) for _ in range(3)]
    assert values[0] == values[1] == values[2]
