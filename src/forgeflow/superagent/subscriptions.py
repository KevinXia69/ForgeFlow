"""Tenant-scoped source subscriptions used by the polling loop."""
from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

from .store import ControlStore


class SubscriptionError(ValueError):
    pass


def list_subscriptions(store: ControlStore, tenant_id: str) -> list[dict]:
    rows = store.connection.execute(
        "SELECT id, source_type, enabled, interval_seconds, config, last_scan_at "
        "FROM control_source_subscriptions WHERE tenant_id=%s ORDER BY source_type",
        (tenant_id,)).fetchall()
    values = [dict(row) for row in rows]
    store.connection.commit()
    return values


def ensure_subscription(store: ControlStore, *, tenant_id: str, source_type: str,
                        interval_seconds: int = 60,
                        config: dict | None = None) -> dict:
    """Create the default subscription only if the tenant has none.

    Startup must not re-enable a source an administrator deliberately turned off,
    nor reset the interval they chose.  ``upsert_subscription`` is for an explicit
    user edit; this is for bootstrap.
    """
    if source_type != "odoo20.purchase_order" or not (5 <= int(interval_seconds) <= 86400):
        raise SubscriptionError("source_subscription_invalid")
    with store.transaction() as cursor:
        cursor.execute(
            "INSERT INTO control_source_subscriptions(id, tenant_id, source_type, "
            "enabled, interval_seconds, config) VALUES (%s, %s, %s, true, %s, %s) "
            "ON CONFLICT (tenant_id, source_type) DO NOTHING",
            (str(uuid.uuid4()), tenant_id, source_type, int(interval_seconds),
             Jsonb(config or {})))
        row = cursor.execute(
            "SELECT id, source_type, enabled, interval_seconds, config "
            "FROM control_source_subscriptions WHERE tenant_id=%s AND source_type=%s",
            (tenant_id, source_type)).fetchone()
    return dict(row)


def upsert_subscription(store: ControlStore, *, tenant_id: str, source_type: str,
                        enabled: bool = True, interval_seconds: int = 60,
                        config: dict | None = None) -> dict:
    if source_type != "odoo20.purchase_order" or not (5 <= int(interval_seconds) <= 86400):
        raise SubscriptionError("source_subscription_invalid")
    with store.transaction() as cursor:
        row = cursor.execute(
            "INSERT INTO control_source_subscriptions(id, tenant_id, source_type, enabled, "
            "interval_seconds, config) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, source_type) DO UPDATE SET enabled=EXCLUDED.enabled, "
            "interval_seconds=EXCLUDED.interval_seconds, config=EXCLUDED.config "
            "RETURNING id, source_type, enabled, interval_seconds, config",
            (str(uuid.uuid4()), tenant_id, source_type, bool(enabled),
             int(interval_seconds), Jsonb(config or {}))).fetchone()
    return dict(row)
