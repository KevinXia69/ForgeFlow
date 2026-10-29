"""Planning headroom from invented bound records, never actual savings or delivery."""
import copy

import pytest
from test_recovery_change_set import fixture, stock_margin_fixture

from forgeflow.product.capacity import build_capacity_summary
from forgeflow.product.decision_metrics import build_decision_metrics, project_decision_metrics
from forgeflow.product.readiness import build_order_readiness
from forgeflow.recovery.change_set import build_change_set
from forgeflow.recovery.product_costs import build_cost_report
from forgeflow.recovery.storage import digest


def facts(*, unknown=False, inputs=None):
    request, snapshot, blueprint, plan = fixture() if inputs is None else inputs
    snapshot["collected_at_utc"] = "2026-01-01T10:00:00+00:00"
    changes = build_change_set(request, snapshot, blueprint, plan)
    if unknown:
        next(row for row in changes["operations"] if row["kind"] == "purchase_order")["after"]["unit_price"] = None
    keys = {"plan_id": "a" * 64, "snapshot_sha256": digest(snapshot)}
    costs = build_cost_report(request, snapshot, blueprint, changes)
    capacity = build_capacity_summary(request, blueprint, changes, **keys)
    readiness = build_order_readiness(request, blueprint, plan["plan"], changes, **keys)
    return (request, snapshot, blueprint, {}, costs, capacity, readiness), keys


def project(value, inputs, keys):
    return project_decision_metrics(value, costs=inputs[4], capacity=inputs[5], readiness=inputs[6], **keys)


def test_margin_customer_cap_and_horizon_are_separate_constraints():
    inputs, keys = facts()
    before = copy.deepcopy(inputs)
    result = build_decision_metrics(*inputs, **keys)
    assert result["schema_version"] == 2
    assert result["basis"]["margin_basis"] == "new_spend_coverage_v2"
    assert result["margin"] == {"sales_value": "1000", "planned_cost": "70", "original_floor_pct": "20",
        "effective_floor_pct": "20", "planned_margin_pct": "93", "headroom_pp": "73",
        "max_planning_cost": "800", "cost_headroom": "730", "new_spend_revenue": "1000",
        "new_spend_units": 10, "stock_units": 0}
    assert result["order_budgets"] == [{"order_ref": "SO1", "sales_value": "1000", "customer_cap": "2000", "headroom": "1000"}]
    center = result["workcenters"][0]
    assert center["used_standard_minutes"] == "30"
    assert result["readiness"] == {"order_count": 1, "zero_buffer_orders": 0, "late_orders": 0,
        "uncovered_orders": 0, "unknown_ready_orders": 0, "minimum_buffer_days": 3}
    assert inputs == before
    assert project(result, inputs, keys) == result


def test_tightened_floor_reduces_headroom_without_changing_original_contract():
    inputs, keys = facts()
    inputs[3]["minimum_margin_pct"] = 30
    result = build_decision_metrics(*inputs, **keys)
    assert result["margin"]["effective_floor_pct"] == "30"
    assert result["margin"]["original_floor_pct"] == "20"
    assert result["margin"]["cost_headroom"] == "630"
    assert inputs[0]["margin"] == 0.2


def test_stock_changes_margin_denominator_but_not_customer_budget():
    inputs, keys = facts(inputs=stock_margin_fixture(unit_cost=76))
    result = build_decision_metrics(*inputs, **keys)
    assert result["margin"] == {"sales_value": "1000", "new_spend_revenue": "500",
        "new_spend_units": 5, "stock_units": 5, "planned_cost": "400", "original_floor_pct": "20",
        "effective_floor_pct": "20", "planned_margin_pct": "20", "headroom_pp": "0",
        "max_planning_cost": "400", "cost_headroom": "0"}
    assert result["order_budgets"][0]["sales_value"] == "1000"
    assert project(result, inputs, keys) == result


