"""Stopping a task stops future work; it never pretends an ERP write rolled back."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Role,
    TenantContext,
    cancel_ticket,
    open_attention,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_cancel_stops_work_and_dismisses_attention() -> None:
    tenant = f"cancel-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Cancel", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(
            ctx, source_id="test", external_event_id="cancel-1", payload_hash="d" * 64,
            object_ref="PO-1", goal="cancel")
        attention_id = open_attention(store, ctx, ticket_id=admitted.ticket_id,
                                      need_type="fact", question="Review")
        assert cancel_ticket(store, ctx, ticket_id=admitted.ticket_id) == 1
        ticket = engine.get_ticket(ctx, admitted.ticket_id)
        assert ticket["state"] == "cancelled"
        # A stopped task is not a finished one: the outward state must not claim
        # completion, and the phase says what actually happened.
        assert ticket["user_state"] == "needs_action"
        assert ticket["phase"] == "stopped"
        assert store.connection.execute(
            "SELECT state FROM control_attentions WHERE id=%s", (attention_id,)
        ).fetchone()["state"] == "dismissed"
        # Pending work is cancelled, not failed: nothing was attempted.
        assert store.connection.execute(
            "SELECT state FROM control_effects WHERE ticket_id=%s", (admitted.ticket_id,)
        ).fetchone()["state"] == "cancelled"
        assert engine.claim_effect("worker", tenant_id=tenant) is None
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_cancel_during_an_erp_write_reports_unknown_instead_of_rolled_back() -> None:
    tenant = f"cancel-write-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Cancel write", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(
            ctx, source_id="test", external_event_id="cancel-2", payload_hash="e" * 64,
            object_ref="PO-2", goal="apply")
        initial = engine.claim_effect("setup", tenant_id=tenant)
        engine.complete_effect(effect_id=initial["id"], tenant_id=tenant,
                               fencing_token=initial["fencing_token"])
        with store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload, "
                "side_effect_class) VALUES (%s, %s, %s, 'apply_approved_plan', "
                "'{}'::jsonb, 'business_write')",
                (str(uuid.uuid4()), tenant, admitted.ticket_id))
        write = engine.claim_effect("worker", lease_seconds=30, tenant_id=tenant)
        assert write["side_effect_class"] == "business_write"
        cancel_ticket(store, ctx, ticket_id=admitted.ticket_id)
        ticket = engine.get_ticket(ctx, admitted.ticket_id)
        assert ticket["state"] == "blocked"
        assert ticket["user_state"] == "needs_action"
        assert store.connection.execute(
            "SELECT need_type FROM control_attentions WHERE tenant_id=%s AND state='open'",
            (tenant,)).fetchone()["need_type"] == "result_unknown"
        row = store.connection.execute(
            "SELECT state, reconcile_state FROM control_effects WHERE id=%s",
            (write["id"],)).fetchone()
        assert row["state"] == "reconcile_required" and row["reconcile_state"] == "unknown"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
