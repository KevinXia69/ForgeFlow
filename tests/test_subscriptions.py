from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.superagent import (
    ControlStore,
    SubscriptionError,
    list_subscriptions,
    upsert_subscription,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_subscription_upsert_and_invalid_source() -> None:
    tenant = f"subs-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Subs", 1, (("planner-a", "Planner", "planner"),))
        row = upsert_subscription(store, tenant_id=tenant,
                                  source_type="odoo20.purchase_order", interval_seconds=30)
        assert row["interval_seconds"] == 30 and row["enabled"] is True
        rows = list_subscriptions(store, tenant)
        assert len(rows) == 1 and rows[0]["source_type"] == "odoo20.purchase_order"
        with pytest.raises(SubscriptionError):
            upsert_subscription(store, tenant_id=tenant, source_type="unknown")
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
