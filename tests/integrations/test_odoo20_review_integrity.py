"""The approval page receives one complete, digest-verified ERP plan snapshot."""
from __future__ import annotations

import json

import pytest

from forgeflow.integrations.odoo20.business import Odoo20BusinessService, PlanPreparationError
from forgeflow.integrations.odoo20.contracts import canonical_review_action
from tests.support.fake_odoo20 import FakeOdoo20, FakeOdooError, logic


def _actions():
    return [
        {"action_id": "buy", "type": "create_purchase_order", "target_model": "purchase.order",
         "target_id": 0, "quantity": 3, "unit": "units", "currency": "USD",
         "params": {"partner_id": 3, "lines": [
             {"product_id": 4, "product_qty": 3, "price_unit": 10}]}},
        {"action_id": "cancel", "type": "cancel_purchase_order", "target_model": "purchase.order",
         "target_id": 1, "summary": "Cancel original supply"},
    ]


def _prepared():
    erp = FakeOdoo20()
    service = Odoo20BusinessService(erp)
    facts = {"order": {"id": 1, "name": "PO-1"}, "lines": [{"id": 1, "product_qty": 3}]}
    prepared = service.prepare_recovery(1, _actions(), facts, tenant_ref="tenant:company:1")
    return erp, service, prepared, facts


def test_prepare_returns_exact_canonical_stored_order_and_detached_facts():
    erp, service, prepared, facts = _prepared()
    loaded = service.load_prepared(prepared.plan_id)
    assert prepared.actions == loaded.actions
    assert prepared.action_ids == ("cancel", "buy")
    assert prepared.facts == loaded.facts
    assert prepared.action_digest == logic.action_digest(list(prepared.actions))
    assert prepared.snapshot_hash == logic.digest(prepared.facts)
    assert prepared.plan_digest == erp._plan_digest(prepared.plan_id)
    facts["lines"][0]["product_qty"] = 999
    assert prepared.facts["lines"][0]["product_qty"] == 3


@pytest.mark.parametrize("field,value,code", [
    ("payload_json", "[]", "payload_invalid"),
    ("payload_json", "{broken", "payload_invalid"),
    ("payload_json", '{"actions": []}', "actions_incomplete"),
    ("action_count", 9, "actions_incomplete"),
    ("fact_snapshot_json", "[]", "snapshot_invalid"),
    ("fact_snapshot_json", '{"order": {"id": 9}}', "snapshot_digest_mismatch"),
    ("fact_snapshot_json", None, "snapshot_digest_mismatch"),
    ("action_digest", "0" * 64, "action_digest_mismatch"),
    ("plan_digest", "0" * 64, "digest_mismatch"),
    ("version", 2, "digest_mismatch"),
    ("tenant_ref", "other-tenant", "digest_mismatch"),
])
def test_incomplete_or_conflicting_readback_is_not_reviewable(field, value, code):
    erp, service, prepared, _ = _prepared()
    erp._plans[prepared.plan_id][field] = value
    before = erp.writes
    with pytest.raises(PlanPreparationError, match=f"recovery_plan_{code}"):
        service.load_prepared(prepared.plan_id)
    assert erp.writes == before
    assert erp.records["forgeflow.decision_record"] == {}


@pytest.mark.parametrize("duplicate", [False, True])
def test_payload_content_cannot_hide_behind_unchanged_count(duplicate):
    erp, service, prepared, _ = _prepared()
    row = erp._plans[prepared.plan_id]
    payload = json.loads(row["payload_json"])
    if duplicate:
        payload["actions"][1] = payload["actions"][0]
    else:
        payload["actions"][1]["params"]["lines"][0]["price_unit"] = 999
    row["payload_json"] = json.dumps(payload)
    with pytest.raises(PlanPreparationError, match="actions_incomplete|action_digest_mismatch"):
        service.load_prepared(prepared.plan_id)


def test_approval_passes_review_identity_to_atomic_native_check():
    erp, service, prepared, _ = _prepared()
    row = erp._plans[prepared.plan_id]
    row["version"] += 1
    row["plan_digest"] = erp._plan_digest(prepared.plan_id)
    before = erp.writes
    with pytest.raises(FakeOdooError, match="approval_plan_changed"):
        service.approve(prepared, approver_uid=2, comment="reviewed previous version")
    assert erp.writes == before
    assert erp.records["forgeflow.decision_record"] == {}


@pytest.mark.parametrize("action", _actions() + [
    {"action_id": "adjust", "type": "adjust_purchase_line", "target_model": "purchase.order.line",
     "target_ref": {"action_id": "buy", "field": "line_ids"}, "quantity": 1.5,
     "unit": "kg", "currency": "EUR", "preconditions": [{"field": "state", "eq": "draft"}]},
])
def test_client_canonical_action_matches_addon_contract(action):
    assert canonical_review_action(action) == logic.canonical_action(action)


