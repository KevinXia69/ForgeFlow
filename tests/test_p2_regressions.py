"""Regression tests for the five static-review P2 findings."""
from __future__ import annotations

import json
import os
import uuid
from datetime import date

import pytest

from forgeflow.integrations.odoo20.business import (
    Odoo20BusinessService,
    PlanPreparationError,
    PreparedCancellation,
)
from forgeflow.integrations.odoo20.procurement import _open_commitments
from forgeflow.superagent import ControlEngine, ControlStore, Role, TenantContext
from forgeflow.superagent.tasks import link_plan_to_ticket
from tests.support.fake_odoo20 import logic

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not DATABASE_URL,
                                 reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


class _ApprovalAdapter:
    def __init__(self, decisions):
        self.decisions = decisions
        self.domains = []
        self.prefix = ""

    def search_read(self, model, domain, fields, limit=100, order=None, extra=None):
        if model == "forgeflow.recovery_plan":
            actions = [logic.canonical_action({"action_id": "cancel", "type": "cancel_purchase_order",
                "target_model": "purchase.order", "target_id": 1})]
            self.action_digest = logic.action_digest(actions)
            self.plan_digest = logic.plan_digest(tenant="", environment_generation="", plan_id=7,
                plan_version=1, fact_snapshot_hash=logic.digest({}), rule_version=1, actions=actions)
            return [{
                "id": 7, "state": "awaiting_approval", "plan_hash": "a" * 64,
                "fact_snapshot_hash": logic.digest({}), "fact_snapshot_json": "{}",
                "payload_json": json.dumps({"actions": actions}), "exception_id": 3,
                "action_count": 1, "action_digest": self.action_digest,
                "plan_digest": self.plan_digest, "version": 1,
            }]
        if model == "forgeflow.decision_record":
            self.domains.append(list(domain))
            if ("revoked_at", "=", False) in domain:
                return [{**row, "plan_digest": self.plan_digest, "action_digest": self.action_digest}
                        for row in self.decisions if not row.get("revoked_at")]
            return list(self.decisions)
        raise AssertionError(model)

    def write(self, model, ids, values):
        return True

    def apply_plan(self, plan_id, prefix, identity):
        self.prefix = prefix
        return {"plan_id": plan_id, "state": "succeeded", "actions": []}


def test_revoked_approval_is_ignored_and_reapproval_gets_a_new_key() -> None:
    adapter = _ApprovalAdapter([
        {"id": 1, "execution_identity": "tenant:7:old", "revoked_at": "2026-11-08"},
        {"id": 2, "execution_identity": "tenant:7:new"},
    ])
    service = Odoo20BusinessService(adapter)
    prepared = service.load_prepared(7)
    assert prepared.execution_identity == "tenant:7:new"
    assert any(("revoked_at", "=", False) in domain for domain in adapter.domains)
    service.apply_plan(prepared)
    assert adapter.prefix.endswith(":new")


def test_a_fully_revoked_plan_cannot_execute() -> None:
    adapter = _ApprovalAdapter([
        {"id": 1, "execution_identity": "tenant:7:old", "revoked_at": "2026-11-08"},
    ])
    service = Odoo20BusinessService(adapter)
    prepared = service.load_prepared(7)
    assert prepared.execution_identity == ""
    with pytest.raises(PlanPreparationError, match="execution_identity_required"):
        service.apply_plan(prepared)


class _ReconcileBusiness:
    def __init__(self):
        self.prepared = PreparedCancellation(
            exception_id=3, plan_id=77, plan_hash="c" * 64, snapshot_hash="d" * 64,
            facts={}, execution_identity="identity")
        self.adapter = _ApprovalAdapter([])
        self.reconciled = []

    def load_prepared(self, plan_id):
        return self.prepared

    def reconcile(self, prepared, *, idempotency_key):
        from forgeflow.integrations.odoo20.executor import ExecutionResult
        self.reconciled.append(idempotency_key)
        return ExecutionResult(operation_id=11, state="succeeded",
                               readback={}, receipt_id=0)


@requires_db
def test_manual_reconcile_queues_verification() -> None:
    from forgeflow.superagent.execution import reconcile_execution

    tenant = f"reconcile-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Reconcile", 1, (("planner", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner", frozenset({Role.PLANNER}), 1)
    engine = ControlEngine(store)
    opened = engine.open_task(ctx, object_ref="purchase.order:9", goal="reconcile",
                              state="running")
    link_plan_to_ticket(store, ctx, plan_id=77, ticket_id=opened["ticket_id"])
    try:
        result = reconcile_execution(store, ctx, _ReconcileBusiness(), plan_id=77,
                                     idempotency_key="plan:key")
        assert result["next"] == "continue_verification"
        assert result["verify_effect"]
        pending = store.connection.execute(
            "SELECT kind FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant,)).fetchall()
        store.connection.commit()
        assert [row["kind"] for row in pending] == ["verify_execution"]
        assert engine.get_ticket(ctx, opened["ticket_id"])["state"] == "verifying"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


class _IncomingAdapter:
    def __init__(self, rows):
        self.rows = rows

    def search_read(self, model, domain, fields, limit=100, order=None, extra=None):
        return list(self.rows)


def test_incoming_supply_excludes_received_units() -> None:
    adapter = _IncomingAdapter([
        {"id": 1, "order_id": [8, "P8"], "product_qty": 10.0, "qty_received": 6.0,
         "price_unit": 5.0, "date_planned": "2026-11-10 09:00:00"},
        {"id": 2, "order_id": [9, "P9"], "product_qty": 10.0, "qty_received": 10.0,
         "price_unit": 5.0, "date_planned": "2026-11-10 09:00:00"},
    ])
    commitments = _open_commitments(adapter, 1, 1, 99,
                                    date(2026, 11, 8), 10)
    assert [item.quantity for item in commitments] == [4]


class _OfferAdapter:
    def __init__(self, offer_currency_id):
        self.offer_currency_id = offer_currency_id

    def search_read(self, model, domain, fields, limit=100, order=None, extra=None):
        if model == "product.supplierinfo":
            return [{"id": 1, "partner_id": [5, "Supplier"], "min_qty": 0,
                     "price": 9.0, "delay": 0, "company_id": 1,
                     "currency_id": self.offer_currency_id}]
        if model == "res.currency":
            cid = _first_id(self.offer_currency_id)
            return [{"id": cid, "name": "EUR" if cid == 1 else "USD"}]
        if model == "res.company":
            return [{"id": 1, "currency_id": [1, "EUR"]}]
        return []


def _first_id(value):
    return int(value[0]) if isinstance(value, (list, tuple)) else int(value or 0)


def test_a_foreign_currency_offer_is_rejected() -> None:
    from forgeflow.integrations.odoo20.procurement import _offers

    with pytest.raises(Exception, match="supplier_offer_currency_mismatch"):
        _offers(_OfferAdapter([2, "USD"]), {"product_tmpl_id": [1, "T"]}, 1, "EUR")


def test_a_same_currency_offer_is_accepted() -> None:
    from forgeflow.integrations.odoo20.procurement import _offers

    offers = _offers(_OfferAdapter([1, "EUR"]), {"product_tmpl_id": [1, "T"]}, 1, "EUR")
    assert len(offers) == 1 and offers[0].currency == "EUR"
