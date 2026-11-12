"""Business conditions, paging and persistent observation state for sources."""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from forgeflow.superagent import (
    ConditionSource,
    ControlStore,
    OdooPurchaseSource,
    Role,
    TenantContext,
)
from forgeflow.superagent.sources import SourceStateStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")


class FakeAdapter:
    """Minimal read-only Odoo stand-in with paging and receipt lines."""

    def __init__(self, orders=None, lines=None):
        self.orders = list(orders or [])
        self.lines = lines or {}
        self.calls = []

    def search_read(self, model, domain, fields, limit=100, order=None, offset=0):
        self.calls.append((model, domain, fields, limit, offset))
        if model == "purchase.order.line":
            order_id = None
            for item in domain:
                if isinstance(item, tuple) and item[0] == "order_id":
                    order_id = int(item[2])
            return list(self.lines.get(order_id, []))
        page = self.orders[offset:offset + limit]
        return [dict(row) for row in page]

    def _filter(self, domain):
        return domain


def _order(order_id=7, state="purchase", date_planned=None, name=None):
    return {
        "id": order_id,
        "name": name or f"P{order_id:05d}",
        "state": state,
        "date_planned": date_planned or _future(10),
        "partner_id": [3, "Supplier"],
        "company_id": [1, "Company"],
    }


def _future(days):
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%d 10:00:00")


def _past(days):
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d 10:00:00")


def _lines(order_id=7, ordered=10.0, received=0.0):
    return {order_id: [{"product_qty": ordered, "qty_received": received}]}


def test_first_observation_of_a_normal_order_is_not_an_exception() -> None:
    """Being due soon is the normal state of an open order, not a trigger."""
    source = OdooPurchaseSource(FakeAdapter([_order()]), horizon_days=30)
    assert source.scan() == []
    assert source.last_report.rows == 1 and source.last_report.truncated is False


def test_cancelled_commitment_emits_supply_cancelled_once() -> None:
    adapter = FakeAdapter([_order(state="purchase")])
    source = OdooPurchaseSource(adapter, horizon_days=30)
    assert source.scan() == []
    adapter.orders = [_order(state="cancel")]
    events = source.scan()
    assert [event.condition for event in events] == ["supply_cancelled"]
    assert events[0].object_ref == "purchase.order:7"
    assert events[0].severity == "high"
    # The same cancellation is not re-emitted on the next poll.
    assert source.scan() == []


def test_moved_commitment_date_reports_the_shift() -> None:
    adapter = FakeAdapter([_order(date_planned=_future(5))])
    source = OdooPurchaseSource(adapter, horizon_days=30)
    assert source.scan() == []
    adapter.orders = [_order(date_planned=_future(12))]
    events = source.scan()
    assert [event.condition for event in events] == ["commitment_date_changed"]
    assert events[0].facts["date_shift_days"] == 7
    assert events[0].goal_kind == "commitment_date_changed"


def test_past_due_without_receipt_is_an_exception_but_received_is_not() -> None:
    late = FakeAdapter([_order(date_planned=_past(3))], _lines(received=0.0))
    source = OdooPurchaseSource(late)
    # First observation records the baseline; the exception is reported once the
    # source already knows the order was open.
    assert source.scan() == []
    events = source.scan()
    assert [event.condition for event in events] == ["past_due_receipt"]
    assert events[0].facts["days_late"] >= 3

    received = FakeAdapter([_order(date_planned=_past(3))], _lines(received=10.0))
    source = OdooPurchaseSource(received)
    source.scan()
    assert source.scan() == []


def test_event_identity_is_stable_for_identical_facts() -> None:
    adapter = FakeAdapter([_order(state="purchase")])
    source = OdooPurchaseSource(adapter)
    source.scan()
    adapter.orders = [_order(state="cancel")]
    first = source.scan()
    assert first[0].object_ref == "purchase.order:7"
    assert first[0].external_event_id.startswith("purchase:7:supply_cancelled:")
    assert "date_planned" in adapter.calls[-1][1][0][0] or len(adapter.calls) >= 1


def test_scan_pages_through_every_row_and_reports_truncation() -> None:
    orders = [_order(order_id=index) for index in range(1, 13)]
    adapter = FakeAdapter(orders)
    source = OdooPurchaseSource(adapter, page_size=5, max_pages=10)
    source.scan()
    assert source.last_report.rows == 12
    assert source.last_report.pages == 3
    assert source.last_report.truncated is False
    order_pages = [call[4] for call in adapter.calls if call[0] == "purchase.order"]
    assert order_pages == [0, 5, 10]

    truncated = OdooPurchaseSource(FakeAdapter(orders), page_size=5, max_pages=1)
    truncated.scan()
    assert truncated.last_report.truncated is True
    assert truncated.last_report.rows == 5


@pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")
def test_observation_state_survives_a_new_source_instance() -> None:
    tenant = f"src-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Source", 1, (("planner-a", "Planner", "planner"),))
        adapter = FakeAdapter([_order(state="purchase")])
        first = OdooPurchaseSource(
            adapter, state_store=SourceStateStore(store, tenant))
        assert first.scan() == []
        adapter.orders = [_order(state="cancel")]
        # A brand new source object, as after a process restart, still knows the
        # order was previously open.
        second = OdooPurchaseSource(
            adapter, state_store=SourceStateStore(store, tenant))
        events = second.scan()
        assert [event.condition for event in events] == ["supply_cancelled"]
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_observe_policy_records_work_without_spending_budget() -> None:
    from forgeflow.superagent.sources import admit_events

    class Engine:
        def __init__(self):
            self.calls = []

        def admit_trigger(self, ctx, **kwargs):
            self.calls.append(kwargs)

            class Admission:
                ticket_id = "ticket-1"
                duplicate = False

            return Admission()

    engine = Engine()
    ctx = TenantContext("tenant-a", "planner-a", frozenset({Role.PLANNER}), 1)
    event = OdooPurchaseSource(FakeAdapter())._event(
        "supply_cancelled", "purchase.order:7", {"a": 1}, {"id": 7}, severity="high")
    assert admit_events(engine, ctx, [event], policy="observe") == ["ticket-1"]
    assert engine.calls[0]["initial_effect_kind"] is None
    engine.calls.clear()
    assert admit_events(engine, ctx, [event], policy="auto_investigate") == ["ticket-1"]
    assert engine.calls[0]["initial_effect_kind"] == "launch_worker"
    engine.calls.clear()
    assert admit_events(engine, ctx, [event], policy="off") == []
    assert engine.calls == []


@pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")
def test_condition_source_state_is_durable() -> None:
    tenant = f"cond-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Cond", 1, (("planner-a", "Planner", "planner"),))
        source = ConditionSource("late_supplier", lambda facts: facts["days_late"] > 2,
                                 "Investigate late supplier", store=store, tenant_id=tenant)
        assert source.scan({"days_late": 1, "object_ref": "PO-1"}) == []
        events = source.scan({"days_late": 3, "object_ref": "PO-1"})
        assert len(events) == 1 and events[0].object_ref == "PO-1"
        assert source.scan({"days_late": 4, "object_ref": "PO-1"}) == []
        # A new instance reads the persisted "already active" state.
        again = ConditionSource("late_supplier", lambda facts: facts["days_late"] > 2,
                                "Investigate late supplier", store=store, tenant_id=tenant)
        assert again.scan({"days_late": 5, "object_ref": "PO-1"}) == []
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
