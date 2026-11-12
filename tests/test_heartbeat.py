from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.superagent import ControlStore, beat, health

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_service_heartbeat_health_and_staleness() -> None:
    tenant = f"heartbeat-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Heartbeat", 1, (("p", "P", "planner"),))
        beat(store, service_id="source:test", tenant_id=tenant, kind="source",
             detail={"events": 2})
        row = health(store, tenant_id=tenant)[0]
        assert row["stale"] is False and row["detail"]["events"] == 2
        with store.transaction() as cursor:
            cursor.execute("UPDATE control_service_heartbeats SET last_seen=now()-interval '2 minutes' "
                           "WHERE service_id='source:test'")
        assert health(store, tenant_id=tenant)[0]["stale"] is True
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