def test_component_stock_cost_does_not_enter_new_spend_even_without_finished_stock():
    inputs, keys = facts(inputs=stock_margin_fixture(stock=0, raw_stock=20))
    result = build_decision_metrics(*inputs, **keys)
    assert result["margin"]["new_spend_revenue"] == result["margin"]["sales_value"] == "1000"
    assert result["margin"]["planned_cost"] == "30"
    assert result["margin"]["planned_margin_pct"] == "97"
    assert result["margin"]["new_spend_units"] == 10


@pytest.mark.parametrize("stock,expected", [(10, None), (5, "100")])
def test_zero_cost_margin_uses_explicit_coverage(stock, expected):
    inputs, keys = facts(inputs=stock_margin_fixture(stock=stock, unit_cost=0, raw_price=0))
    result = build_decision_metrics(*inputs, **keys)
    assert result["margin"]["planned_margin_pct"] == expected
    assert result["margin"]["headroom_pp"] == (None if expected is None else "80")
    assert result["margin"]["max_planning_cost"] == ("0" if stock == 10 else "400")
    assert result["margin"]["cost_headroom"] == ("0" if stock == 10 else "400")
    assert project(result, inputs, keys) == result


def test_historical_v1_is_read_without_relabelling_or_recomputing():
    inputs, keys = facts(inputs=stock_margin_fixture(unit_cost=76))
    legacy = build_decision_metrics(*inputs, **keys)
    legacy["schema_version"] = 1
    legacy["basis"].pop("margin_basis")
    legacy["margin"] = {"sales_value": "1000", "planned_cost": "400", "original_floor_pct": "20",
        "effective_floor_pct": "20", "planned_margin_pct": "60", "headroom_pp": "40",
        "max_planning_cost": "800", "cost_headroom": "400"}
    original = copy.deepcopy(legacy)
    assert project(legacy, inputs, keys) == original and legacy == original
    legacy["schema_version"] = 2
    with pytest.raises(ValueError, match="invalid_decision_metrics"):
        project(legacy, inputs, keys)


@pytest.mark.parametrize("field,value", [("new_spend_revenue", "1000"), ("new_spend_units", 10),
                                        ("stock_units", False), ("planned_margin_pct", "60")])
def test_corrected_projection_rejects_changed_coverage_or_legacy_ratio(field, value):
    inputs, keys = facts(inputs=stock_margin_fixture(unit_cost=76))
    result = build_decision_metrics(*inputs, **keys)
    result["margin"][field] = value
    with pytest.raises(ValueError, match="invalid_decision_metrics"):
        project(result, inputs, keys)


def test_unknown_cost_or_capacity_does_not_become_zero_headroom():
    inputs, keys = facts(unknown=True)
    for center in inputs[2]["workcenters"]:
        center.pop("capacity_minutes", None)
    result = build_decision_metrics(*inputs, **keys)
    assert result["margin"]["planned_cost"] is None
    assert result["margin"]["headroom_pp"] is result["margin"]["cost_headroom"] is None
    assert all(row["headroom_minutes"] is None for row in result["workcenters"])
    assert project(None, inputs, keys) is None


@pytest.mark.parametrize("fault", ["plan", "snapshot", "cost", "margin", "customer", "capacity", "readiness", "private"])
def test_projection_refuses_rebinding_changed_arithmetic_or_extra_fields(fault):
    inputs, keys = facts()
    value = build_decision_metrics(*inputs, **keys)
    if fault in {"plan", "snapshot"}:
        value["basis"]["plan_id" if fault == "plan" else "snapshot_sha256"] = "c" * 64
    elif fault == "cost":
        value["margin"]["planned_cost"] = "0"
    elif fault == "margin":
        value["margin"]["headroom_pp"] = "100"
    elif fault == "customer":
        value["order_budgets"][0]["headroom"] = "2000"
    elif fault == "capacity":
        value["workcenters"][0]["headroom_minutes"] = "0"
    elif fault == "readiness":
        value["readiness"]["zero_buffer_orders"] = True
    else:
        value["private_path"] = "CANARY"
    with pytest.raises(ValueError, match="invalid_decision_metrics"):
        project(value, inputs, keys)


