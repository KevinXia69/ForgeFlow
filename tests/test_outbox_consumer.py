"""The addon outbox is drained reliably: admit first, mark consumed after."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    OutboxConsumer,
    Role,
    TenantContext,
    link_plan,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


class FakeOutbox:
    def __init__(self, rows):
        self.rows = rows
        self.writes = []
        self.fail_mark = False

    def search_read(self, model, domain, fields, limit=100, order=None):
        assert model == "forgeflow.event_outbox"
        return [dict(row) for row in self.rows
                if row.get("state") == "pending"][:limit]

    def write(self, model, ids, values):
        if self.fail_mark:
            raise RuntimeError("odoo_unavailable")
        self.writes.append((list(ids), dict(values)))
        for row in self.rows:
            if row["id"] in ids:
                row.update(values)
        return True


def _tenant(prefix):
    tenant = f"{prefix}-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, prefix, 1, (("planner-a", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    return tenant, store, ctx


def _row(identifier, event_type, *, payload=None, external=None):
    return {"id": identifier, "event_type": event_type,
            "aggregate_model": "forgeflow.execution_operation",
            "aggregate_id": identifier,
            "external_event_id": external or f"outbox:{identifier}",
            "payload_json": json.dumps(payload or {}),
            "state": "pending", "created_at": "2026-11-08 10:00:00"}


def _ticket_count(store, tenant):
    return store.connection.execute(
        "SELECT count(*) AS count FROM control_tickets WHERE tenant_id=%s",
        (tenant,)).fetchone()["count"]


def test_a_self_event_is_consumed_without_opening_a_ticket() -> None:
    tenant, store, ctx = _tenant("outbox-self")
    try:
        adapter = FakeOutbox([_row(1, "execution.applied",
                                   payload={"plan_id": 999, "operation_id": 7})])
        result = OutboxConsumer(store, adapter, ctx).tick()
        assert result["consumed"] == 1
        assert result["admitted"] == []
        assert _ticket_count(store, tenant) == 0
        assert adapter.rows[0]["state"] == "consumed"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_a_business_event_admits_one_ticket_then_marks_consumed() -> None:
    tenant, store, ctx = _tenant("outbox-work")
    try:
        adapter = FakeOutbox([_row(2, "purchase.cancelled",
                                   payload={"object_ref": "purchase.order:5",
                                            "goal": "Recover supply"})])
        result = OutboxConsumer(store, adapter, ctx).tick()
        assert len(result["admitted"]) == 1
        assert _ticket_count(store, tenant) == 1
        assert adapter.rows[0]["state"] == "consumed"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_a_failed_mark_redelivers_without_a_second_ticket() -> None:
    tenant, store, ctx = _tenant("outbox-retry")
    try:
        adapter = FakeOutbox([_row(3, "purchase.delayed",
                                   payload={"object_ref": "purchase.order:6"})])
        adapter.fail_mark = True
        first = OutboxConsumer(store, adapter, ctx).tick()
        assert first["failures"] and adapter.rows[0]["state"] == "pending"
        assert _ticket_count(store, tenant) == 1
        adapter.fail_mark = False
        second = OutboxConsumer(store, adapter, ctx).tick()
        assert second["admitted"] and _ticket_count(store, tenant) == 1
        assert adapter.rows[0]["state"] == "consumed"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_an_unknown_event_stays_pending_and_is_reported() -> None:
    tenant, store, ctx = _tenant("outbox-unknown")
    try:
        adapter = FakeOutbox([_row(4, "something.new")])
        result = OutboxConsumer(store, adapter, ctx).tick()
        assert result["unsupported"] == [{"id": 4, "event_type": "something.new"}]
        assert result["consumed"] == 0
        assert adapter.rows[0]["state"] == "pending"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_a_self_event_is_linked_to_the_original_plan_ticket() -> None:
    tenant, store, ctx = _tenant("outbox-link")
    try:
        linked = link_plan(store, ctx, plan_id=77, goal="Approve the plan")
        adapter = FakeOutbox([_row(5, "execution.applied",
                                   payload={"plan_id": 77, "operation_id": 9,
                                            "action_id": "cancel"})])
        result = OutboxConsumer(store, adapter, ctx).tick()
        assert result["self_linked"][0]["ticket"] == linked.ticket_id
        kinds = [row["kind"] for row in ControlEngine(store).ticket_events(
            ctx, linked.ticket_id)]
        assert "execution_applied" in kinds
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_a_failed_timeline_write_keeps_the_self_event_pending(monkeypatch) -> None:
    tenant, store, ctx = _tenant("outbox-timeline")
    try:
        link_plan(store, ctx, plan_id=78, goal="Approve the plan")
        adapter = FakeOutbox([_row(6, "execution.applied",
                                   payload={"plan_id": 78, "operation_id": 10})])
        consumer = OutboxConsumer(store, adapter, ctx)
        def fail(*args, **kwargs):
            raise RuntimeError("control_db_unavailable")
        monkeypatch.setattr(consumer.engine, "append_event", fail)
        result = consumer.tick()
        assert result["consumed"] == 0
        assert result["failures"][0]["id"] == 6
        assert adapter.rows[0]["state"] == "pending"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
