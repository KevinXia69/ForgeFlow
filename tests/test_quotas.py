"""Global and tenant quota consumption is atomic and per UTC day."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.superagent import ControlStore
from forgeflow.superagent.quotas import QuotaLimits, QuotaStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not DATABASE_URL,
                                 reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _tenant(store):
    tenant = f"quota-{uuid.uuid4().hex}"
    store.migrate()
    store.seed_tenant(tenant, "Quota", 1, (("planner", "Planner", "planner"),))
    return tenant


@requires_db
def test_tenant_and_global_limits_are_enforced_together() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    other = _tenant(store)
    limits = QuotaLimits(tenant={"model_tokens": 100}, global_={"model_tokens": 150})
    quotas = QuotaStore(store, limits)
    try:
        assert quotas.consume(tenant, "model_tokens", 60)["allowed"] is True
        denied = quotas.consume(tenant, "model_tokens", 50)
        assert denied["allowed"] is False and denied["denied_scope"] == "tenant"
        assert quotas.consume(other, "model_tokens", 60)["allowed"] is True
        global_denied = quotas.consume(other, "model_tokens", 40)
        assert global_denied["allowed"] is False and global_denied["denied_scope"] == "global"
        snapshot = {row["tenant_id"]: row["used"] for row in quotas.snapshot(tenant)}
        assert snapshot[tenant] == 60
        assert snapshot["*"] == 120
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_quota_usage WHERE tenant_id = ANY(%s)",
                ([tenant, other, "*"],))
            store.connection.execute("DELETE FROM control_tenants WHERE id = ANY(%s)",
                                     ([tenant, other],))
        store.close()


@requires_db
def test_zero_limits_mean_unlimited_and_amounts_are_checked() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    quotas = QuotaStore(store, QuotaLimits())
    try:
        assert quotas.consume(tenant, "tool_calls", 10_000)["allowed"] is True
        assert quotas.consume(tenant, "wall_seconds", 10_000_000)["allowed"] is True
        with pytest.raises(ValueError, match="quota_kind_invalid"):
            quotas.check(tenant, "not_a_kind", 1)
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_quota_usage WHERE tenant_id = ANY(%s)",
                ([tenant, "*"],))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


@requires_db
def test_a_reservation_is_settled_to_actual_spend_and_overage_is_recorded() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    limits = QuotaLimits(tenant={"model_tokens": 100}, global_={"model_tokens": 100})
    quotas = QuotaStore(store, limits)
    try:
        assert quotas.reserve(tenant, "model_tokens", 80)["allowed"] is True
        quotas.settle(tenant, "model_tokens", 80, 40)
        assert quotas.check(tenant, "model_tokens", 0)["tenant_used"] == 40
        quotas.settle(tenant, "model_tokens", 40, 90)
        over = quotas.check(tenant, "model_tokens", 0)
        assert over["tenant_used"] == 90
        # A forced settlement records spend even beyond the limit.
        denied = quotas.consume(tenant, "model_tokens", 20, force=True)
        assert denied["allowed"] is False and denied["over_limit"] is True
        assert quotas.check(tenant, "model_tokens", 0)["tenant_used"] == 110
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_quota_usage WHERE tenant_id = ANY(%s)",
                ([tenant, "*"],))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


@requires_db
def test_reserve_many_is_all_or_nothing_across_kinds() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    quotas = QuotaStore(store, QuotaLimits(tenant={"wall_seconds": 10,
                                                   "model_tokens": 5}))
    try:
        denied = quotas.reserve_many(tenant, [("wall_seconds", 10),
                                              ("model_tokens", 10)])
        assert denied["allowed"] is False and denied["denied_scope"] == "tenant"
        assert quotas.check(tenant, "wall_seconds", 0)["tenant_used"] == 0
        assert quotas.check(tenant, "model_tokens", 0)["tenant_used"] == 0
        allowed = quotas.reserve_many(tenant, [("wall_seconds", 10),
                                               ("model_tokens", 5)])
        assert allowed["allowed"] is True
        assert quotas.check(tenant, "model_tokens", 0)["tenant_used"] == 5
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_quota_usage WHERE tenant_id = ANY(%s)",
                ([tenant, "*"],))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


@requires_db
def test_a_task_finishing_after_utc_midnight_refunds_the_reservation_day() -> None:
    from datetime import date

    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    other = _tenant(store)
    quotas = QuotaStore(store, QuotaLimits(tenant={"model_tokens": 100}))
    try:
        first = quotas.reserve_many(tenant, [("model_tokens", 100)],
                                    window_start=date(2026, 11, 8))
        assert first["window_start"] == "2026-11-08"
        quotas.reserve_many(other, [("model_tokens", 50)],
                            window_start=date(2026, 11, 9))
        # Task reserved on the 8th finishes after midnight: refund must hit the 8th.
        quotas.settle(tenant, "model_tokens", 100, 40, window_start="2026-11-08")
        day8 = quotas.check(tenant, "model_tokens", 0,
                            window_start=date(2026, 11, 8))
        day9 = quotas.check(other, "model_tokens", 0,
                            window_start=date(2026, 11, 9))
        assert day8["tenant_used"] == 40
        assert day9["tenant_used"] == 50
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_quota_usage WHERE tenant_id = ANY(%s)",
                ([tenant, other, "*"],))
            store.connection.execute("DELETE FROM control_tenants WHERE id = ANY(%s)",
                                     ([tenant, other],))
        store.close()


@requires_db
def test_index_tasks_are_a_governed_quota_kind() -> None:
    from forgeflow.semantic.index import IndexQuotaExceeded, consume_index_task

    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    quotas = QuotaStore(store, QuotaLimits(tenant={"index_tasks": 1}))
    try:
        first = consume_index_task(store, tenant, quotas=quotas)
        assert first["allowed"] is True and first["kind"] == "index_tasks"
        with pytest.raises(IndexQuotaExceeded) as raised:
            consume_index_task(store, tenant, quotas=quotas)
        assert raised.value.code == "index_quota_exhausted"
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_quota_usage WHERE tenant_id = ANY(%s)",
                ([tenant, "*"],))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
