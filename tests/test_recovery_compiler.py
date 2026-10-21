"""Compile fixed recovery steps from independent synthetic business records."""

import copy
import importlib.util

import pytest
from recovery_synthetic import compilation_inputs

from forgeflow.recovery.compiler import CompileError, compile_steps

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None,
    reason="Install optional planning dependencies for recovery checks.",
)


@pytest.mark.parametrize("case", ["mixed", "new_purchase"])
def test_repeated_compilation_preserves_names_and_inputs(case):
    inputs = compilation_inputs(case)
    original = copy.deepcopy(inputs)
    first = compile_steps(*inputs)
    assert first == compile_steps(*inputs)
    assert inputs == original
    assert first["execution_ready"] is False
    assert len({row["name"] for row in first["bindings"].values()}) == len(first["bindings"])


def test_target_total_is_not_recreated_and_purchase_origins_use_actual_names():
    result = compile_steps(*compilation_inputs())
    creates = [step for step in result["steps"] if step.kind == "create_manufacturing"]
    assert len(creates) == 1
    assert creates[0].payload["arguments"]["quantity"] == 4
    assert not any(step.kind == "create_purchase" for step in result["steps"])
    active_names = {
        value["identity"]["name"]
        for value in result["final_expectations"].values()
        if value["identity"]["model"] == "mrp.production"
        and value["fields"].get("state") == "confirmed"
    }
    for step in result["steps"]:
        if step.kind == "update_purchase" and step.payload["arguments"]["mode"] == "origin":
            assert set(step.payload["arguments"]["consumer_names"]) == active_names


def test_new_purchase_is_created_then_confirmed_with_same_identity():
    result = compile_steps(*compilation_inputs("new_purchase"))
    created = next(step for step in result["steps"] if step.kind == "create_purchase")
    confirmed = next(step for step in result["steps"] if step.kind == "confirm_purchase")
    assert created.payload["arguments"]["quantity"] == 20
    assert created.payload["identity"] == confirmed.payload["identity"]
    assert confirmed.depends_on == (created.key,)
    assert confirmed.payload["before"] == created.payload["after"]


@pytest.mark.parametrize("case", ["mixed", "quantity"])
def test_new_or_quantity_changed_workorder_is_bound_before_scheduling(case):
    result = compile_steps(*compilation_inputs(case))
    updates = {
        step.document_token
        for step in result["steps"]
        if step.kind == "update_manufacturing"
        and step.payload["arguments"]["mode"] in ("quantity", "plan")
    }
    previous = {}
    verified = 0
    for step in result["steps"]:
        identity = step.payload["identity"]
        if step.kind == "schedule_workorder" and (
            identity["id"] is None or identity["production"]["token"] in updates
        ):
            binding = previous[step.document_token]
            assert binding.kind == "bind_workorder"
            assert step.payload["before"] == {"binding_key": binding.key}
            verified += 1
        previous[step.document_token] = step
    assert verified > 0


def test_origin_only_write_omits_dates_and_does_not_rebind_existing_workorder():
    result = compile_steps(*compilation_inputs("origin_only"))
    updates = [step for step in result["steps"] if step.kind == "update_manufacturing"]
    assert updates
    for step in updates:
        assert step.payload["arguments"]["mode"] == "origin"
        assert set(step.payload["arguments"]) == {"mode", "consumer_names"}
        assert step.payload["before"]["date_start"] == step.payload["after"]["date_start"]
        assert step.payload["before"]["date_deadline"] == step.payload["after"]["date_deadline"]
        assert not any(
            other.kind == "bind_workorder"
            and other.payload["identity"]["production"]["token"] == step.document_token
            for other in result["steps"]
        )


def test_cancel_keeps_original_identity_quantity_and_unexecuted_preconditions():
    result = compile_steps(*compilation_inputs())
    cancel = next(step for step in result["steps"] if step.kind == "cancel_manufacturing")
    before, after = cancel.payload["before"], cancel.payload["after"]
    assert before["company_id"] == 1
    assert before["product_qty"] == 4 and before["qty_produced"] == 0
    assert after == {**before, "state": "cancel"}


def test_purchase_cancel_keeps_full_line_precondition():
    # Isolate cancellation compilation. Projection validation is the caller's
    # separate gate; this mutation is not claimed to be a feasible recovery plan.
    changes, snapshot, workorders = compilation_inputs()
    purchase = next(op for op in changes["operations"] if op["kind"] == "purchase_order")
    purchase["action"] = "cancel"
    result = compile_steps(changes, snapshot, workorders)
    cancel = next(step for step in result["steps"] if step.kind == "cancel_purchase")
    before = cancel.payload["before"]
    assert before["partner_id"] == 2 and before["company_id"] == 1
    assert before["line"]["product_qty"] == 12
    assert before["line"]["qty_received"] == 0
    assert before["line"]["price_unit"] == 2
    assert cancel.payload["after"] == {**before, "state": "cancel"}


def test_missing_workorder_preflight_is_not_invented():
    changes, snapshot, workorders = compilation_inputs()
    workorders[0].pop("duration_expected")
    with pytest.raises(CompileError, match="incomplete_workorder_preflight"):
        compile_steps(changes, snapshot, workorders)


def test_snapshot_drift_stops_before_compilation():
    changes, snapshot, workorders = compilation_inputs()
    snapshot["models"]["mrp.production"][0]["product_qty"] += 1
    with pytest.raises(CompileError, match="snapshot_mismatch"):
        compile_steps(changes, snapshot, workorders)
