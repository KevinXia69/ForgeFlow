"""Versioned capacity candidates over invented ERP and the pinned real solver."""
from __future__ import annotations

import copy
import json

import pytest
import test_product_candidate_cases as case_tests
import test_product_candidate_local as local_tests
import test_product_candidates as candidate_tests

from forgeflow.planning.objectives import (
    CAPACITY_CANDIDATE_PROFILE,
    CAPACITY_PRODUCT_OBJECTIVES,
    PRODUCT_OBJECTIVES,
    bound_objective,
    canonical_objectives,
    profile_objectives,
)
from forgeflow.product.candidates import project_comparison
from forgeflow.product.capacity import build_capacity_summary, project_capacity
from forgeflow.product.executor import ProductExecutionError
from forgeflow.product.service import ProductCaseService
from forgeflow.recovery.product_constraints import constrained_blueprint

system = candidate_tests.system
serve = candidate_tests.serve
workspace = candidate_tests.workspace
handlers = candidate_tests.handlers
wired_candidates = local_tests.wired_candidates
case_service = case_tests.case_service


def test_capacity_case_approval_replay_and_answers_keep_one_selected_plan(case_service):
    from forgeflow.api.case_context import case_context
    from forgeflow.api.case_qa import answer_case
    from forgeflow.product import ProductCaseError

    cases, case_id, executor, system, calls, _, root = case_service
    executor.candidate_objectives = CAPACITY_PRODUCT_OBJECTIVES
    prepared = cases.prepare(case_id, local_tests.ACTOR)
    comparison = prepared["comparison"]
    capacity = comparison["plans"][2]
    readiness = capacity["order_readiness"]
    metrics = capacity["decision_metrics"]
    impact = capacity["impact_chain"]
    assert impact["basis"]["plan_id"] == capacity["plan_id"]
    assert [row["readiness"] for row in impact["orders"]] == readiness["orders"]
    assert metrics["basis"]["plan_id"] == capacity["plan_id"]
    assert metrics["margin"]["planned_cost"] == "90"
    assert readiness["basis"]["plan_id"] == capacity["plan_id"]
    assert sum(row["covered_units"] for row in readiness["orders"]) == 10
    assert all(row["status"] == "on_time" for row in readiness["orders"])
    for question in ("比较候选方案", "Compare the candidates"):
        reply = answer_case(question, prepared)
        citations = {row["field"]: row["value"] for row in reply["citations"]}
        assert citations["comparison.plans.2.capacity.standard_minutes"] == 10
        assert citations["comparison.plans.2.costs.planned.total"] == "90.0"
        assert "10.00" in reply["answer"] and "90.00" in reply["answer"]
    context = json.loads(case_context(prepared))
    assert len(context["comparison"]["plans"]) == 3
    assert context["comparison"]["plans"][2]["capacity"]["standard_minutes"] == 10
    selected = case_tests.choose(cases, case_id, prepared, index=2)
    selected_context = case_context(selected)
    assert len(selected_context) <= 6000
    assert len(json.loads(selected_context)["comparison"]["plans"]) == 3
    assert selected["plan"]["plan_id"] == capacity["plan_id"]
    assert ProductCaseService(cases.root, executor).get(case_id, local_tests.ACTOR) == selected
    with pytest.raises(ProductCaseError):
        cases.approve(case_id, local_tests.ACTOR, comparison["plans"][0]["plan_id"])
    assert not system[0].writes and len(calls) == 3
    root.product_verifier = lambda: {"status": "passed", "source": "synthetic_executor",
        "strict_success": True, "evaluation_valid": True, "evaluation_scope": "product_original_verifier"}
    complete = cases.approve(case_id, local_tests.ACTOR, capacity["plan_id"], comment="保留产能，接受较高规划成本")
    assert complete["state"] == "succeeded" and complete["plan"]["plan_id"] == capacity["plan_id"]
    assert complete["approval"]["plan_id"] == capacity["plan_id"]
    assert complete["plan"]["order_readiness"] == readiness
    assert complete["plan"]["decision_metrics"] == metrics
    assert complete["plan"]["impact_chain"] == impact
    reason_answer = answer_case("为什么选择这个候选，余量如何？", complete)
    assert "保留产能，接受较高规划成本" in reason_answer["answer"]
    assert any(row["field"] == "approval.comment" for row in reason_answer["citations"])
    writes = list(system[0].writes)
    assert writes
    assert cases.approve(case_id, local_tests.ACTOR, capacity["plan_id"]) == complete
    assert ProductCaseService(cases.root, executor).get(case_id, local_tests.ACTOR) == complete
    assert system[0].writes == writes and len(calls) == 3


@pytest.fixture
def candidates(system, monkeypatch, tmp_path):
    original = candidate_tests.two_route_facts

    def three_routes():
        request, snapshot = original()
        centers = snapshot["models"]["mrp.workcenter"]
        center = copy.deepcopy(centers[1])
        center.update(id=4, name="WC-D", code="WC-D")
        center["note"] = center["note"].replace('"unit_cost": 3', '"unit_cost": 5').replace(
            '"time_per_unit_minutes": 3', '"time_per_unit_minutes": 1')
        centers.append(center)
        centers[0]["alternative_workcenter_ids"] = [2, 3, 4]
        return request, snapshot

    monkeypatch.setattr(candidate_tests, "two_route_facts", three_routes)
    result = candidate_tests.candidates.__wrapped__(system, monkeypatch, tmp_path)
    request, snapshot = three_routes()
    result[2].update(candidate_tests.pinned_plans(system[2]["bundle"],
        constrained_blueprint(snapshot, request), tmp_path, objectives=("preserve_capacity",)))
    return result


