"""Offline A3 regressions using the real verifier and addon's digest logic.

These prove query/verification behavior, not Odoo RPC, ORM or transaction safety.
The live Odoo suite remains a separate acceptance requirement.
"""
from __future__ import annotations

import copy
import json

import pytest
from support.fake_odoo20 import FakeOdoo20, logic

from forgeflow.integrations.odoo20.verifier import Odoo20BusinessVerifier

OPERATION = "forgeflow.execution_operation"
RECEIPT = "forgeflow.execution_receipt"
PLAN = "forgeflow.recovery_plan"


def executed(count=2):
    adapter = FakeOdoo20()
    actions = []
    for index in range(1, count + 1):
        adapter.seed_purchase_order(order_id=index, name=f"PO{index}")
        actions.append({"action_id": f"a{index}", "type": "cancel_purchase_order",
                        "target_model": "purchase.order", "target_id": index, "params": {}})
    plan = adapter.prepare_plan(1, actions, {}, environment_generation="env-test")
    adapter.approve_plan(plan, 7, "execution-test")
    adapter.apply_plan(plan, "attempt", "execution-test")
    return adapter, plan, Odoo20BusinessVerifier(adapter)


def statuses(checks):
    return {check.code: check.status for check in checks}


def test_complete_execution_reaches_verified_and_preserves_native_readback():
    adapter, plan, verifier = executed()
    result = verifier.verify_plan(plan)
    assert result.verified
    assert result.layers["readback"] == "verified"
    assert statuses(result.checks)["receipts_present"] == "pass"
    assert {model for model, _ in adapter.calls} >= {OPERATION, RECEIPT, "purchase.order"}
    adapter.records["purchase.order"][2]["state"] = "purchase"
    assert verifier.verify_plan(plan).status == "failed"


def test_duplicate_and_aggregate_receipts_cannot_hide_another_missing_operation():
    adapter, plan, verifier = executed()
    adapter.records[RECEIPT][2] = {**adapter.records[RECEIPT][1], "id": 2}
    adapter.records[RECEIPT][3] = {
        "id": 3, "operation_id": 2, "company_id": [1, "Company"],
        "state": "verified", "verified": True,
        "readback_json": json.dumps({"checks": [{"status": "pass"}], "readback": {}})}
    called = []
    adapter.verify_plan = lambda _: called.append(True)
    result = verifier.verify_plan(plan)
    assert result.status == "unknown"
    assert statuses(result.checks)["a2:receipt"] == "unknown"
    assert statuses(result.checks)["receipts_present"] == "unknown"
    assert not called  # The addon must not persist a success terminal state.


def test_legitimate_history_and_multiple_verifications_remain_valid():
    adapter, plan, verifier = executed()
    original = copy.deepcopy(adapter.records[RECEIPT][1])
    adapter.records[RECEIPT][1].update(state="failed", readback_json='{"error":"retry"}')
    adapter.records[RECEIPT][3] = {**original, "id": 3}
    adapter.records[RECEIPT][4] = {**original, "id": 4}
    for record_id in (5, 6):
        adapter.records[RECEIPT][record_id] = {
            **original, "id": record_id, "state": "verified", "verified": True,
            "readback_json": json.dumps({"checks": [], "readback": {}})}
    assert verifier.verify_plan(plan).verified
    assert len(adapter.records[RECEIPT]) == 6


@pytest.mark.parametrize("change", [
    {"state": "failed"}, {"state": "unknown"}, {"state": "pending"},
    {"state": "verified", "verified": False}, {"company_id": [2, "Other"]},
    {"readback_json": "invalid"},
    {"readback_json": json.dumps({"model": "purchase.order", "id": 999})},
    {"readback_json": json.dumps({"model": "mrp.production", "id": 1})},
])
def test_unresolved_or_mismatched_execution_receipt_is_unknown(change):
    adapter, plan, verifier = executed()
    adapter.records[RECEIPT][1].update(change)
    assert verifier.verify_plan(plan).status == "unknown"


@pytest.mark.parametrize("change", [
    {"plan_id": 999}, {"company_id": [2, "Other"]}, {"plan_version": 2},
    {"plan_digest": "wrong"}, {"environment_generation": "other"},
    {"execution_identity": "other"}, {"fingerprint": "wrong"},
    {"request_hash": "wrong"},
    {"target_model": "mrp.production"}, {"target_id": 999},
    {"operation_type": "preserve_supply"}, {"execution_key": "other"},
])
def test_operation_must_match_exact_plan_and_execution(change):
    adapter, plan, verifier = executed()
    adapter.records[OPERATION][1].update(change)
    assert not verifier.verify_plan(plan).verified


