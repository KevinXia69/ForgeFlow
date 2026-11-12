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
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_dispatcher_marks_worker_exception_failed() -> None:
    tenant = f"dispatch-fail-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Dispatch fail", 1, (("p", "P", "planner"),))
        ctx = TenantContext(tenant, "p", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        admitted = engine.admit_trigger(ctx, source_id="t", external_event_id="f1",
                                        payload_hash="f" * 64, object_ref="PO-1", goal="fail")
        first = engine.claim_effect("setup", tenant_id=tenant)
        engine.complete_effect(effect_id=first["id"], tenant_id=tenant,
                               fencing_token=first["fencing_token"])
        with store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload) "
                "VALUES (%s, %s, %s, 'launch_worker', %s)",
                (str(uuid.uuid4()), tenant, admitted.ticket_id,
                 Jsonb({"capability": "assess", "goal": "fail"})))
        router = Router()
        def raise_error(request):
            raise RuntimeError("dsh_unavailable")
        router.register(CallableWorker("dsh", {"assess"}, raise_error))
        result = Dispatcher(store, router).tick("w", tenant_id=tenant)
        assert result["status"] == "failed"
        assert store.connection.execute(
            "SELECT state FROM control_effects WHERE tenant_id=%s AND kind='launch_worker'",
            (tenant,)).fetchone()["state"] == "failed"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
