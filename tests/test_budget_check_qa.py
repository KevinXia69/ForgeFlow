"""Budget answers distinguish bound per-order evidence from old aggregate checks."""
from __future__ import annotations

import copy
import json

import pytest

from forgeflow.api.budget_context import LEGACY_AGGREGATE_SOURCE, budget_fact
from forgeflow.api.case_context import case_context
from forgeflow.api.case_qa import answer_case
from forgeflow.product.budget_checks import BUDGET_BASIS_KIND


def case_with_check(*, status="pass", actual=5, threshold=5, legacy=False):
    plan_id, snapshot = "a" * 64, "b" * 64
    check = {"code": "budget", "label": "Sales-order values within individual customer caps",
             "source": "Bound sales-order values", "status": status,
             "actual": actual, "threshold": threshold}
    if not legacy:
        check["basis"] = {"kind": BUDGET_BASIS_KIND, "plan_id": plan_id, "snapshot_sha256": snapshot}
    return {"case_id": "c" * 32, "state": "awaiting_approval", "decision": {},
            "plan": {"plan_id": plan_id, "snapshot_sha256": snapshot, "actions": [],
                     "checks": [check], "new_spend": 70, "margin": 93, "on_time": True}}


@pytest.mark.parametrize("question", ["Explain the plan checks", "说明方案检查", "Customer budget?", "客户预算？"])
def test_bound_budget_answer_uses_individual_order_counts_and_scalar_citations(question):
    case = case_with_check()
    before = copy.deepcopy(case)
    reply = answer_case(question, case)
    assert "5/5" in reply["answer"]
    assert "逐单客户销售金额预算检查" in reply["answer"] or "Per-order customer sales-value budget check" in reply["answer"]
    assert "historical budget" not in reply["answer"].lower() and "历史预算" not in reply["answer"]
    fields = {c["field"]: c["value"] for c in reply["citations"]}
    assert fields["plan.checks.0.basis.kind"] == BUDGET_BASIS_KIND
    assert fields["plan.checks.0.actual"] == fields["plan.checks.0.threshold"] == 5
    assert case == before


@pytest.mark.parametrize("source,expected", [(LEGACY_AGGREGATE_SOURCE, "legacy_aggregate"),
                                            ("Custom historical source", "legacy_unrecorded")])
@pytest.mark.parametrize("question", ["Explain the plan checks", "说明方案检查", "Customer budget?", "客户预算？"])
def test_new_label_or_metrics_never_upgrades_legacy_budget_proof(source, expected, question):
    case = case_with_check(legacy=True, actual="70.00", threshold="2000.00")
    case["plan"]["checks"][0]["source"] = source
    # A marker-looking label or unrelated metric presence is not a bound check.
    case["plan"]["decision_metrics"] = {"basis": {"kind": BUDGET_BASIS_KIND}}
    before = copy.deepcopy(case)
    assert budget_fact(case["plan"])["basis"] == expected
    answer = answer_case(question, case)["answer"]
    assert "历史预算" in answer or "historical" in answer
    assert "不代表逐单预算检查" in answer or "不能据此确认逐单预算" in answer or "not a per-order budget check" in answer or "does not establish per-order compliance" in answer
    assert case == before


@pytest.mark.parametrize("status,actual,threshold", [("block", 4, 5), ("unknown", None, 5), ("unknown", None, None)])
def test_blocked_and_unknown_checks_are_not_answered_as_passed(status, actual, threshold):
    case = case_with_check(status=status, actual=actual, threshold=threshold)
    reply = answer_case("Customer budget?", case)["answer"]
    assert f"check: {status}" in reply and "check: pass" not in reply
    if actual is None:
        assert "incomplete" in reply and "0/" not in reply


@pytest.mark.parametrize("fault", ["plan", "snapshot", "kind", "count", "extra"])
def test_invalid_basis_is_neither_legacy_nor_current_compliance(fault):
    case = case_with_check()
    check = case["plan"]["checks"][0]
    if fault in {"plan", "snapshot"}:
        check["basis"]["plan_id" if fault == "plan" else "snapshot_sha256"] = "d" * 64
    elif fault == "kind":
        check["basis"]["kind"] = "invented"
    elif fault == "count":
        check["actual"] = 4
    else:
        check["basis"]["secret"] = "CANARY_PRIVATE"
    answer = answer_case("Customer budget?", case)["answer"]
    assert "invalid basis or plan binding" in answer
    assert "historical" not in answer and "check: pass" not in answer and "CANARY_PRIVATE" not in answer


