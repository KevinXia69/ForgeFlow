"""Per-tenant Document Mind cost accounting and alerts."""
from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from forgeflow.knowledge.docmind_costs import DocMindPrices, DocMindUsageStore
from forgeflow.superagent import ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _tenant(store) -> str:
    tenant = f"docmind-cost-{uuid.uuid4().hex}"
    store.migrate()
    store.seed_tenant(tenant, "DocMind cost", 1, (("planner", "Planner", "planner"),))
    return tenant


def _prices(**kwargs) -> DocMindPrices:
    values = {"usd_per_page": 0.01, "usd_per_call": 0.001,
              "daily_alert_usd": 0.02}
    values.update(kwargs)
    return DocMindPrices(**values)


def test_usage_is_accumulated_per_tenant_and_day() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    other = _tenant(store)
    try:
        usage = DocMindUsageStore(store, _prices())
        usage.record(tenant, calls=1, pages=3, blocks=4)
        usage.record(tenant, calls=2, pages=1)
        usage.record(other, calls=1, pages=1)
        summary = usage.summary(tenant, days=7)
        assert summary["totals"] == {"vendor_calls": 3, "pages": 4, "blocks": 4,
                                     "tokens": 0, "cost_micros": 43_000}
        assert summary["today"]["cost_micros"] == 43_000
        assert summary["prices"]["priced"] is True
        assert [alert["code"] for alert in summary["alerts"]] == [
            "docmind_daily_cost_alert"]
        assert usage.summary(other, days=7)["totals"]["cost_micros"] == 11_000
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_tenants WHERE id=ANY(%s)", ([tenant, other],))
        store.close()


def test_an_unpriced_deployment_records_counters_but_no_cost_or_alert() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    try:
        usage = DocMindUsageStore(store, DocMindPrices())
        usage.record(tenant, calls=2, pages=5)
        summary = usage.summary(tenant)
        assert summary["totals"]["pages"] == 5
        assert summary["totals"]["cost_micros"] == 0
        assert summary["alerts"] == []
        assert summary["prices"]["priced"] is False
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_the_window_excludes_old_days_and_alerts_are_per_day() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    today = datetime.now(UTC).date()
    try:
        usage = DocMindUsageStore(store, _prices(daily_alert_usd=0.05))
        usage.record(tenant, pages=3, when=today - timedelta(days=10))
        summary = usage.summary(tenant, days=7, now=today)
        assert summary["totals"]["pages"] == 0
        usage.record(tenant, pages=3, when=today)
        summary = usage.summary(tenant, days=7, now=today)
        assert summary["totals"]["pages"] == 3
        assert summary["alerts"] == []  # 0.03 is below 0.05
        usage.record(tenant, pages=3, when=today)
        assert [row["window_start"] for row in usage.summary(tenant, days=7)["daily"]] == [
            today.isoformat()]
        assert usage.summary(tenant, days=7)["alerts"]
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_prices_are_read_from_the_environment_and_negative_values_are_ignored() -> None:
    prices = DocMindPrices.from_env({
        "FORGEFLOW_DOCMIND_USD_PER_PAGE": "0.02",
        "FORGEFLOW_DOCMIND_USD_PER_CALL": "-1",
        "FORGEFLOW_DOCMIND_DAILY_ALERT_USD": "2.5",
    })
    assert prices.usd_per_page == 0.02
    assert prices.usd_per_call == 0.0
    assert prices.daily_alert_usd == 2.5
    assert prices.cost_micros(pages=2) == 40_000
