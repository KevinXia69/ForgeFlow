"""The approved actions are compiled from the solved change set, not hand-written."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from recovery_synthetic import inputs

from forgeflow.planning.actions import (
    ActionCompileError,
    compile_actions,
    solver_status,
)
from forgeflow.recovery.change_set import build_change_set

ADDON = Path(__file__).resolve().parents[1] / "odoo-addons" / "forgeflow_general" / "models"
_spec = importlib.util.spec_from_file_location("recovery_logic_actions",
                                               ADDON / "recovery_logic.py")
logic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logic)


def _changes(case: str = "mixed"):
    values = inputs(case)
    return build_change_set(*values), values


def test_actions_are_derived_from_the_solved_operations() -> None:
    changes, values = _changes()
    compiled = compile_actions(changes, plan=values[3], solver_status_value="OPTIMAL")
    by_type = {action["type"]: action for action in compiled["actions"]}
    # The synthetic plan cancels one MO, raises one purchase line to 20 and
    # creates one MO of 4 units.
    assert by_type["cancel_manufacturing_order"]["target_id"] == 1
    assert by_type["adjust_purchase_line"]["quantity"] == 20
    assert by_type["create_manufacturing_order"]["quantity"] == 4
    assert len(compiled["actions"]) == 3
    for action in compiled["actions"]:
        entry = compiled["action_trace"][action["action_id"]]
        assert entry["operation_id"]
        if action["type"].startswith("create_"):
            assert entry["origin_refs"]


def test_the_generated_actions_are_valid_for_the_odoo_addon() -> None:
    changes, values = _changes()
    compiled = compile_actions(changes, plan=values[3], solver_status_value="OPTIMAL")
    validated = logic.validate_action_set(copy.deepcopy(compiled["actions"]))
    # The addon orders the same set; nothing is rejected as unsupported or
    # ambiguous.
    assert [action["type"] for action in logic.execution_order(validated)] == [
        "cancel_manufacturing_order", "create_manufacturing_order",
        "adjust_purchase_line"]


def test_compilation_is_deterministic() -> None:
    changes, values = _changes()
    first = compile_actions(changes, plan=values[3], solver_status_value="OPTIMAL")
    second = compile_actions(copy.deepcopy(changes), plan=values[3],
                             solver_status_value="OPTIMAL")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_changing_business_inputs_changes_the_actions() -> None:
    mixed, mixed_values = _changes("mixed")
    smaller, smaller_values = _changes("quantity")
    mixed_actions = compile_actions(mixed, plan=mixed_values[3],
                                    solver_status_value="OPTIMAL")["actions"]
    smaller_actions = compile_actions(smaller, plan=smaller_values[3],
                                      solver_status_value="OPTIMAL")["actions"]
    assert json.dumps(mixed_actions, sort_keys=True) != json.dumps(
        smaller_actions, sort_keys=True)


def test_an_unsolved_status_cannot_become_an_approval() -> None:
    changes, values = _changes()
    for status in ("INFEASIBLE", "UNKNOWN", "MODEL_INVALID", ""):
        with pytest.raises(ActionCompileError, match="plan_status_not_executable"):
            compile_actions(changes, plan=values[3], solver_status_value=status)


def test_solver_status_comes_from_the_trace() -> None:
    plan = {"trace": {"solve_calls": [{"status": "FEASIBLE"}]}}
    assert solver_status(plan) == "FEASIBLE"
    plan = {"trace": {"solve_calls": [{"status": "UNKNOWN"}]},
            "plan": {"status": "OPTIMAL"}}
    assert solver_status(plan) == "UNKNOWN"


def test_a_failed_projection_check_blocks_actions() -> None:
    changes, values = _changes()
    broken = copy.deepcopy(changes)
    broken["projected_state"]["checks"]["quantity_conservation"] = False
    with pytest.raises(ActionCompileError, match="plan_check_failed"):
        compile_actions(broken, plan=values[3], solver_status_value="OPTIMAL")


def test_a_created_document_without_an_origin_is_refused() -> None:
    changes, values = _changes()
    broken = copy.deepcopy(changes)
    for operation in broken["operations"]:
        if operation["action"] == "create":
            operation["after"]["origin_refs"] = []
    with pytest.raises(ActionCompileError, match="action_without_origin"):
        compile_actions(broken, plan=values[3], solver_status_value="OPTIMAL")


def test_an_unsupported_operation_is_refused_instead_of_skipped() -> None:
    changes, values = _changes()
    broken = copy.deepcopy(changes)
    broken["operations"].append({"operation_id": "op:new:res.users:1",
                                 "kind": "user", "action": "create"})
    with pytest.raises(ActionCompileError, match="operation_kind_unsupported"):
        compile_actions(broken, plan=values[3], solver_status_value="OPTIMAL")


def test_a_plan_with_unconfirmed_assumptions_is_analysis_only() -> None:
    changes, values = _changes()
    plan = copy.deepcopy(values[3])
    plan["assumptions"] = ["budget_unknown:SO1"]
    with pytest.raises(ActionCompileError, match="plan_has_unconfirmed_assumptions"):
        compile_actions(changes, plan=plan, solver_status_value="OPTIMAL")