@pytest.mark.parametrize("legacy", [False, True])
def test_large_context_keeps_explicit_budget_interpretation_and_comparison(legacy):
    case = case_with_check(legacy=legacy)
    plans = []
    for i, token in enumerate("ade"):
        plan = copy.deepcopy(case["plan"])
        plan["plan_id"] = token * 64
        if not legacy:
            plan["checks"][0]["basis"]["plan_id"] = plan["plan_id"]
        plan.update(objective=["preserve_quantities", "min_planned_cost", "preserve_capacity"][i],
                    actions=[{"summary": "large document " * 100}] * 100,
                    costs={"planned": {"total": str(70 + i)}, "basis": {"currency": "USD"}})
        plans.append(plan)
    case["plan"] = plans[1]
    case["comparison"] = {"status": "ready", "plans": plans, "selected_plan_id": plans[1]["plan_id"]}
    case["execution"] = {"status": "succeeded", "verifier": {"strict_success": False, "score": 95.18}}
    before = copy.deepcopy(case)
    context = case_context(case)
    parsed = json.loads(context)
    assert len(context) <= 6000
    assert parsed["plan"]["budget_check"]["basis"] == ("legacy_unrecorded" if legacy else "per_order")
    assert parsed["plan"]["check_counts"]["pass"] == 1
    assert "historical statuses" in parsed["budget_check_interpretation"]
    assert [p["costs"]["planned"]["total"] for p in parsed["comparison"]["plans"]] == ["70", "71", "72"]
    assert parsed["execution"]["verifier"]["strict_success"] is False
    assert case == before


def test_small_context_also_warns_against_unmarked_budget_status_inference():
    case = case_with_check(legacy=True)
    before = copy.deepcopy(case)
    parsed = json.loads(case_context(case))
    assert "unmarked budget check is historical" in parsed["budget_check_interpretation"]
    assert "basis" not in parsed["plan"]["checks"][0]
    assert case == before


def test_context_redaction_cannot_repair_an_invalid_basis_or_trust_injected_summary():
    case = case_with_check()
    case["plan"]["checks"][0]["basis"]["secret"] = "CANARY_PRIVATE"
    case["plan"]["budget_check"] = {"basis": "per_order", "recorded_status": "pass"}
    parsed = json.loads(case_context(case))
    assert parsed["plan"]["budget_check"]["basis"] == "invalid"
    assert "CANARY_PRIVATE" not in json.dumps(parsed)


def completed_comparison_case():
    """Full bounded business summaries that crowded out the comparison before execution."""
    case = case_with_check()
    plans = []
    for index, token in enumerate("ade"):
        plan = copy.deepcopy(case["plan"])
        plan["plan_id"] = token * 64
        plan["checks"][0]["basis"]["plan_id"] = plan["plan_id"]
        plan.update(policy_sha256="f" * 64, prepared_at="2026-10-23T23:00:00Z",
                    objective=["preserve_quantities", "min_planned_cost", "preserve_capacity"][index],
                    new_spend=133394.07 + index, cost_delta=None,
                    actions=[{"summary": "large document " * 100}] * 100,
                    capacity={"manufactured_units": 130, "finished_purchase_units": 38,
                              "standard_minutes": 7382},
                    costs={"known_delta": "-23123.45", "new_purchase_value": "12345.67",
                           "purchase_increase": "12345.67", "new_manufacturing_standard": "12345.67",
                           "comparable": True, "full_delta": None, "actual_cost": None,
                           "baseline": {"purchase": "12345.67", "manufacturing": "12345.67",
                                        "total": "156517.52", "known_subtotal": "156517.52"},
                           "planned": {"purchase": "12345.67", "manufacturing": "12345.67",
                                       "total": str(133394.07 + index), "known_subtotal": str(133394.07 + index)},
                           "basis": {"as_of": "2026-10-23T23:00:00Z", "currency": "USD",
                                     "snapshot_sha256": plan["snapshot_sha256"]}})
        plans.append(plan)
    case.update(scenario="B", synthetic=False, state="succeeded", reason=None, plan=plans[1],
        revision={"version": 2, "parent_plan_id": "0" * 64, "reason": "tighten margin",
                  "changes": {"minimum_margin_pct": 30}},
        revision_limits={"original_margin_pct": 20, "effective_margin_pct": 30, "supported": True},
        comparison={"status": "ready", "comparison_id": "1" * 32, "version": 2, "selection_revision": 3,
                    "selected_plan_id": plans[1]["plan_id"], "plans": plans,
                    "attempts": [{"objective": plan["objective"], "status": "prepared"} for plan in plans]},
        approval={"phase": "consumed", "expires_at": "2026-10-24T00:00:00Z", "issued_at": "2026-10-23T23:00:00Z",
                  "plan_id": plans[1]["plan_id"], "comment": "A" * 160, "approver_label": "owner"},
        execution={"status": "succeeded", "readback": {"execution_validated": True, "verified_steps": 30,
                   "receipt_sha256": "2" * 64, "readback_sha256": "3" * 64, "verified_at": "2026-10-23T23:00:00Z"},
                   "verifier": {"source": "original_verifier", "evaluation_scope": "product_original_verifier",
                                "strict_success": False, "evaluation_valid": True, "score": 95.18,
                                "passed_rule_count": 95, "applicable_rule_count": 95, "failed_rule_count": 0}, "writes": []},
        result={"verification_source": "original_verifier", "execution_status": "succeeded", "readback_status": "passed",
                "verifier_status": "failed", "strict_success": False, "evaluation_valid": True,
                "verified": False, "outcome": "failed"})
    case["decision"] = {key: False for key in (
        "can_approve", "can_reject", "can_request_changes", "can_reconcile", "can_resume_preparation",
        "preparation_pending", "can_cancel_unstarted", "can_resume_revision", "revision_pending",
        "can_reprepare", "repreparation_pending", "can_resume_repreparation", "can_select_candidate",
        "selection_pending", "can_resume_selection")}
    case["decision"].update(reason="plan_already_applied", revision_reason="revision_unsupported")
    return case


