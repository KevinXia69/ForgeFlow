"""Lease recovery, side-effect classification and real progress monitoring."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    EffectWatchdog,
    ProcessWatchdog,
    Role,
    TenantContext,
    retry_failed_ticket,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _setup(prefix: str):
    tenant = f"{prefix}-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Watchdog", 1, (("planner-a", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    return tenant, store, ctx, ControlEngine(store)


def _teardown(store, tenant):
    with store.connection.transaction():
        store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
    store.close()


def _expire_lease(store, effect_id):
    with store.transaction() as cursor:
        cursor.execute(
            "UPDATE control_effects SET lease_expires_at=now() - interval '1 minute' "
            "WHERE id=%s RETURNING clock_timestamp() AS moment", (effect_id,))
        return cursor.fetchone()["moment"]


def test_read_only_effect_is_recovered_then_fails_exhausted() -> None:
    tenant, store, ctx, engine = _setup("watchdog")
    try:
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="w1",
                                        payload_hash="b" * 64, object_ref="PO-1", goal="watch")
        effect = engine.claim_effect("worker-a", lease_seconds=1, tenant_id=tenant)
        moment = _expire_lease(store, effect["id"])
        result = EffectWatchdog(store, max_attempts=2, backoff_seconds=0).reap_expired(
            tenant_id=tenant, now=moment)
        assert result["recovered"] == 1 and result["failed"] == 0
        # Claiming uses the database clock. Use that same clock for the zero
        # backoff so a host/VM clock offset cannot defer the immediate retry.
        retried_effect = engine.claim_effect("worker-a", lease_seconds=1, tenant_id=tenant)
        assert retried_effect is not None and retried_effect["id"] == effect["id"]
        moment = _expire_lease(store, effect["id"])
        result = EffectWatchdog(store, max_attempts=2, backoff_seconds=0).reap_expired(
            tenant_id=tenant, now=moment)
        assert result["failed"] == 1
        attention = store.connection.execute(
            "SELECT need_type, state FROM control_attentions WHERE tenant_id=%s",
            (tenant,)).fetchone()
        assert attention["need_type"] == "failure" and attention["state"] == "open"
        ticket = engine.get_ticket(ctx, admitted.ticket_id)
        assert ticket["state"] == "blocked" and ticket["user_state"] == "needs_action"
        events = engine.ticket_events(ctx, effect["ticket_id"])
        assert any(row["kind"] == "lease_recovered" for row in events)
        assert retry_failed_ticket(store, ctx, ticket_id=effect["ticket_id"]) == 1
        retried = store.connection.execute(
            "SELECT state, attempt FROM control_effects WHERE id=%s", (effect["id"],)).fetchone()
        assert retried["state"] == "pending" and retried["attempt"] == 0
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "queued"
        assert any(row["kind"] == "operator_retry"
                   for row in engine.ticket_events(ctx, effect["ticket_id"]))
    finally:
        _teardown(store, tenant)


def test_business_write_with_lost_lease_is_reconciled_not_replayed() -> None:
    """The single most important recovery rule: an unknown ERP write is never retried."""
    tenant, store, ctx, engine = _setup("watchdog-write")
    try:
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="w2",
                                        payload_hash="c" * 64, object_ref="PO-2", goal="apply")
        initial = engine.claim_effect("setup", tenant_id=tenant)
        engine.complete_effect(effect_id=initial["id"], tenant_id=tenant,
                               fencing_token=initial["fencing_token"])
        with store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload, "
                "side_effect_class) VALUES (%s, %s, %s, 'apply_approved_plan', "
                "'{}'::jsonb, 'business_write')",
                (str(uuid.uuid4()), tenant, admitted.ticket_id))
        write = engine.claim_effect("worker-b", lease_seconds=1, tenant_id=tenant)
        assert write["side_effect_class"] == "business_write"
        _expire_lease(store, write["id"])
        result = EffectWatchdog(store, max_attempts=5, backoff_seconds=0).reap_expired(
            tenant_id=tenant)
        assert result["reconciling"] == 1 and result["recovered"] == 0
        row = store.connection.execute(
            "SELECT state, reconcile_state FROM control_effects WHERE id=%s",
            (write["id"],)).fetchone()
        assert row["state"] == "reconcile_required"
        assert row["reconcile_state"] == "unknown"
        # It is not pending, so the dispatcher cannot pick it up again.
        assert store.connection.execute(
            "SELECT count(*) AS count FROM control_effects "
            "WHERE tenant_id=%s AND state='pending' AND kind='apply_approved_plan'",
            (tenant,)).fetchone()["count"] == 0
        attention = store.connection.execute(
            "SELECT need_type FROM control_attentions WHERE tenant_id=%s AND state='open'",
            (tenant,)).fetchone()
        assert attention["need_type"] == "result_unknown"
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "blocked"
        pending = EffectWatchdog(store).reconcile_required(tenant_id=tenant)
        assert [item["effect_id"] for item in pending] == [write["id"]]
        resolved = EffectWatchdog(store).resolve_reconcile(
            tenant_id=tenant, effect_id=write["id"], confirmed=True,
            detail={"receipt": "R-1"})
        assert resolved["confirmed_applied"] is True
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "verifying"
    finally:
        _teardown(store, tenant)


def test_stalled_ticket_is_stopped_by_the_progress_watchdog() -> None:
    tenant, store, ctx, engine = _setup("watchdog-progress")
    try:
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="w3",
                                        payload_hash="d" * 64, object_ref="PO-3", goal="watch")
        engine.transition_ticket(ctx, ticket_id=admitted.ticket_id, to_state="running")
        with store.transaction() as cursor:
            cursor.execute(
                "UPDATE control_tickets SET progress_at=now() - interval '1 hour' WHERE id=%s",
                (admitted.ticket_id,))
        result = EffectWatchdog(store, stagnation_seconds=60).check_progress(tenant_id=tenant)
        assert result["stalled"] == 1
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "blocked"
        needs = [row["need_type"] for row in engine.list_attentions(ctx)]
        assert needs == ["budget"]
    finally:
        _teardown(store, tenant)


def test_parked_ticket_is_not_reported_as_stalled() -> None:
    tenant, store, ctx, engine = _setup("watchdog-parked")
    try:
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="w4",
                                        payload_hash="e" * 64, object_ref="PO-4", goal="watch")
        engine.open_attention(ctx, ticket_id=admitted.ticket_id, need_type="fact",
                              question="Which budget applies?")
        with store.transaction() as cursor:
            cursor.execute(
                "UPDATE control_tickets SET progress_at=now() - interval '1 hour' WHERE id=%s",
                (admitted.ticket_id,))
        result = EffectWatchdog(store, stagnation_seconds=60).check_progress(tenant_id=tenant)
        assert result["stalled"] == 0
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "waiting_input"
    finally:
        _teardown(store, tenant)


def test_token_budget_exhaustion_blocks_the_ticket() -> None:
    tenant, store, ctx, engine = _setup("watchdog-budget")
    try:
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="w5",
                                        payload_hash="f" * 64, object_ref="PO-5", goal="watch")
        with store.transaction() as cursor:
            cursor.execute(
                "UPDATE control_tickets SET budget_tokens=100, spent_tokens=250, "
                "state='running' WHERE id=%s", (admitted.ticket_id,))
        result = EffectWatchdog(store).check_progress(tenant_id=tenant)
        assert result["budget_exhausted"] == 1
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "blocked"
        assert [row["need_type"] for row in engine.list_attentions(ctx)] == ["budget"]
    finally:
        _teardown(store, tenant)


def test_process_watchdog_reports_a_stale_service() -> None:
    tenant, store, ctx, engine = _setup("watchdog-proc")
    try:
        from forgeflow.superagent import beat

        beat(store, service_id="source:x", tenant_id=tenant, kind="source")
        assert ProcessWatchdog(store, stale_seconds=60).status(tenant_id=tenant)["ok"] is True
        with store.transaction() as cursor:
            cursor.execute(
                "UPDATE control_service_heartbeats SET last_seen=now() - interval '10 minutes' "
                "WHERE service_id='source:x'")
        status = ProcessWatchdog(store, stale_seconds=60).status(tenant_id=tenant)
        assert status["ok"] is False and status["stale"] == ["source:x"]
    finally:
        _teardown(store, tenant)


def test_expired_mandates_are_closed_server_side() -> None:
    tenant, store, ctx, engine = _setup("watchdog-mandate")
    try:
        mandate = engine.issue_mandate(ctx, object_refs=("PO-9",), ttl_seconds=1)
        with store.transaction() as cursor:
            cursor.execute(
                "UPDATE control_mandates SET deadline_at=now() - interval '1 minute' "
                "WHERE id=%s", (mandate.id,))
        assert EffectWatchdog(store).expire_mandates() >= 1
        assert engine.get_mandate(ctx, mandate.id).state == "expired"
    finally:
        _teardown(store, tenant)


def test_system_attention_is_retryable() -> None:
    tenant, store, ctx, engine = _setup("watchdog-system")
    try:
        admitted = engine.admit_trigger(
            ctx, source_id="test", external_event_id="ws1",
            payload_hash="c" * 64, object_ref="PO-2", goal="watch")
        engine.open_attention(
            ctx, ticket_id=admitted.ticket_id, need_type="system",
            question="Solver timeout; retry", detail={"code": "manufacturing_incomplete"})
        with store.connection.transaction():
            store.connection.execute(
                "UPDATE control_tickets SET state='blocked' WHERE tenant_id=%s AND id=%s",
                (tenant, admitted.ticket_id))
        retry_failed_ticket(store, ctx, ticket_id=admitted.ticket_id)
        attention = store.connection.execute(
            "SELECT state FROM control_attentions WHERE tenant_id=%s AND ticket_id=%s",
            (tenant, admitted.ticket_id)).fetchone()
        store.connection.commit()
        assert attention["state"] == "answered"
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "queued"
    finally:
        _teardown(store, tenant)
