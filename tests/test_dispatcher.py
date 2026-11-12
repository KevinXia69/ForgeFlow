"""Live dispatcher test: a worker result is a result, not an approval request."""
from __future__ import annotations

import os
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
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _prepare_worker_effect(store, engine, ctx, tenant, ticket_id, payload=None):
    """Consume the admission effect and queue a launch_worker effect."""
    first = engine.claim_effect("setup", tenant_id=tenant)
    engine.complete_effect(effect_id=first["id"], tenant_id=tenant,
                           fencing_token=first["fencing_token"])
    with store.transaction() as cursor:
        cursor.execute(
            "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload, "
            "side_effect_class) VALUES (%s, %s, %s, 'launch_worker', %s, 'model_inference')",
            (str(uuid.uuid4()), tenant, ticket_id,
             Jsonb(payload or {"capability": "assess", "goal": "check", "input": {"x": 1}})))


def test_completed_worker_result_creates_no_human_attention() -> None:
    tenant = f"dispatch-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Dispatch", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="dispatch-1",
                                        payload_hash="a" * 64, object_ref="PO-1",
                                        goal="investigate")
        _prepare_worker_effect(store, engine, ctx, tenant, admitted.ticket_id)
        router = Router()
        router.register(CallableWorker("dsh", {"assess"}, lambda request: WorkerResult(
            "completed", "checked", findings=({"order": "SO1"},),
            usage={"tokens": 5}, usage_complete=True)))
        result = Dispatcher(store, router).tick("dispatcher-1", tenant_id=tenant)
        assert result is not None and result["status"] == "completed"
        assert engine.pending_effect_count(tenant) == 0
        events = engine.ticket_events(ctx, admitted.ticket_id)
        assert any(row["kind"] == "effect_completed" for row in events)
        assert any(row["kind"] == "worker_result" for row in events)
        # Plan section 13.1: a normal successful result must not block on a human.
        attention = store.connection.execute(
            "SELECT count(*) AS count FROM control_attentions WHERE tenant_id=%s",
            (tenant,)).fetchone()
        assert int(attention["count"]) == 0
        ticket = engine.get_ticket(ctx, admitted.ticket_id)
        assert ticket["state"] == "result_ready"
        assert ticket["user_state"] == "investigating"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_worker_needing_input_opens_one_blocking_attention() -> None:
    tenant = f"dispatch-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Dispatch", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="d2",
                                        payload_hash="b" * 64, object_ref="PO-2",
                                        goal="investigate")
        _prepare_worker_effect(store, engine, ctx, tenant, admitted.ticket_id)
        router = Router()
        router.register(CallableWorker("dsh", {"assess"}, lambda request: WorkerResult(
            "needs_input", "missing budget",
            unknowns=({"field": "budget", "object": "SO-9", "why_blocking": "needed"},))))
        Dispatcher(store, router).tick("dispatcher-1", tenant_id=tenant)
        attentions = engine.list_attentions(ctx)
        assert len(attentions) == 1
        assert attentions[0]["need_type"] == "fact"
        assert engine.get_ticket(ctx, admitted.ticket_id)["user_state"] == "needs_input"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_dispatcher_runs_under_the_ticket_mandate_not_an_admin_identity() -> None:
    tenant = f"dispatch-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Dispatch", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="d3",
                                        payload_hash="c" * 64, object_ref="PO-3",
                                        goal="investigate")
        seen = {}
        _prepare_worker_effect(store, engine, ctx, tenant, admitted.ticket_id)

        def run(request):
            seen["mandate"] = request.mandate_id
            seen["run_token"] = request.run_token
            return WorkerResult("completed", "ok", usage={"tokens": 1})

        router = Router()
        router.register(CallableWorker("dsh", {"assess"}, run))
        Dispatcher(store, router).tick("dispatcher-1", tenant_id=tenant)
        mandate = engine.get_mandate(ctx, seen["mandate"])
        assert mandate.principal_id == "planner-a"
        assert mandate.object_refs == frozenset({"PO-3"})
        assert seen["run_token"], "the assignment must carry the effect fencing token"
        service = engine.service_context_for_ticket(
            tenant_id=tenant, ticket_id=admitted.ticket_id, service_id="dispatcher:x")
        assert service.principal_id == "planner-a" and service.is_service
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_unknown_effect_kind_fails_closed_and_is_recorded() -> None:
    tenant = f"dispatch-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Dispatch", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="d4",
                                        payload_hash="d" * 64, object_ref="PO-4",
                                        goal="investigate")
        with store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload) "
                "VALUES (%s, %s, %s, 'not_a_real_effect', '{}'::jsonb)",
                (str(uuid.uuid4()), tenant, admitted.ticket_id))
        result = Dispatcher(store, Router()).tick("dispatcher-1", tenant_id=tenant)
        # The admission effect is claimed first; drain until the bad one is reached.
        results = [result]
        while result is not None:
            result = Dispatcher(store, Router()).tick("dispatcher-1", tenant_id=tenant)
            results.append(result)
        assert any(item and item.get("error") == "unsupported_effect" for item in results)
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_a_worker_cannot_open_an_approval_before_a_plan_exists() -> None:
    """Approval is bound to a plan digest; a worker recommendation is not one."""
    tenant = f"dispatch-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Dispatch", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="test", external_event_id="dispatch-9",
                                        payload_hash="9" * 64, object_ref="purchase.order:9",
                                        goal="recover supply")
        _prepare_worker_effect(store, engine, ctx, tenant, admitted.ticket_id)
        router = Router()
        router.register(CallableWorker("dsh", {"assess"}, lambda request: WorkerResult(
            "completed", "the purchase is cancelled",
            attention=({"need": "approval",
                        "question": "Approve a replacement purchase?"},),
            plan_request={"objective": "replace", "facts": {"purchase_order_id": 9}},
            usage={"tokens": 5}, usage_complete=True)))
        result = Dispatcher(store, router).tick("dispatcher-1", tenant_id=tenant)
        assert result is not None and result["status"] == "completed"
        attention = store.connection.execute(
            "SELECT count(*) AS count FROM control_attentions WHERE tenant_id=%s",
            (tenant,)).fetchone()
        assert int(attention["count"]) == 0, "a worker must not create an approval"
        pending = store.connection.execute(
            "SELECT kind FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant,)).fetchall()
        assert [str(row["kind"]) for row in pending] == ["prepare_plan"]
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "result_ready"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
