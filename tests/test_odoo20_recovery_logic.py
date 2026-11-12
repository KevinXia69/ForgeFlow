"""Approval binding, action validation and acceptance for an Odoo 20 plan.

The pure decision logic lives in the addon (``forgeflow_general/models/
recovery_logic.py``) so it can be tested without an Odoo server.  These tests are
the contract the addon glue relies on.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[1] / "odoo-addons" / "forgeflow_general" / "models"
_spec = importlib.util.spec_from_file_location("recovery_logic",
                                               ADDON / "recovery_logic.py")
logic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logic)


def _action(action_id="a1", kind="cancel_purchase_order", model="purchase.order",
            target=7, **extra):
    action = {"action_id": action_id, "type": kind, "target_model": model,
              "target_id": target, "params": {}}
    action.update(extra)
    return action


def _create(**extra):
    action = {"action_id": "a2", "type": "create_purchase_order",
              "target_model": "purchase.order", "target_id": 0,
              "quantity": 10, "unit": "Units", "currency": "USD",
              "params": {"partner_id": 3, "lines": [{"product_id": 1, "product_qty": 10}]}}
    action.update(extra)
    return action


# --------------------------------------------------------------- action sets
def test_action_set_must_be_complete_and_unambiguous() -> None:
    with pytest.raises(logic.PlanError, match="plan_actions_empty"):
        logic.validate_action_set([])
    with pytest.raises(logic.PlanError, match="action_id_duplicated"):
        logic.validate_action_set([_action(), _action()])
    with pytest.raises(logic.PlanError, match="action_type_unsupported"):
        logic.validate_action_set([_action(kind="write_any_model")])
    with pytest.raises(logic.PlanError, match="action_target_id_required"):
        logic.validate_action_set([_action(target=0)])
    with pytest.raises(logic.PlanError, match="action_target_id_must_be_zero"):
        logic.validate_action_set([_create(target_id=7)])
    with pytest.raises(logic.PlanError, match="action_quantity_required"):
        logic.validate_action_set([{**_create(), "quantity": 0}])
    with pytest.raises(logic.PlanError, match="action_currency_required"):
        logic.validate_action_set([{**_create(), "currency": None}])


def test_a_full_recovery_action_set_is_accepted() -> None:
    actions = [
        _action("keep", "preserve_supply", target=11),
        _action("cancel", "cancel_purchase_order", target=7),
        _create(),
        {"action_id": "mo", "type": "create_manufacturing_order",
         "target_model": "mrp.production", "target_id": 0, "quantity": 5,
         "unit": "Units", "currency": "USD",
         "params": {"product_id": 2, "bom_id": 4}},
        {"action_id": "link", "type": "link_supply_to_demand",
         "target_model": "stock.move", "target_id": 9,
         "params": {"sale_line_id": 31}},
    ]
    validated = logic.validate_action_set(actions)
    assert len(validated) == 5
    assert logic.execution_order(actions)[0]["action_id"] == "keep"
    assert logic.execution_order(actions)[-1]["action_id"] == "link"


def test_digests_change_with_any_bound_identity() -> None:
    base = dict(tenant="t1", environment_generation="env1", plan_id=5, plan_version=1,
                fact_snapshot_hash="snap1", rule_version=1, actions=[_action()])
    reference = logic.plan_digest(**base)
    assert logic.plan_digest(**{**base, "plan_version": 2}) != reference
    assert logic.plan_digest(**{**base, "environment_generation": "env2"}) != reference
    assert logic.plan_digest(**{**base, "fact_snapshot_hash": "snap2"}) != reference
    assert logic.plan_digest(**{**base, "rule_version": 2}) != reference
    changed = [{**_action(), "quantity": 3}]
    assert logic.plan_digest(**{**base, "actions": changed}) != reference
    # Key order inside an action must not matter.
    reordered = [{"target_id": 7, "type": "cancel_purchase_order",
                  "action_id": "a1", "params": {}, "target_model": "purchase.order"}]
    assert logic.plan_digest(**{**base, "actions": reordered}) == reference


def test_idempotency_fingerprint_is_bound_to_company_environment_and_version() -> None:
    base = dict(company_id=1, environment_generation="env1", plan_id=5, plan_version=1,
                action_id="a1", params={"date_planned": "2026-11-10"})
    reference = logic.idempotency_fingerprint(**base)
    assert logic.idempotency_fingerprint(**{**base, "company_id": 2}) != reference
    assert logic.idempotency_fingerprint(**{**base, "plan_version": 2}) != reference
    assert logic.idempotency_fingerprint(
        **{**base, "params": {"date_planned": "2026-11-11"}}) != reference
    assert logic.idempotency_fingerprint(**base) == reference


# ----------------------------------------------------------------- approval
def _decision(**overrides):
    decision = {
        "decision": "approved", "plan_hash": "hash-1", "plan_version": 1,
        "plan_digest": "digest-1", "action_digest": "digest-1",
        "environment_generation": "env1", "approver_id": 9,
        "execution_identity": "run-1",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    decision.update(overrides)
    return decision


def _plan(**overrides):
    plan = {"plan_hash": "hash-1", "version": 1, "environment_generation": "env1"}
    plan.update(overrides)
    return plan


def test_approval_must_name_this_plan_version_and_action_set() -> None:
    with pytest.raises(logic.PlanError, match="approval_required"):
        logic.validate_approval(plan=_plan(), decision=None, expected_digest="digest-1")
    with pytest.raises(logic.PlanError, match="approval_plan_hash_mismatch"):
        logic.validate_approval(plan=_plan(), decision=_decision(plan_hash="other"),
                                expected_digest="digest-1")
    with pytest.raises(logic.PlanError, match="approval_plan_version_mismatch"):
        logic.validate_approval(plan=_plan(), decision=_decision(plan_version=2),
                                expected_digest="digest-1")
    with pytest.raises(logic.PlanError, match="approval_environment_mismatch"):
        logic.validate_approval(plan=_plan(),
                                decision=_decision(environment_generation="env9"),
                                expected_digest="digest-1")
    with pytest.raises(logic.PlanError, match="approval_plan_digest_mismatch"):
        logic.validate_approval(plan=_plan(), decision=_decision(),
                                expected_digest="digest-2")
    with pytest.raises(logic.PlanError, match="approval_action_set_mismatch"):
        logic.validate_approval(plan=_plan(), decision=_decision(),
                                expected_digest="digest-1",
                                expected_action_digest="different-actions")
    with pytest.raises(logic.PlanError, match="approval_expired"):
        logic.validate_approval(
            plan=_plan(),
            decision=_decision(expires_at=(datetime.now(UTC)
                                           - timedelta(minutes=1)).isoformat()),
            expected_digest="digest-1")
    with pytest.raises(logic.PlanError, match="approval_execution_identity_required"):
        logic.validate_approval(plan=_plan(),
                                decision=_decision(execution_identity=None),
                                expected_digest="digest-1")
    assert logic.validate_approval(plan=_plan(), decision=_decision(),
                                   expected_digest="digest-1")["execution_identity"] == "run-1"


def test_non_approved_decision_is_rejected() -> None:
    with pytest.raises(logic.PlanError, match="approval_not_granted"):
        logic.validate_approval(plan=_plan(), decision=_decision(decision="rejected"),
                                expected_digest="digest-1")


# ---------------------------------------------------------------- execution
def test_execution_order_never_creates_before_it_cancels() -> None:
    actions = [
        {"action_id": "link", "type": "link_supply_to_demand",
         "target_model": "stock.move", "target_id": 9,
         "params": {"sale_line_id": 31}},
        _create(),
        _action("cancel", "cancel_purchase_order", target=7),
        _action("keep", "preserve_supply", target=11),
    ]
    order = [action["action_id"] for action in logic.execution_order(actions)]
    assert order == ["keep", "cancel", "a2", "link"]


def test_preconditions_are_checked_against_the_current_record() -> None:
    action = {**_action(), "preconditions": [{"field": "state", "in": ["purchase", "sent"]}]}
    logic.check_preconditions(action, {"state": "purchase"})
    with pytest.raises(logic.PlanError, match="precondition_failed"):
        logic.check_preconditions(action, {"state": "done"})
    with pytest.raises(logic.PlanError, match="precondition_field_missing"):
        logic.check_preconditions(action, {"other": 1})


def test_readback_targets_cover_every_touched_document() -> None:
    actions = [_action("cancel", target=7), _create()]
    targets = logic.readback_targets(actions)
    existing = [target for target in targets if target["kind"] == "existing"]
    assert existing and existing[0]["model"] == "purchase.order"
    assert existing[0]["id"] == 7
    created = [target for target in targets if target["kind"] == "created"]
    assert created and created[0]["action_id"] == "a2"


# --------------------------------------------------------------- acceptance
def test_business_acceptance_reads_the_actual_business_state() -> None:
    actions = [_action("cancel", target=7), _action("keep", "preserve_supply", target=11)]
    readback = {
        "purchase.order:7": {"id": 7, "state": "cancel"},
        "purchase.order:11": {"id": 11, "state": "purchase"},
    }
    checks = logic.business_acceptance(actions, readback)
    assert logic.acceptance_status(checks) == "verified"

    missing = logic.business_acceptance(
        actions, {"purchase.order:7": {"id": 7, "state": "cancel"}})
    assert logic.acceptance_status(missing) == "unknown"

    wrong = logic.business_acceptance(actions, {
        "purchase.order:7": {"id": 7, "state": "purchase"},
        "purchase.order:11": {"id": 11, "state": "purchase"}})
    assert logic.acceptance_status(wrong) == "failed"


def test_created_document_is_accepted_by_live_state() -> None:
    # An order header does not carry the line quantity, so acceptance checks that
    # the document exists and is live rather than comparing a number it cannot see.
    checks = logic.business_acceptance(
        [_create()], {"purchase.order:new:a2": {"id": 44, "state": "purchase"}})
    assert logic.acceptance_status(checks) == "verified"
    cancelled = logic.business_acceptance(
        [_create()], {"purchase.order:new:a2": {"id": 44, "state": "cancel"}})
    assert logic.acceptance_status(cancelled) == "failed"
    missing = logic.business_acceptance([_create()], {})
    assert logic.acceptance_status(missing) == "unknown"


def test_plan_checks_require_every_affected_target_to_be_covered() -> None:
    checks = logic.plan_checks(
        actions=[_action("cancel", target=7)],
        facts={"affected_targets": ["purchase.order:7", "purchase.order:8"]})
    statuses = {check["code"]: check["status"] for check in checks}
    assert statuses["action_set_valid"] == "pass"
    assert statuses["target_covered:purchase.order:7"] == "pass"
    assert statuses["target_covered:purchase.order:8"] == "failed"


def test_rule_failure_is_reported_by_the_plan_layer() -> None:
    checks = logic.plan_checks(
        actions=[_action()], facts={},
        rules=[{"name": "margin floor", "satisfied": False, "detail": "12% < 15%"}])
    assert [check for check in checks if check["code"] == "rule:margin floor"][0][
        "status"] == "failed"


def test_payload_round_trips_through_json() -> None:
    actions = [_action(), _create()]
    payload = json.loads(json.dumps({"actions": actions}))
    assert logic.action_digest(payload["actions"]) == logic.action_digest(actions)


def test_none_values_are_stripped_before_an_xmlrpc_response() -> None:
    # Odoo serialises XML-RPC responses without allow_none, so one None anywhere
    # in the structure fails the whole call on the server side.
    payload = {"plan_id": 7, "status": "verified",
               "checks": [{"code": "a", "status": "pass", "detail": None},
                          {"code": "b", "status": "unknown",
                           "detail": [{"why": None, "field": "budget"}]}],
               "readback": {"id": 1, "date_start": None}}
    cleaned = logic.clean_for_rpc(payload)
    assert "detail" not in cleaned["checks"][0]
    assert cleaned["checks"][1]["detail"] == [{"field": "budget"}]
    assert "date_start" not in cleaned["readback"]
    assert cleaned["plan_id"] == 7


def test_receipt_states_cover_acceptance_outcomes() -> None:
    # The acceptance layer writes "verified"/"unknown"; the model must accept them.
    import re
    source = (ADDON / "execution_receipt.py").read_text()
    states = set(re.findall(r'"([a-z_]+)", "[A-Z][a-z]+"', source))
    assert {"succeeded", "failed", "verified", "unknown", "pending"} <= states


# ------------------------------------------------------- whole-plan execution
def test_a_created_target_is_a_reference_not_a_guessed_id() -> None:
    link = {"action_id": "link", "type": "link_supply_to_demand",
            "target_model": "purchase.order.line", "target_id": 0,
            "target_ref": {"action_id": "a2", "field": "line_id"},
            "params": {"sale_line_id": 31}}
    order = [action["action_id"] for action in
             logic.execution_order([link, _create()])]
    assert order == ["a2", "link"]
    created = {"a2": {"id": 55, "model": "purchase.order",
                      "line_id": 77, "line_id_model": "purchase.order.line"}}
    resolved = logic.resolve_action_references(link, created)
    assert resolved["target_id"] == 77
    assert "target_ref" not in resolved
    assert resolved["params"] == {"sale_line_id": 31}


def test_a_parameter_reference_to_the_wrong_model_is_refused() -> None:
    # A purchase order id is not a sales line id: the numbers may coincide, and
    # filling one into the other would link an unrelated record.
    link = {"action_id": "link", "type": "link_supply_to_demand",
            "target_model": "stock.move", "target_id": 9,
            "params": {"sale_line_id": {"$ref": {"action_id": "a2", "field": "id"}}}}
    created = {"a2": {"id": 55, "model": "purchase.order"}}
    with pytest.raises(logic.PlanError, match="action_reference_model_mismatch"):
        logic.resolve_action_references(link, created)


def test_a_parameter_reference_to_the_expected_model_resolves() -> None:
    reserve = {"action_id": "reserve", "type": "reserve_stock",
               "target_model": "stock.move", "target_id": 0,
               "params": {"move_id": {"$ref": {"action_id": "a2",
                                               "field": "move_id"}}}}
    created = {"a2": {"id": 700, "model": "mrp.production", "move_id": 800,
                      "move_id_model": "stock.move"}}
    resolved = logic.resolve_action_references(reserve, created)
    assert resolved["params"] == {"move_id": 800}


def test_reference_dependencies_override_the_phase_table() -> None:
    # A cancel belongs to an earlier phase than a create (20 before 30), but if
    # it references the created record the dependency must win.
    create = _create(action_id="buy")
    cancel = _action("cancel", "cancel_purchase_order", target=7,
                     params={"line_id": {"$ref": {"action_id": "buy", "field": "id"}}})
    order = [action["action_id"] for action in logic.execution_order([cancel, create])]
    assert order == ["buy", "cancel"]


def test_the_reference_is_part_of_the_approved_action_set_digest() -> None:
    base = {"action_id": "link", "type": "link_supply_to_demand",
            "target_model": "purchase.order.line", "target_id": 0,
            "target_ref": {"action_id": "a2", "field": "line_id"},
            "params": {"sale_line_id": 31}}
    other = {**base, "target_ref": {"action_id": "a2", "field": "id"}}
    assert logic.action_digest([base, _create()]) != logic.action_digest([other, _create()])


def test_unknown_or_cyclic_references_are_rejected() -> None:
    unknown = {"action_id": "link", "type": "link_supply_to_demand",
               "target_model": "stock.move", "target_id": 9,
               "params": {"sale_line_id": {"$ref": {"action_id": "nope", "field": "id"}}}}
    with pytest.raises(logic.PlanError, match="action_reference_unknown"):
        logic.validate_action_set([unknown])

    first = _create(action_id="a",
                    params={"partner_id": {"$ref": {"action_id": "b", "field": "id"}}})
    second = _create(action_id="b",
                     params={"partner_id": {"$ref": {"action_id": "a", "field": "id"}}})
    with pytest.raises(logic.PlanError, match="action_reference_cycle"):
        logic.validate_action_set([first, second])


def test_a_reference_to_the_wrong_model_is_rejected_at_resolution() -> None:
    link = {"action_id": "link", "type": "link_supply_to_demand",
            "target_model": "stock.move", "target_id": 0,
            "target_ref": {"action_id": "a2", "field": "line_id"},
            "params": {"sale_line_id": 31}}
    created = {"a2": {"id": 55, "model": "purchase.order",
                      "line_id": 77, "line_id_model": "purchase.order.line"}}
    with pytest.raises(logic.PlanError, match="action_reference_model_mismatch"):
        logic.resolve_action_references(link, created)


def test_a_missing_reference_field_is_reported_not_guessed() -> None:
    link = {"action_id": "link", "type": "link_supply_to_demand",
            "target_model": "purchase.order.line", "target_id": 0,
            "target_ref": {"action_id": "a2", "field": "line_id"},
            "params": {"sale_line_id": 31}}
    with pytest.raises(logic.PlanError, match="action_reference_field_missing"):
        logic.resolve_action_references(link, {"a2": {"id": 55}})


def test_plan_execution_key_ignores_the_retry_prefix() -> None:
    first = logic.plan_execution_key(plan_id=7, plan_version=1, plan_digest="d",
                                     execution_identity="run-1",
                                     environment_generation="env")
    second = logic.plan_execution_key(plan_id=7, plan_version=1, plan_digest="d",
                                      execution_identity="run-1",
                                      environment_generation="env")
    other = logic.plan_execution_key(plan_id=7, plan_version=1, plan_digest="d",
                                     execution_identity="run-2",
                                     environment_generation="env")
    assert first == second
    assert first != other


def test_failure_codes_are_bounded_and_never_a_traceback() -> None:
    assert logic.failure_code(logic.PlanError("stock_unavailable")) == "stock_unavailable"
    assert logic.failure_code(ValueError("stock_unavailable")) == "stock_unavailable"
    assert logic.failure_code(ValueError("A long human sentence with spaces")) == \
        "execution_error"
    division = type("DivisionByZero", (Exception,), {})("division by zero")
    assert logic.failure_code(division) == "database_error"
    assert "Traceback" not in logic.failure_code(RuntimeError("boom"))


def test_a_revoked_approval_authorises_nothing() -> None:
    with pytest.raises(logic.PlanError, match="approval_revoked"):
        logic.validate_approval(plan=_plan(), decision=_decision(revoked_at="2026-11-08"),
                                expected_digest="digest-1")
    with pytest.raises(logic.PlanError, match="approval_approver_inactive"):
        logic.validate_approval(plan=_plan(),
                                decision=_decision(approver_active=False),
                                expected_digest="digest-1")
    with pytest.raises(logic.PlanError, match="approval_approver_not_permitted"):
        logic.validate_approval(plan=_plan(),
                                decision=_decision(approver_permitted=False),
                                expected_digest="digest-1")
