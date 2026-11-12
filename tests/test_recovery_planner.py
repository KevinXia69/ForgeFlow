"""Time-phased recovery planning: capacity, stock, lead time, MOQ, alternates."""
from __future__ import annotations

import pytest

from forgeflow.planning.candidates import (
    Candidate,
    candidate_from_plan,
    compare,
    fingerprint,
)
from forgeflow.planning.multi_bom import (
    BomNode,
    ComponentSupply,
    SolverStatus,
    SupplyStatus,
)
from forgeflow.planning.recovery import (
    DemandLine,
    Period,
    ProductPlanInput,
    RecoveryPlanner,
    StockOnHand,
    SupplierOffer,
    Workcenter,
    plan_to_dict,
)

PERIODS = [Period(index, f"w{index + 1}") for index in range(4)]


def _planner(**overrides) -> RecoveryPlanner:
    defaults = dict(
        periods=PERIODS,
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=[DemandLine("SO1", "FG", 10, 2, 1)],
        workcenters=[Workcenter("press", {t: 12.0 for t in range(4)})],
    )
    defaults.update(overrides)
    return RecoveryPlanner(**defaults)


def test_production_is_limited_by_per_period_capacity() -> None:
    plan = _planner(workcenters=[Workcenter("press", {t: 4.0 for t in range(4)})]).solve()
    assert plan.status is SolverStatus.OPTIMAL
    assert plan.unmet["SO1"] == 0
    assert plan.produce["FG"][0] == 4
    assert all(value <= 4 for value in plan.produce["FG"].values())
    assert "workcenter:press:period0" in plan.binding_constraints


def test_due_period_prevents_using_later_capacity() -> None:
    planner = _planner(
        demands=[DemandLine("SO1", "FG", 10, 0, 1)],
        workcenters=[Workcenter("press", {0: 4.0, 1: 50.0, 2: 50.0, 3: 50.0})])
    plan = planner.solve()
    assert plan.produce["FG"].get(0) == 4
    assert plan.unmet["SO1"] == 6


def test_stock_reduces_production_without_double_counting() -> None:
    plan = _planner(stock=[StockOnHand("FG", 6)]).solve()
    assert plan.stock_use["FG"] == {0: 6}
    assert sum(plan.produce["FG"].values()) == 4


def test_supplier_lead_time_and_minimum_order_quantity() -> None:
    planner = _planner(
        workcenters=[Workcenter("press", {t: 0.0 for t in range(4)})],
        offers=[SupplierOffer("sup-a", "FG", 5.0, min_order_quantity=5,
                              lead_periods=2)])
    plan = planner.solve()
    assert plan.status is SolverStatus.OPTIMAL
    bought = plan.buy["FG"]["sup-a"]
    # Nothing can arrive before the lead time.
    assert 0 not in bought and 1 not in bought
    # Whatever is bought respects the supplier minimum.
    assert all(value == 0 or value >= 5 for value in bought.values())
    assert plan.unmet["SO1"] == 0


def test_alternate_workcenters_share_one_capacity_pool() -> None:
    planner = _planner(
        products=[ProductPlanInput("FG", BomNode("FG"), {"wc1": 1.0, "wc2": 1.0})],
        demands=[DemandLine("SO1", "FG", 10, 0, 1)],
        workcenters=[
            Workcenter("wc1", {t: 6.0 for t in range(4)}, alternates=("wc2",)),
        ])
    plan = planner.solve()
    # wc1 and wc2 are one pool of 6 hours in period 0, not two pools of 6.
    assert plan.produce["FG"].get(0) == 6
    assert plan.unmet["SO1"] == 4


def test_unavailable_period_removes_capacity() -> None:
    planner = _planner(
        workcenters=[Workcenter("press", {t: 10.0 for t in range(4)},
                                unavailable_periods=(0,))])
    plan = planner.solve()
    assert 0 not in plan.produce["FG"]
    assert plan.produce["FG"].get(1) == 10


def test_unknown_component_supply_is_an_assumption_not_infinite_stock() -> None:
    planner = _planner(
        products=[ProductPlanInput("FG", BomNode("FG", children=(BomNode("C1", 2),)))],
        component_supply={})  # nobody supplied the fact
    plan = planner.solve()
    assert "component_supply_unknown:C1" in plan.assumptions
    # The plan is still solvable, but it is explicitly not trustworthy.
    assert plan.solved is True and plan.trustworthy is False


def test_explicitly_unlimited_supply_is_not_an_assumption() -> None:
    planner = _planner(
        demands=[DemandLine("SO1", "FG", 10, 2, 1, budget_cap=500.0)],
        products=[ProductPlanInput("FG", BomNode("FG", children=(BomNode("C1", 2),)))],
        component_supply={"C1": ComponentSupply("C1", None, SupplyStatus.UNLIMITED)})
    plan = planner.solve()
    assert plan.assumptions == ()
    assert plan.trustworthy is True


def test_limited_component_supply_caps_production() -> None:
    planner = _planner(
        products=[ProductPlanInput("FG", BomNode("FG", children=(BomNode("C1", 2),)))],
        component_supply={"C1": 12})
    plan = planner.solve()
    assert plan.unmet["SO1"] == 4
    assert "component:C1" in plan.binding_constraints