def test_all_operations_and_receipts_are_paginated_and_unrelated_history_is_excluded():
    adapter, plan, verifier = executed(205)
    for record_id in range(1000, 1300):
        adapter.records[RECEIPT][record_id] = {
            "id": record_id, "operation_id": 9999, "state": "succeeded"}
    assert verifier.verify_plan(plan).verified
    queries = [domain for model, domain in adapter.calls if model == RECEIPT]
    assert all(any(clause[0:2] == ("operation_id", "in") for clause in domain)
               for domain in queries)
    assert any(("id", ">", 200) in domain for model, domain in adapter.calls
               if model == OPERATION)
    del adapter.records[RECEIPT][205]
    assert verifier.verify_plan(plan).status == "unknown"


def test_more_than_one_page_of_receipts_for_one_operation():
    adapter, plan, verifier = executed(1)
    original = adapter.records[RECEIPT][1]
    for record_id in range(2, 231):
        adapter.records[RECEIPT][record_id] = {**original, "id": record_id}
    assert verifier.verify_plan(plan).verified
    # The latest unresolved receipt cannot be hidden beyond the first page.
    adapter.records[RECEIPT][230]["state"] = "unknown"
    assert verifier.verify_plan(plan).status == "unknown"


def test_short_pages_do_not_claim_query_completion():
    adapter, plan, verifier = executed(5)
    search = adapter.search_read
    adapter.search_read = lambda *args, **kwargs: search(*args, **{**kwargs, "limit": 2})
    assert verifier.verify_plan(plan).verified


@pytest.mark.parametrize("mode", ["failure", "repeated_page", "unrelated"])
def test_incomplete_or_unscoped_query_cannot_verify(mode):
    adapter, plan, verifier = executed()
    search = adapter.search_read

    def query(model, domain, fields, **kwargs):
        if model == RECEIPT:
            if mode == "failure":
                raise RuntimeError("injected read failure")
            if mode == "repeated_page":
                return [adapter.records[RECEIPT][1]]
            if ("id", ">", 0) in domain:
                return [{**adapter.records[RECEIPT][1], "operation_id": 999}]
            return []
        return search(model, domain, fields, **kwargs)

    adapter.search_read = query
    assert verifier.verify_plan(plan).status == "unknown"


def test_explicit_record_limit_does_not_verify_partial_results():
    _, plan, verifier = executed(3)
    verifier.MAX_RECORDS = 2
    result = verifier.verify_plan(plan)
    assert result.status == "unknown"
    assert statuses(result.checks)["readback_supported_limit_exceeded"] == "unknown"


def test_legacy_verification_accepts_multiple_execution_and_aggregate_receipts():
    adapter, plan, verifier = executed(1)
    original = adapter.records[RECEIPT][1]
    adapter.records[RECEIPT][2] = {**original, "id": 2}
    adapter.records[RECEIPT][3] = {
        **original, "id": 3, "state": "verified", "verified": True,
        "readback_json": json.dumps({"checks": [], "readback": {}})}
    key = adapter.records[OPERATION][1]["idempotency_key"]
    assert verifier.verify_purchase_cancel(plan, key).verified


def test_ambiguous_duplicate_operation_is_not_arbitrarily_chosen():
    adapter, plan, verifier = executed()
    adapter.records[OPERATION][3] = {**adapter.records[OPERATION][1], "id": 3}
    assert not verifier.verify_plan(plan).verified


def test_plan_version_change_during_readback_is_unknown():
    adapter, plan, verifier = executed()
    search = adapter.search_read

    def query(model, domain, fields, **kwargs):
        if model == RECEIPT:
            adapter.records[PLAN][plan]["version"] = 2
        return search(model, domain, fields, **kwargs)

    adapter.search_read = query
    result = verifier.verify_plan(plan)
    assert result.status == "unknown"
    assert statuses(result.checks)["plan_readback_stable"] == "unknown"


def test_payload_action_count_mismatch_is_unknown():
    adapter, plan, verifier = executed()
    adapter.records[PLAN][plan]["action_count"] = 1
    assert verifier.verify_plan(plan).status == "unknown"


def test_old_attempt_operations_and_receipts_do_not_replace_current_evidence():
    adapter, plan, verifier = executed()
    adapter.records[OPERATION][3] = {
        **adapter.records[OPERATION][1], "id": 3, "execution_key": "old-attempt"}
    adapter.records[RECEIPT][3] = {**adapter.records[RECEIPT][1], "id": 3, "operation_id": 3}
    assert verifier.verify_plan(plan).verified
    del adapter.records[RECEIPT][1]
    assert verifier.verify_plan(plan).status == "unknown"


