"""Recovery handlers: an unknown ERP write is reconciled, never replayed."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.integrations.odoo20.business import PreparedRecovery
from forgeflow.integrations.odoo20.executor import ExecutionResult
from forgeflow.superagent import ControlEngine, ControlStore, Role, TenantContext
from forgeflow.superagent.dispatcher import EffectContext
from forgeflow.superagent.plan_effects import PlanEffects
from forgeflow.superagent.tasks import link_plan_to_ticket

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


class _Adapter:
    def __init__(self, operations):
        self.operations = operations

    def search_read(self, model, domain, fields, limit=100, order=None, offset=0):
        if model == "forgeflow.execution_operation":
            return list(self.operations)
        return []


class _Business:
    def __init__(self, state="unknown", apply_error=False):
        self.prepared = PreparedRecovery(
            exception_id=5, plan_id=77, plan_hash="a" * 64, snapshot_hash="b" * 64,
            facts={}, execution_identity="identity:77", plan_state="approved", actions=())
        self.reconcile_state = state
        self.apply_error = apply_error
        self.reconciled: list[str] = []
        self.adapter = _Adapter([{"id": 1, "action_id": "buy", "state": "unknown",
                                  "idempotency_key": "plan:abc:buy"}])

    def load_prepared(self, plan_id):
        return self.prepared

    def reconcile(self, prepared, *, idempotency_key):
        self.reconciled.append(idempotency_key)
        return ExecutionResult(operation_id=1, state=self.reconcile_state,
                               readback={"state": self.reconcile_state}, receipt_id=0)

    def apply_plan(self, prepared):
        if self.apply_error:
            raise TimeoutError("lost response")
        return ExecutionResult(operation_id=1, state="succeeded", readback={}, receipt_id=0)


def _seed():
    tenant = f"recovery-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Recovery", 1, (("planner", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner", frozenset({Role.PLANNER}), 1)
    engine = ControlEngine(store)
    opened = engine.open_task(ctx, object_ref="purchase.order:9", goal="recover",
                              state="running")
    link_plan_to_ticket(store, ctx, plan_id=77, ticket_id=opened["ticket_id"])
    return tenant, store, engine, ctx, opened["ticket_id"]


def _context(engine, ctx, ticket_id, plan_id=77):
    return EffectContext(effect={"id": "e1"}, ticket={"id": ticket_id},
                         payload={"plan_id": plan_id}, ctx=ctx, tenant_id=ctx.tenant_id,
                         ticket_id=ticket_id, fencing_token="f",
                         cancel_check=lambda: False, engine=engine)


def _cleanup(store, tenant):
    with store.connection.transaction():
        store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
    store.close()


def test_a_settled_reconcile_continues_to_verification() -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business(state="succeeded")
    try:
        outcome = PlanEffects(business).reconcile_execution(
            _context(engine, ctx, ticket_id))
        assert outcome["status"] == "completed"
        assert outcome["operations"] == [{"action_id": "buy", "state": "succeeded"}]
        assert engine.get_ticket(ctx, ticket_id)["state"] == "verifying"
        pending = store.connection.execute(
            "SELECT kind FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant,)).fetchall()
        store.connection.commit()
        assert [row["kind"] for row in pending] == ["verify_execution"]
    finally:
        _cleanup(store, tenant)


def test_an_unresolved_reconcile_blocks_and_never_replays() -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business(state="unknown")
    try:
        outcome = PlanEffects(business).reconcile_execution(
            _context(engine, ctx, ticket_id))
        assert outcome["status"] == "failed" and outcome["state"] == "unknown"
        assert engine.get_ticket(ctx, ticket_id)["state"] == "blocked"
        needs = [row["need_type"] for row in store.connection.execute(
            "SELECT need_type FROM control_attentions WHERE tenant_id=%s "
            "AND ticket_id=%s AND state='open'", (tenant, ticket_id)).fetchall()]
        store.connection.commit()
        assert "result_unknown" in needs
        # Reconcile reads the original operation key; it does not apply again.
        assert business.reconciled == ["plan:abc:buy"]
    finally:
        _cleanup(store, tenant)


def test_a_lost_apply_response_queues_a_reconcile_effect() -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business(apply_error=True)
    try:
        outcome = PlanEffects(business).apply_approved_plan(
            _context(engine, ctx, ticket_id))
        assert outcome["status"] == "failed" and outcome["state"] == "unknown"
        assert outcome["reconcile_effect"]
        row = store.connection.execute(
            "SELECT kind, state FROM control_effects WHERE tenant_id=%s AND id=%s",
            (tenant, outcome["reconcile_effect"])).fetchone()
        store.connection.commit()
        assert row["kind"] == "reconcile_execution" and row["state"] == "pending"
        assert engine.get_ticket(ctx, ticket_id)["state"] == "blocked"
    finally:
        _cleanup(store, tenant)