def test_proven_infeasibility_is_reported_as_infeasible() -> None:
    from forgeflow.planning.rules import BusinessRule, RuleSet

    rule = BusinessRule("no gaps", "mrp.production",
                        {"lte": {"field": "unmet", "value": 0}})
    planner = _planner(
        demands=[DemandLine("SO1", "FG", 100, 0, 1)],
        workcenters=[Workcenter("press", {t: 1.0 for t in range(4)})])
    plan = planner.solve(RuleSet([rule]))
    assert plan.status is SolverStatus.INFEASIBLE
    assert plan.solved is False


def test_solver_timeout_is_never_reported_as_infeasible(monkeypatch) -> None:
    from ortools.sat.python import cp_model

    monkeypatch.setattr(cp_model.CpSolver, "solve", lambda self, model: cp_model.UNKNOWN)
    plan = _planner().solve()
    assert plan.status is SolverStatus.UNKNOWN
    assert plan.solved is False


def test_invalid_input_is_rejected_at_construction() -> None:
    from forgeflow.planning.multi_bom import BomError

    with pytest.raises(BomError, match="demand_product_unknown"):
        _planner(demands=[DemandLine("SO1", "UNKNOWN-PRODUCT", 1, 0)])


def test_invalid_model_is_distinct_from_infeasible() -> None:
    # A malformed supply fact cannot be silently treated as a free resource.
    plan = _planner(
        products=[ProductPlanInput("FG", BomNode("FG", children=(BomNode("C1", 1),)))],
        component_supply={"C1": True}).solve()
    assert plan.status is SolverStatus.MODEL_INVALID
    assert plan.solved is False


def test_plan_serialises_every_decision_it_made() -> None:
    plan = _planner(stock=[StockOnHand("FG", 3)]).solve()
    payload = plan_to_dict(plan)
    assert payload["status"] == "OPTIMAL"
    assert payload["produce"] and payload["stock_use"]
    assert "assumptions" in payload and "binding_constraints" in payload


# ------------------------------------------------------------------ candidates
def _candidate(name: str, hours: float) -> Candidate:
    plan = _planner(
        demands=[DemandLine("SO1", "FG", 10, 2, 1, budget_cap=500.0)],
        workcenters=[Workcenter("press", {t: hours for t in range(4)})]).solve()
    return candidate_from_plan(plan, candidate_id=name, label=f"{hours}h")


def test_equivalent_plans_are_collapsed() -> None:
    comparison = compare([_candidate("a", 12.0), _candidate("b", 12.0)])
    assert comparison.frontier == ("a",)
    assert comparison.equivalent == {"b": "a"}


def test_a_dominated_candidate_is_named_not_offered() -> None:
    generous = _candidate("generous", 20.0)   # meets all 10 units
    tight = _candidate("tight", 2.0)          # 4 periods x 2h = 8 units
    comparison = compare([generous, tight])
    assert "tight" not in comparison.frontier
    assert comparison.dominated["tight"] == "generous"
    assert comparison.best_by_objective["unmet_units"] == "generous"


def test_unsolved_candidates_are_reported_as_a_warning() -> None:
    from forgeflow.planning.recovery import RecoveryPlan

    unsolved = Candidate("broken", "broken", RecoveryPlan(SolverStatus.UNKNOWN, tuple(PERIODS)))
    comparison = compare([_candidate("fine", 12.0), unsolved])
    assert comparison.frontier == ("fine",)
    assert any("unsolved_candidates" in warning for warning in comparison.warnings)


def test_fingerprint_ignores_computation_but_not_actions() -> None:
    first = _planner().solve()
    second = _planner(time_limit_seconds=5.0).solve()
    assert fingerprint(first) == fingerprint(second)
    different = _planner(stock=[StockOnHand("FG", 4)]).solve()
    assert fingerprint(first) != fingerprint(different)


def test_moq_above_explicit_supplier_capacity_is_not_offered_as_supply() -> None:
    planner = _planner(
        demands=[DemandLine("SO1", "FG", 5, 0, 1)],
        workcenters=[Workcenter("press", {t: 0.0 for t in range(4)})],
        offers=[SupplierOffer("sup", "FG", 10.0, min_order_quantity=5,
                              capacity_per_period=4, lead_periods=0)])
    plan = planner.solve()
    assert sum(sum(values.values()) for values in plan.buy.get("FG", {}).values()) == 0
    assert plan.unmet["SO1"] == 5
    assert planner.infeasible_offers[0]["reason"] == "moq_exceeds_supplier_capacity"


def test_explicit_supplier_capacity_caps_a_larger_demand() -> None:
    planner = _planner(
        demands=[DemandLine("SO1", "FG", 10, 0, 1)],
        workcenters=[Workcenter("press", {t: 0.0 for t in range(4)})],
        offers=[SupplierOffer("sup", "FG", 10.0, min_order_quantity=3,
                              capacity_per_period=4, lead_periods=0)])
    plan = planner.solve()
    assert sum(plan.buy["FG"]["sup"].values()) <= 4
    assert plan.unmet["SO1"] >= 6
