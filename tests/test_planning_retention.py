"""Real constrained reallocation plus independent mapping and fallback checks."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import test_planning_tool as planning_tests
from test_recovery_change_set import fixture

from forgeflow.planning import retention
from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.planning.live import build_blueprint
from forgeflow.planning.tool import solve_blueprint
from forgeflow.recovery.change_set import ChangeSetError, build_change_set, validate_projection

planner_bundle = planning_tests.planner_bundle


def two_deadline_facts(early=4, late=6, stock=2, old_start=5):
    """One valid late MO cannot supply the earlier customer; quantities vary."""
    request, snapshot, _, _ = fixture(early + late)
    models = snapshot["models"]
    request["sales_order_ids"] = [1, 2]
    request["sales_order_budget_caps"] = {"1": 10000, "2": 10000}
    snapshot["scope"]["sales_order_ids"] = [1, 2]
    customer = copy.deepcopy(models["res.partner"][0])
    customer.update(id=3, name="Later customer", ref="C2")
    models["res.partner"].append(customer)
    order = copy.deepcopy(models["sale.order"][0])
    order.update(id=2, name="SO2", partner_id=[3, "Later customer"],
                 commitment_date="2026-01-09 12:00:00")
    models["sale.order"].append(order)
    models["sale.order"][0]["commitment_date"] = "2026-01-05 12:00:00"
    line = copy.deepcopy(models["sale.order.line"][0])
    line.update(id=2, order_id=[2, "SO2"], product_uom_qty=late)
    models["sale.order.line"].append(line)
    models["sale.order.line"][0]["product_uom_qty"] = early
    models["stock.quant"][0]["quantity"] = stock
    models["mrp.production"][0]["product_qty"] = early
    models["mrp.production"][1].update(product_qty=late, origin="SO2",
        date_start=f"2026-01-{old_start + 1:02d} 12:00:00",
        date_deadline=f"2026-01-{old_start + 2:02d} 12:00:00")
    return request, snapshot


def solved(planner_bundle, **kwargs):
    request, snapshot = two_deadline_facts(**kwargs)
    blueprint = build_blueprint(snapshot, request)
    upstream = planner_bundle[0] / "upstream"
    plan = solve_blueprint(blueprint, upstream)
    return request, snapshot, blueprint, plan, upstream


def retained(inputs, plan):
    request, snapshot, blueprint, _, _ = inputs
    changes = build_change_set(request, snapshot, blueprint, plan)
    validate_projection(changes, request, snapshot, blueprint, plan)
    return changes["projected_state"]["totals"]["retained_manufactured_quantity"]


def test_fixed_objectives_joint_stock_allocation_retains_six_instead_of_four(planner_bundle):
    inputs = solved(planner_bundle)
    before = copy.deepcopy(inputs[:-1])
    result = retention.refine_retention(*inputs)
    assert inputs[:-1] == before
    audit = result["retention_refinement"]
    assert audit["status"] == "improved"
    assert audit["profile"] == retention.RETENTION_PROFILE
    assert audit["retained_before"] == retained(inputs, inputs[3]) == 4
    assert audit["retained_after"] == retained(inputs, result) == 6
    assert audit["primary_optimum"] == result["plan"]["objective_value"] == 6
    assert audit["spend_optimum_cents"] == 5600
    assert result["plan"]["expected_new_spend"] == inputs[3]["plan"]["expected_new_spend"] == 56
    assert retention._totals(result["plan"]) == retention._totals(inputs[3]["plan"])
    assert [(a["stock_allocated"], a["assembly_units"]) for a in result["plan"]["allocations"]] == [
        (2, 2), (0, 6)]
    assert audit["solver"]["status"] == "OPTIMAL"
    assert audit["solver"]["solve_calls"] == 1
    assert 0 < audit["solver"]["time_limit_seconds"] <= 2
    assert audit["original_plan_sha256"] == retention._digest(inputs[3])
    assert audit["original_plan"] == inputs[3]
    retention.validate_refinement_audit(result)
    # Planning remains opt-in; the original solve has no refinement metadata.
    assert "retention_refinement" not in inputs[3]


@pytest.mark.parametrize("early,late,stock", [(5, 8, 3), (3, 7, 1), (6, 5, 2)])
def test_allocation_refinement_is_general_not_specific_to_regression_numbers(
    planner_bundle, early, late, stock,
):
    inputs = solved(planner_bundle, early=early, late=late, stock=stock)
    result = retention.refine_retention(*inputs)
    assert result["retention_refinement"]["status"] in {"improved", "unchanged"}
    assert retained(inputs, result) == late
    assert result["plan"]["expected_new_spend"] == (early + late - stock) * 7
    assert result["plan"]["objective_value"] == inputs[3]["plan"]["objective_value"]
    assert retention._totals(result["plan"]) == retention._totals(inputs[3]["plan"])


def test_no_improvement_preserves_exact_original_plan(planner_bundle):
    inputs = solved(planner_bundle, stock=0)
    result = retention.refine_retention(*inputs)
    assert result["plan"] == inputs[3]["plan"]
    assert result["retention_refinement"]["status"] == "unchanged"
    assert result["retention_refinement"]["retained_before"] == 6
    assert result["retention_refinement"]["retained_after"] == 6


def test_component_arrival_prevents_false_retention(planner_bundle):
    # Existing MO starts before the only RAW supplier can arrive (day 1).
    inputs = solved(planner_bundle, old_start=0)
    result = retention.refine_retention(*inputs)
    assert result["retention_refinement"]["status"] == "unchanged"
    assert retained(inputs, result) == 0
    assert result["retention_refinement"]["solver"]["retained_quantity_optimum"] == 0


def test_zero_budget_does_not_solve_or_replace_verified_plan(planner_bundle, monkeypatch):
    inputs = solved(planner_bundle)
    monkeypatch.setattr(retention, "_solve_refinement", lambda *args: pytest.fail("Unexpected solve"))
    result = retention.refine_retention(*inputs, time_limit_seconds=0)
    assert result["plan"] == inputs[3]["plan"]
    assert result["retention_refinement"]["status"] == "fallback"
    assert result["retention_refinement"]["reason"] == "budget_exhausted"
    assert result["retention_refinement"]["solver"]["solve_calls"] == 0


def test_model_build_exhausting_budget_does_not_start_cp_sat(planner_bundle):
    inputs = solved(planner_bundle)
    result = retention.refine_retention(*inputs, time_limit_seconds=1e-9)
    assert result["plan"] == inputs[3]["plan"]
    assert result["retention_refinement"]["solver"] == {
        "status": "BUDGET_EXHAUSTED", "solve_calls": 0}
    assert result["retention_refinement"]["status"] == "fallback"


@pytest.mark.parametrize("status", ["UNKNOWN", "FEASIBLE", "INFEASIBLE"])
def test_uncertified_refinement_always_falls_back(planner_bundle, monkeypatch, status):
    inputs = solved(planner_bundle)
    monkeypatch.setattr(retention, "_solve_refinement", lambda *args: (
        None, {"status": status, "solve_calls": 1}))
    result = retention.refine_retention(*inputs)
    assert result["plan"] == inputs[3]["plan"]
    assert result["retention_refinement"]["status"] == "fallback"
    assert result["retention_refinement"]["solver"]["status"] == status
    assert result["retention_refinement"]["retained_after"] == 4


def test_independent_validation_failure_keeps_original_without_leaking_error(
    planner_bundle, monkeypatch,
):
    inputs = solved(planner_bundle)
    validate = retention._validate
    calls = []

    def fail_replacement(*args):
        calls.append(args)
        if len(calls) == 2:
            raise ValueError("sensitive ERP content must not be published")
        return validate(*args)

    monkeypatch.setattr(retention, "_validate", fail_replacement)
    result = retention.refine_retention(*inputs)
    assert len(calls) == 2
    assert result["plan"] == inputs[3]["plan"]
    assert result["retention_refinement"]["status"] == "fallback"
    assert "sensitive ERP" not in json.dumps(result)


@pytest.mark.parametrize("tamper", ["cost", "primary", "quantity"])
def test_replacement_cannot_change_fixed_objectives(planner_bundle, monkeypatch, tamper):
    inputs = solved(planner_bundle)
    replacement = copy.deepcopy(inputs[3]["plan"])
    if tamper == "cost":
        replacement["purchase_orders"][0]["unit_cost"] += 1
    elif tamper == "primary":
        replacement["objective_value"] += 1
    else:
        replacement["manufacturing_orders"][0]["quantity"] += 1
    monkeypatch.setattr(retention, "_solve_refinement", lambda *args: (
        replacement, {"status": "OPTIMAL", "solve_calls": 1, "retained_quantity_optimum": 6}))
    result = retention.refine_retention(*inputs)
    assert result["plan"] == inputs[3]["plan"]
    assert result["retention_refinement"]["reason"] == "fixed_objectives_changed"


def test_invalid_original_never_becomes_a_successful_fallback(planner_bundle):
    inputs = list(solved(planner_bundle))
    inputs[3]["blueprint_sha256"] = "0" * 64
    with pytest.raises(ChangeSetError):
        retention.refine_retention(*inputs)


def test_repeated_refinement_is_rejected_before_nested_audit_or_validation(planner_bundle, monkeypatch):
    inputs = list(solved(planner_bundle))
    inputs[3] = retention.refine_retention(*inputs)
    monkeypatch.setattr(retention, "_validate", lambda *args: pytest.fail("Unexpected revalidation"))
    with pytest.raises(ValueError, match="retention_already_refined"):
        retention.refine_retention(*inputs)


@pytest.mark.parametrize("tamper", ["source", "hash", "nested", "status", "quantity"])
def test_retention_audit_checks_original_hash_and_result_semantics(planner_bundle, tamper):
    result = retention.refine_retention(*solved(planner_bundle))
    audit = result["retention_refinement"]
    if tamper == "source":
        audit["original_plan"]["plan"]["expected_new_spend"] = 1
    elif tamper == "hash":
        audit["original_plan_sha256"] = "0" * 64
    elif tamper == "nested":
        audit["original_plan"]["retention_refinement"] = {}
        audit["original_plan_sha256"] = retention._digest(audit["original_plan"])
    elif tamper == "status":
        audit["status"] = "fallback"
    else:
        audit["retained_after"] = audit["retained_before"]
    with pytest.raises(ValueError, match="retention_audit_invalid"):
        retention.validate_refinement_audit(result)


def test_unproven_original_optima_skip_experiment(planner_bundle, monkeypatch):
    inputs = list(solved(planner_bundle))
    inputs[3]["trace"] = {"models": [], "solve_calls": []}
    monkeypatch.setattr(retention, "_solve_refinement", lambda *args: pytest.fail("Unexpected solve"))
    result = retention.refine_retention(*inputs)
    assert result["plan"] == inputs[3]["plan"]
    assert result["retention_refinement"]["status"] == "skipped"
    assert result["retention_refinement"]["reason"] == "original_optima_not_proven"


def test_other_objectives_are_not_silently_converted_to_repair(planner_bundle):
    request, snapshot = two_deadline_facts()
    blueprint = build_blueprint(snapshot, request)
    upstream = planner_bundle[0] / "upstream"
    plan = solve_blueprint(blueprint, upstream, product_objective="min_planned_cost")
    result = retention.refine_retention(request, snapshot, blueprint, plan, upstream,
                                        product_objective="min_planned_cost")
    assert result["plan"] == plan["plan"]
    assert result["retention_refinement"]["status"] == "skipped"
    assert result["retention_refinement"]["reason"] == "non_repair_objective"


@pytest.mark.parametrize("limit", [-1, 21, True, "2", float("nan"), float("inf")])
def test_invalid_time_budgets_rejected_before_any_work(limit):
    with pytest.raises(ValueError, match="retention_time_limit_invalid"):
        retention.refine_retention(None, None, None, None, None, time_limit_seconds=limit)


def test_recovery_bundle_contains_the_complete_refinement_runtime(tmp_path):
    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "recovery.tar.gz"
    record = build_planner_bundle(root, archive, recovery=True)
    assert "forgeflow/planning/retention.py" in record["files_sha256"]
    with tarfile.open(archive) as packed:
        packed.extractall(tmp_path, filter="data")
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(two_deadline_facts()))
    script = """
import json, sys
from pathlib import Path
bundle = Path(sys.argv[1]); sys.path.insert(0, str(bundle))
from forgeflow.planning.live import build_blueprint
from forgeflow.planning.retention import refine_retention
from forgeflow.planning.tool import solve_blueprint
request, snapshot = json.loads(Path(sys.argv[2]).read_text())
blueprint = build_blueprint(snapshot, request)
plan = solve_blueprint(blueprint, bundle / 'upstream')
result = refine_retention(request, snapshot, blueprint, plan, bundle / 'upstream')
print(json.dumps(result['retention_refinement']))
"""
    completed = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                str(tmp_path / "planner"), str(facts)],
                               check=True, capture_output=True, text=True, timeout=30)
    audit = json.loads(completed.stdout)
    assert audit["status"] == "improved"
    assert audit["retained_before"] == 4 and audit["retained_after"] == 6
