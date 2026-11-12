from __future__ import annotations

import os
import time
import uuid

import pytest

from forgeflow.superagent import ControlEngine, ControlStore, LeaseKeeper, Role, TenantContext

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_heartbeat_extends_running_lease() -> None:
    tenant = f"lease-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Lease", 1, (("planner-a", "Planner", "planner"),))
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        engine = ControlEngine(store)
        engine.admit_trigger(ctx, source_id="test", external_event_id="lease-1",
                             payload_hash="c" * 64, object_ref="PO-1", goal="lease")
        effect = engine.claim_effect("worker-a", lease_seconds=1, tenant_id=tenant)
        before = store.connection.execute(
            "SELECT lease_expires_at FROM control_effects WHERE id=%s", (effect["id"],)
        ).fetchone()["lease_expires_at"]
        keeper = LeaseKeeper(store, effect_id=effect["id"], tenant_id=tenant,
                             fencing_token=effect["fencing_token"], interval_seconds=0.1,
                             lease_seconds=1)
        keeper.start()
        time.sleep(0.35)
        keeper.stop()
        after = store.connection.execute(
            "SELECT lease_expires_at FROM control_effects WHERE id=%s", (effect["id"],)
        ).fetchone()["lease_expires_at"]
        assert after > before and not keeper.lost.is_set()
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