def test_builder_refuses_changed_snapshot_or_ambiguous_sales_lines():
    inputs, keys = facts()
    inputs[1]["models"]["sale.order.line"].append(copy.deepcopy(inputs[1]["models"]["sale.order.line"][0]))
    with pytest.raises(ValueError, match="invalid_decision_metrics"):
        build_decision_metrics(*inputs, **keys)

    keys["snapshot_sha256"] = digest(inputs[1])
    with pytest.raises(ValueError, match="invalid_decision_metrics"):
        build_decision_metrics(*inputs, **keys)


@pytest.mark.parametrize("question", ["Compare candidates, per-order readiness, costs, headroom and why selected?",
                                     "比较候选的成本、逐单齐货和余量，为什么选择？"])
def test_mixed_questions_keep_cost_dates_headroom_and_bound_human_reason(question):
    from forgeflow.api.case_qa import answer_case

    inputs, keys = facts()
    plan = {**keys, "objective": "min_planned_cost", "costs": inputs[4], "capacity": inputs[5],
            "order_readiness": inputs[6], "decision_metrics": build_decision_metrics(*inputs, **keys),
            "actions": [], "margin": 93, "on_time": True}
    case = {"state": "awaiting_approval", "plan": plan, "decision": {},
        "comparison": {"status": "ready", "plans": [plan], "version": 1,
                       "selected_plan_id": plan["plan_id"], "attempts": []},
        "approval": {"plan_id": plan["plan_id"], "comment": "Accepted this recorded trade-off"}}
    answer = answer_case(question, case)
    assert "70.00" in answer["answer"] and "730.00" in answer["answer"]
    assert "2026-01-06" in answer["answer"] and "2026-01-09" in answer["answer"]
    assert "Accepted this recorded trade-off" in answer["answer"]
    fields = {row["field"] for row in answer["citations"]}
    assert {"comparison.plans.0.costs.planned.total", "approval.comment",
            "comparison.plans.0.decision_metrics.margin.cost_headroom",
            "plan.order_readiness.orders.0.planned_ready_date"} <= fields
    case["approval"]["plan_id"] = "c" * 64
    assert "Accepted this recorded trade-off" not in answer_case(question, case)["answer"]
    case["decision"]["selection_pending"] = True
    pending = answer_case(question, case)
    assert "730.00" not in pending["answer"] and "2026-01-06" not in pending["answer"]


@pytest.mark.parametrize("value", ["1e999999999", "1e-999999999", "NaN", "Infinity", True])
def test_unbounded_or_nonfinite_amounts_are_rejected(value):
    inputs, keys = facts()
    result = build_decision_metrics(*inputs, **keys)
    result["order_budgets"][0]["customer_cap"] = value
    with pytest.raises(ValueError, match="invalid_decision_metrics"):
        project(result, inputs, keys)


@pytest.mark.parametrize("question", ["What is each customer's budget headroom?", "每单客户预算余量是多少？"])
def test_customer_budget_answer_is_sales_value_not_purchase_authority(question):
    from forgeflow.api.case_qa import answer_case

    inputs, keys = facts()
    plan = {**keys, "costs": inputs[4], "capacity": inputs[5], "order_readiness": inputs[6],
            "decision_metrics": build_decision_metrics(*inputs, **keys), "actions": [], "margin": 93, "on_time": True}
    case = {"state": "awaiting_approval", "plan": plan, "decision": {},
            "order": {"ref": "SO1", "units": 10, "product": "FIN", "due_days": 8}, "disruption": {}}
    answer = answer_case(question, case)
    assert "USD 1000" in answer["answer"] and "USD 2000" in answer["answer"]
    fields = {row["field"]: row["value"] for row in answer["citations"]}
    assert fields["plan.decision_metrics.order_budgets.0.headroom"] == "1000"
    assert fields["plan.decision_metrics.order_budgets.0.sales_value"] == "1000"
    assert "purchase authorization" in answer["answer"] or "不是采购授权" in answer["answer"]
    assert "2026-01" not in answer["answer"] and "730" not in answer["answer"]