@pytest.mark.parametrize("field", ["plan_digest", "action_digest"])
def test_stale_decision_does_not_become_an_existing_approval(field):
    erp, service, prepared, _ = _prepared()
    decision_id = service.approve(prepared, approver_uid=2, comment="valid review")
    erp.records["forgeflow.decision_record"][decision_id][field] = "0" * 64
    with pytest.raises(PlanPreparationError, match="approval_digest_mismatch"):
        service.load_prepared(prepared.plan_id)


def test_expiry_remains_available_for_command_policy_and_historical_reconciliation():
    erp, service, prepared, _ = _prepared()
    decision_id = service.approve(prepared, approver_uid=2, comment="valid review")
    erp.records["forgeflow.decision_record"][decision_id]["expires_at"] = "2000-01-01 00:00:00"
    loaded = service.load_prepared(prepared.plan_id)
    assert loaded.execution_identity
    assert loaded.detail["approval_expires_at"] == "2000-01-01T00:00:00+00:00"


@pytest.mark.parametrize("count,complete", [(499, True), (500, True), (501, False)])
def test_purchase_line_boundary_probes_beyond_rpc_page_cap(count, complete):
    erp = FakeOdoo20()
    erp.seed_purchase_order(order_id=1, name="PO-1", lines=[{"product_id": 4}] * count)
    service = Odoo20BusinessService(erp)
    snapshot = service.snapshot_purchase_order(1)
    assert len(snapshot["lines"]) == min(count, 500)
    assert snapshot["completeness"]["lines_complete"] is complete
    assert snapshot["completeness"]["truncated"] is (not complete)
    assert snapshot["completeness"]["line_limit"] == 500
    if not complete:
        assert snapshot["completeness"]["reason"] == "purchase_order_line_limit_exceeded"
        prepared = service.prepare_recovery(1, _actions(), snapshot)
        # A partial plan remains inspectable, but cannot create a decision.
        assert service.load_prepared(prepared.plan_id).facts == snapshot
        before = erp.writes
        with pytest.raises(PlanPreparationError, match="snapshot_incomplete"):
            service.approve(prepared, approver_uid=2, comment="truncated")
        assert erp.writes == before
        assert not erp.records["forgeflow.decision_record"]


@pytest.mark.parametrize("completeness", [
    {"complete": False}, {"truncated": True}, {"lines_complete": False},
    {"query_failures": ["purchase.order.line"]}, {"missing_relations": ["stock.move"]},
])
def test_explicitly_partial_snapshot_is_not_approvable(completeness):
    erp, service, _, facts = _prepared()
    facts["completeness"] = completeness
    prepared = service.prepare_recovery(1, _actions(), facts)
    before = erp.writes
    with pytest.raises(PlanPreparationError, match="snapshot_incomplete"):
        service.approve(prepared, approver_uid=2, comment="partial")
    assert erp.writes == before


def test_recovery_snapshot_keeps_source_and_line_truncation_visible(monkeypatch):
    from forgeflow.integrations.odoo20 import resolver

    erp = FakeOdoo20()
    erp.seed_purchase_order(order_id=1, name="PO-1", lines=[{"product_id": 4}] * 501)
    monkeypatch.setattr(resolver.BusinessObjectResolver, "snapshot", lambda self, ref: {
        "record": {"record": {"id": 1, "name": "PO-1"}},
        "lines": [], "completeness": {"complete": False, "truncated": True,
            "query_failures": ["stock.move"], "reason": "partial"},
    })
    snapshot = Odoo20BusinessService(erp).recovery_snapshot(order_id=1)
    assert len(snapshot["lines"]) == 500
    assert snapshot["completeness"]["query_failures"] == ["stock.move"]
    assert snapshot["completeness"]["lines_complete"] is False
    assert snapshot["completeness"]["complete"] is False


def test_failed_overflow_probe_never_claims_complete(monkeypatch):
    erp = FakeOdoo20()
    erp.seed_purchase_order(order_id=1, name="PO-1", lines=[{"product_id": 4}] * 500)
    read = erp.search_read
    def search_read(*args, **kwargs):
        if kwargs.get("offset") == 500:
            raise RuntimeError("synthetic probe failure")
        return read(*args, **kwargs)
    monkeypatch.setattr(erp, "search_read", search_read)
    with pytest.raises(RuntimeError, match="probe failure"):
        Odoo20BusinessService(erp).snapshot_purchase_order(1)
