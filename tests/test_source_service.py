"""Subscription scheduling for the source service, now driven by real exceptions."""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Role,
    SourceService,
    TenantContext,
    upsert_subscription,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


class FakeAdapter:
    """One open purchase order that the test cancels to create a real exception."""

    def __init__(self) -> None:
        self.orders = [{"id": 7, "name": "P00007", "state": "purchase",
                        "date_planned": "2026-11-10 10:00:00",
                        "partner_id": [3, "Supplier"], "company_id": [1, "C"]}]

    def search_read(self, model, domain, fields, limit=100, order=None, offset=0):
        if model == "purchase.order.line":
            return [{"product_qty": 10.0, "qty_received": 0.0}]
        return [dict(row) for row in self.orders][offset:offset + limit]


def _store(tenant: str) -> ControlStore:
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Service", 1, (("planner-a", "Planner", "planner"),))
    return store


def _drop(store: ControlStore, tenant: str) -> None:
    with store.connection.transaction():
        store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
    store.close()


def test_source_service_scans_only_when_due_and_reports_no_false_exception() -> None:
    tenant = f"source-service-{uuid.uuid4().hex}"
    store = _store(tenant)
    try:
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        upsert_subscription(store, tenant_id=tenant,
                            source_type="odoo20.purchase_order", interval_seconds=60)
        adapter = FakeAdapter()
        service = SourceService(store, adapter, ctx)
        # A normal open order is not an exception, so it creates no work at all.
        first = service.tick()
        assert first[0]["events"] == 0 and first[0]["tickets"] == 0
        assert service.tick() == []  # not due yet
        later = datetime.now(UTC) + timedelta(seconds=61)
        assert service.tick(now=later)[0]["tickets"] == 0
        # Cancelling the commitment is a real exception.
        adapter.orders[0]["state"] = "cancel"
        fourth = service.tick(now=later + timedelta(seconds=61))
        assert fourth[0]["events"] == 1 and fourth[0]["tickets"] == 1
        # A normal source exception does not create a human "please confirm" task.
        assert store.connection.execute(
            "SELECT count(*) AS count FROM control_attentions WHERE tenant_id=%s",
            (tenant,)).fetchone()["count"] == 0
    finally:
        _drop(store, tenant)


def test_auto_investigate_policy_queues_a_worker_effect() -> None:
    tenant = f"source-policy-{uuid.uuid4().hex}"
    store = _store(tenant)
    try:
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        upsert_subscription(store, tenant_id=tenant,
                            source_type="odoo20.purchase_order", interval_seconds=60,
                            config={"policy": "auto_investigate"})
        adapter = FakeAdapter()
        service = SourceService(store, adapter, ctx)
        service.tick()  # baseline
        adapter.orders[0]["state"] = "cancel"
        later = datetime.now(UTC) + timedelta(seconds=61)
        result = service.tick(now=later)
        assert result[0]["tickets"] == 1
        pending = store.connection.execute(
            "SELECT kind FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant,)).fetchall()
        assert [row["kind"] for row in pending] == ["launch_worker"]
    finally:
        _drop(store, tenant)


def test_observe_policy_records_work_without_a_worker() -> None:
    tenant = f"source-observe-{uuid.uuid4().hex}"
    store = _store(tenant)
    try:
        ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
        upsert_subscription(store, tenant_id=tenant,
                            source_type="odoo20.purchase_order", interval_seconds=60,
                            config={"policy": "observe"})
        adapter = FakeAdapter()
        service = SourceService(store, adapter, ctx)
        service.tick()
        adapter.orders[0]["state"] = "cancel"
        later = datetime.now(UTC) + timedelta(seconds=61)
        assert service.tick(now=later)[0]["tickets"] == 1
        assert store.connection.execute(
            "SELECT count(*) AS count FROM control_effects WHERE tenant_id=%s",
            (tenant,)).fetchone()["count"] == 0
        assert ControlEngine(store).claim_effect("w", tenant_id=tenant) is None
        ticket = store.connection.execute(
            "SELECT state FROM control_tickets WHERE tenant_id=%s", (tenant,)).fetchone()
        assert ticket["state"] == "waiting_event"
    finally:
        _drop(store, tenant)