@pytest.mark.parametrize("legacy", [False, True])
def test_full_completed_case_keeps_all_candidate_budgets_and_strict_failure_before_optional_details(legacy):
    case = completed_comparison_case()
    if legacy:
        for plan in case["comparison"]["plans"]:
            plan["checks"][0].pop("basis")
            plan["checks"][0]["source"] = LEGACY_AGGREGATE_SOURCE
    original = copy.deepcopy(case)
    context = case_context(case)
    parsed = json.loads(context)
    assert len(context) <= 6000
    assert parsed["plan"]["plan_id"] == case["plan"]["plan_id"]
    assert parsed["plan"]["check_counts"]["pass"] == 1
    assert len(parsed["comparison"]["plans"]) == 3
    assert [row["costs"]["planned"]["total"] for row in parsed["comparison"]["plans"]] == [
        plan["costs"]["planned"]["total"] for plan in case["comparison"]["plans"]]
    assert all(row["budget_check"]["basis"] == ("legacy_aggregate" if legacy else "per_order")
               for row in parsed["comparison"]["plans"])
    assert parsed["execution"]["verifier"]["strict_success"] is False
    assert parsed["execution"]["verifier"]["score"] == 95.18
    assert parsed["execution"]["readback"]["execution_validated"] is True
    assert parsed["decision"]["can_approve"] is False
    assert not {"comparison", "execution", "plan"}.intersection(parsed["omitted_sections"])
    assert case == original


@pytest.mark.parametrize("question", ["Compare the budget checks for all candidates", "比较所有候选的预算检查"])
def test_comparison_explains_and_cites_each_candidate_budget_without_upgrading_legacy(question):
    case = case_with_check()
    new = case["plan"]
    new["objective"] = "preserve_quantities"
    old = copy.deepcopy(new)
    old.update(plan_id="d" * 64, objective="min_planned_cost")
    old["checks"][0].pop("basis")
    old["checks"][0].update(actual="70.00", threshold="2000.00", source=LEGACY_AGGREGATE_SOURCE)
    case["comparison"] = {"status": "ready", "plans": [new, old], "selected_plan_id": new["plan_id"], "version": 1}
    before = copy.deepcopy(case)
    reply = answer_case(question, case)
    answer = reply["answer"]
    assert "5/5" in answer
    assert "历史预算总额对照" in answer or "historical aggregate comparison" in answer
    assert "不代表逐单预算检查" in answer or "not a per-order budget check" in answer
    assert "当前方案的预算事实" in answer or "Budget facts for the recorded current plan" in answer
    fields = {row["field"]: row["value"] for row in reply["citations"]}
    assert fields["comparison.plans.0.checks.0.basis.kind"] == BUDGET_BASIS_KIND
    assert fields["comparison.plans.0.checks.0.actual"] == fields["comparison.plans.0.checks.0.threshold"] == 5
    assert fields["comparison.plans.1.checks.0.status"] == "pass"
    assert fields["comparison.plans.1.checks.0.actual"] == "70.00"
    assert "comparison.plans.1.checks.0.basis.kind" not in fields
    assert case == before