def enable_capacity(wired):
    executor, case, *_ = wired
    executor.candidate_objectives = profile_objectives(CAPACITY_CANDIDATE_PROFILE)
    case.preparation["intent"] = executor.preparation_intent(ProductCaseService._executor_view(case))


def test_three_profiles_are_real_distinct_choices_and_capacity_selection_is_bound(wired_candidates):
    executor, case, system, calls, _, _ = wired_candidates
    enable_capacity(wired_candidates)
    before = copy.deepcopy(system[0].db)
    comparison = local_tests.prepare(wired_candidates)
    assert calls == list(CAPACITY_PRODUCT_OBJECTIVES)
    assert len(comparison["plans"]) == 3 and comparison["selected_plan_id"] is None
    assert [p["capacity"]["standard_minutes"] for p in comparison["plans"]] == [30, 30, 10]
    assert [p["costs"]["planned"]["total"] for p in comparison["plans"]] == ["62.0", "50.0", "90.0"]
    assert all(p["capacity"]["manufactured_units"] == 10 for p in comparison["plans"])
    assert all(p["capacity"]["finished_purchase_units"] == 0 for p in comparison["plans"])
    projected = [json.loads((system[2]["store"] / plan["plan_id"] / "change-set.json").read_text())[
        "projected_state"] for plan in comparison["plans"]]
    assert [row["totals"]["retained_manufactured_quantity"] for row in projected] == [6, 0, 0]
    assert [row["totals"]["new_manufactured_quantity"] for row in projected] == [4, 10, 10]
    disrupted = system[1]["disruption"]["workcenter_code"]
    assert disrupted == "WC-A"
    assert all(mo["workcenter_code"] != disrupted for row in projected
               for mo in row["manufacturing_orders"])
    capacity = comparison["plans"][2]
    view = ProductCaseService._executor_view(case)
    intent = executor.selection_intent(view, comparison, capacity["plan_id"])
    chosen = executor.continue_selection(view, comparison, intent)
    assert chosen["selected_plan_id"] == capacity["plan_id"]
    assert executor.continue_selection(view, comparison, intent) == chosen
    assert len(calls) == 3 and system[0].db == before and not system[0].writes
    assert not list(executor.approvals.glob("approval-*.json"))
    public = project_comparison(chosen)
    assert public["plans"][2]["capacity"] == capacity["capacity"]
    assert "executor_binding" not in json.dumps(public)


def test_legacy_case_cannot_be_reprepared_under_a_new_profile(wired_candidates):
    executor, case, *_ = wired_candidates
    original = local_tests.prepare(wired_candidates)
    executor.candidate_objectives = CAPACITY_PRODUCT_OBJECTIVES
    with pytest.raises(ProductExecutionError, match="product_binding_changed"):
        executor.prepare(ProductCaseService._executor_view(case))
    assert len(original["attempts"]) == 2 and all("capacity" not in p for p in original["plans"])


def test_capacity_is_not_an_orphan_or_legacy_approval_preference():
    assert canonical_objectives(list(PRODUCT_OBJECTIVES)) == list(PRODUCT_OBJECTIVES)
    assert canonical_objectives(list(CAPACITY_PRODUCT_OBJECTIVES)) == list(CAPACITY_PRODUCT_OBJECTIVES)
    for values in (["preserve_capacity"], list(reversed(CAPACITY_PRODUCT_OBJECTIVES)), []):
        with pytest.raises(ValueError, match="product_objectives_invalid"):
            canonical_objectives(values)
    with pytest.raises(ValueError, match="product_objective_binding_changed"):
        bound_objective({"product": {}, "product_objectives": list(PRODUCT_OBJECTIVES),
                         "product_objective": "preserve_capacity"})


def test_capacity_summary_does_not_invent_unknown_times_or_sum_unrelated_product_units():
    request = {"target_product_code": "FIN"}
    blueprint = {"manufactured_products": [{"product": {"code": "FIN"}, "workcenter_choices": [
        {"workcenter_code": "WC-B", "time_per_unit_minutes": 2}]}]}
    changes = {"operations": [
        {"kind": "manufacturing_order", "after": {"product_code": "FIN", "quantity": 4, "workcenter_code": "WC-B"}},
        {"kind": "manufacturing_order", "after": {"product_code": "PART", "quantity": 9, "workcenter_code": "WC-C"}},
        {"kind": "manufacturing_order", "after": None},
        {"kind": "purchase_order", "after": {"product_code": "FIN", "quantity": 3, "supply_role": "finished"}},
    ]}
    before = copy.deepcopy((request, blueprint, changes))
    result = build_capacity_summary(request, blueprint, changes, plan_id="a" * 64, snapshot_sha256="b" * 64)
    assert result["manufactured_units"] == 4 and result["finished_purchase_units"] == 3
    assert result["standard_minutes"] is None and result["missing"] == ["standard_unit_minutes"]
    assert result["workcenters"] == [{"code": "WC-B", "standard_minutes": 8}, {"code": "WC-C", "standard_minutes": None}]
    assert (request, blueprint, changes) == before
    for fault in ("wrong_plan", "false_total", "private_field"):
        invalid = copy.deepcopy(result)
        if fault == "wrong_plan":
            invalid["basis"]["plan_id"] = "c" * 64
        elif fault == "false_total":
            invalid["standard_minutes"] = 8
        else:
            invalid["workcenters"][0]["private"] = "CANARY"
        with pytest.raises(ValueError, match="invalid_capacity_summary"):
            project_capacity(invalid, plan_id="a" * 64, snapshot_sha256="b" * 64)
    assert project_capacity(None, plan_id="a" * 64, snapshot_sha256="b" * 64) is None
