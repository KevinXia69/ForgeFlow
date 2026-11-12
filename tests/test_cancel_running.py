"""A worker result that arrives after a stop must not describe the task as complete."""
from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from psycopg.types.json import Jsonb

from forgeflow.superagent import (
    CallableWorker,
    ControlEngine,
    ControlStore,
    Dispatcher,
    Role,
    Router,
    TenantContext,
    WorkerResult,
    cancel_ticket,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_cancelled_running_worker_result_is_discarded() -> None:
    tenant = f"cancel-running-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Cancel running", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="running-1",
                                        payload_hash="e" * 64, object_ref="PO-1", goal="running")
        first = engine.claim_effect("setup", tenant_id=tenant)
        engine.complete_effect(effect_id=first["id"], tenant_id=tenant,
                               fencing_token=first["fencing_token"])
        with store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload, "
                "side_effect_class) VALUES (%s, %s, %s, 'launch_worker', %s, "
                "'model_inference')",
                (str(uuid.uuid4()), tenant, admitted.ticket_id,
                 Jsonb({"capability": "assess", "goal": "slow"})))
        router = Router()
        router.register(CallableWorker("assess", {"assess"}, lambda request: (
            time.sleep(0.4) or WorkerResult("completed", "late"))))
        dispatcher = Dispatcher(store, router)
        holder = {}
        thread = threading.Thread(target=lambda: holder.setdefault(
            "result", dispatcher.tick("worker-1", tenant_id=tenant, lease_seconds=1)))
        thread.start()
        time.sleep(0.1)
        cancel_ticket(store, ctx, ticket_id=admitted.ticket_id)
        thread.join()
        assert holder["result"]["status"] in {"cancelled", "discarded"}
        ticket = engine.get_ticket(ctx, admitted.ticket_id)
        assert ticket["state"] == "cancelled"
        kinds = [row["kind"] for row in engine.ticket_events(ctx, admitted.ticket_id)]
        assert "worker_result" not in kinds
        assert any(kind in {"worker_result_rejected", "task_cancelled"} for kind in kinds)
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_late_assignment_result_cannot_overwrite_a_newer_state() -> None:
    tenant = f"late-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Late", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="late-1",
                                        payload_hash="f" * 64, object_ref="PO-9", goal="late")
        effect = engine.claim_effect("worker", lease_seconds=30, tenant_id=tenant)
        assignment = engine.start_assignment(
            ctx, ticket_id=admitted.ticket_id, capability="assess", runner="dsh",
            fencing_token=effect["fencing_token"])
        # A newer attempt supersedes this one.
        with store.transaction() as cursor:
            cursor.execute(
                "UPDATE control_effects SET fencing_token=%s WHERE id=%s",
                (str(uuid.uuid4()), effect["id"]))
        from forgeflow.superagent.engine import LateResultRejected

        with pytest.raises(LateResultRejected):
            engine.record_worker_result(
                ctx, ticket_id=admitted.ticket_id, assignment_id=assignment,
                report={"status": "completed", "summary": "stale"},
                fencing_token=effect["fencing_token"])
        row = store.connection.execute(
            "SELECT state, rejected_reason FROM control_assignments WHERE id=%s",
            (assignment,)).fetchone()
        assert row["state"] == "rejected" and row["rejected_reason"] == "fencing_token_stale"
        assert "worker_result" not in [
            item["kind"] for item in engine.ticket_events(ctx, admitted.ticket_id)]
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
