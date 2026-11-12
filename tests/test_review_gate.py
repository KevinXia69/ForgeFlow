"""A review gate holds pending work; a closed task is never silently reopened."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Role,
    TenantContext,
    answer_attention_by_id,
    open_attention,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_review_holds_effects_and_closed_ticket_is_not_reopened() -> None:
    tenant = f"review-gate-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Gate", 1, (("p", "P", "planner"),))
        ctx = TenantContext(tenant, "p", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="t", external_event_id="g1",
                                        payload_hash="a" * 64, object_ref="PO-1", goal="gate")
        attention = open_attention(store, ctx, ticket_id=admitted.ticket_id,
                                   need_type="fact", question="Review")
        assert attention is not None
        # The pending effect is parked behind the blocking attention.
        assert engine.claim_effect("w", tenant_id=tenant) is None
        answer_attention_by_id(store, ctx, attention_id=attention)
        assert engine.claim_effect("w", tenant_id=tenant) is not None
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] in {"queued", "running"}
        # Simulate a closed task and verify a duplicate scan cannot reopen it.
        with store.transaction() as cursor:
            cursor.execute("UPDATE control_tickets SET state='closed' WHERE id=%s",
                           (admitted.ticket_id,))
        assert open_attention(store, ctx, ticket_id=admitted.ticket_id,
                              need_type="fact", question="Review") is None
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_non_blocking_attention_does_not_park_the_task() -> None:
    tenant = f"review-nonblocking-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Gate", 1, (("p", "P", "planner"),))
        ctx = TenantContext(tenant, "p", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="t", external_event_id="g2",
                                        payload_hash="b" * 64, object_ref="PO-2", goal="gate")
        attention = engine.open_attention(ctx, ticket_id=admitted.ticket_id,
                                          need_type="budget", question="FYI",
                                          blocking=False)
        assert attention is not None
        assert engine.claim_effect("w", tenant_id=tenant) is not None
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "queued"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