def test_created_and_stock_reservation_records_are_read_independently():
    adapter = FakeOdoo20()
    adapter.seed_stock_move(8, product_id=1, quantity=2)
    actions = [
        {"action_id": "create", "type": "create_purchase_order",
         "target_model": "purchase.order", "target_id": 0,
         "quantity": 2, "unit": "Units", "currency": "USD",
         "params": {"partner_id": 3, "lines": [{"product_id": 1, "product_qty": 2}]}},
        {"action_id": "reserve", "type": "reserve_stock", "target_model": "stock.move",
         "target_id": 0, "quantity": 2, "params": {"product_id": 1, "move_id": 8}},
    ]
    plan = adapter.prepare_plan(1, actions, {}, environment_generation="env-test")
    adapter.approve_plan(plan, 7, "execution-test")
    adapter.apply_plan(plan, "attempt", "execution-test")
    verifier = Odoo20BusinessVerifier(adapter)
    assert verifier.verify_plan(plan).verified
    del adapter.records["stock.move"][8]
    assert verifier.verify_plan(plan).status == "failed"


def test_resolved_target_references_bind_to_the_producing_operation():
    adapter = FakeOdoo20()
    actions = [
        {"action_id": "create", "type": "create_purchase_order",
         "target_model": "purchase.order", "target_id": 0,
         "quantity": 2, "unit": "Units", "currency": "USD",
         "params": {"partner_id": 3, "lines": [{"product_id": 1, "product_qty": 2}]}},
        {"action_id": "keep", "type": "preserve_supply", "target_model": "purchase.order",
         "target_id": 0, "params": {}, "target_ref": {"action_id": "create", "field": "id"}},
    ]
    plan = adapter.prepare_plan(1, actions, {}, environment_generation="env-test")
    adapter.approve_plan(plan, 7, "execution-test")
    # The small shared fake does not resolve references. Reproduce the real
    # addon's resolution before dispatch, using its actual pure helper.
    run = adapter._run_action
    created = {}

    def run_resolved(action):
        resolved = logic.resolve_action_references(action, created)
        operation = list(adapter.records[OPERATION].values())[-1]
        operation["target_id"] = resolved["target_id"]
        result = run(resolved)
        created[action["action_id"]] = result
        return result

    adapter._run_action = run_resolved
    adapter.apply_plan(plan, "attempt", "execution-test")
    verifier = Odoo20BusinessVerifier(adapter)
    checks, _ = verifier.readback(plan)
    assert all(check.status == "pass" for check in checks)
    adapter.records[OPERATION][2]["target_id"] = 999
    checks, _ = verifier.readback(plan)
    assert statuses(checks)["keep:operation_binding"] == "unknown"


def test_unavailable_plan_query_returns_unknown_and_does_not_run_acceptance():
    adapter, plan, verifier = executed()
    called = []

    def unavailable(*args, **kwargs):
        raise RuntimeError("injected plan read failure")

    adapter.search_read = unavailable
    adapter.verify_plan = lambda _: called.append(True)
    assert verifier.verify_plan(plan).status == "unknown"
    assert not called


@pytest.mark.parametrize("state", ["unknown", "unsupported", None])
def test_unconfirmed_business_acceptance_cannot_become_verified(state):
    adapter, plan, verifier = executed()
    adapter.verify_plan = lambda _: {"status": state, "checks": []}
    assert verifier.verify_plan(plan).status == "unknown"


def native_evidence(adapter, plan):
    record = adapter.records[PLAN][plan]
    operations = [operation for operation in adapter.records[OPERATION].values()
                  if operation.get("execution_key") == record.get("execution_key")]
    operation_ids = {operation["id"] for operation in operations}
    receipts = [receipt for receipt in adapter.records[RECEIPT].values()
                if receipt.get("operation_id") in operation_ids]
    return logic.execution_evidence_complete(
        plan=record, actions=json.loads(record["payload_json"])["actions"],
        operations=operations, receipts=receipts)


def test_native_terminal_gate_allows_complete_history_and_rejects_duplicate_masking():
    adapter, plan, _ = executed(205)
    assert native_evidence(adapter, plan)
    adapter.records[RECEIPT][206] = {**adapter.records[RECEIPT][1], "id": 206}
    assert native_evidence(adapter, plan)
    del adapter.records[RECEIPT][205]
    assert not native_evidence(adapter, plan)


def test_native_terminal_gate_does_not_count_plan_verification_as_execution():
    adapter, plan, _ = executed()
    adapter.records[RECEIPT][2].update(
        state="verified", verified=True, readback_json=json.dumps({"checks": [], "readback": {}}))
    assert not native_evidence(adapter, plan)


@pytest.mark.parametrize("change", [
    {"state": "unknown"}, {"company_id": [2, "Other"]}, {"plan_version": 2},
    {"execution_identity": "other"}, {"fingerprint": "wrong"}, {"request_hash": "wrong"},
    {"target_id": 999}, {"response_json": "invalid"},
])
def test_native_terminal_gate_rejects_unbound_or_unresolved_operations(change):
    adapter, plan, _ = executed()
    adapter.records[OPERATION][1].update(change)
    assert not native_evidence(adapter, plan)
