"""Global and per-tenant quotas for model, tool, wall-clock and search work.

Limits are deployment configuration (``FORGEFLOW_QUOTA_*`` for a tenant and
``FORGEFLOW_GLOBAL_QUOTA_*`` globally); zero or absent means unlimited, so a
deployment that has not configured quotas keeps its existing behaviour.  Usage
is persisted per UTC day and consumed atomically so two concurrent workers
cannot both spend the last unit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Mapping

from .store import ControlStore

KINDS = ("model_tokens", "tool_calls", "wall_seconds", "search_calls",
         "index_tasks")
GLOBAL_TENANT = "*"


class QuotaError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    tenant: dict[str, int] = field(default_factory=dict)
    global_: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "QuotaLimits":
        env = os.environ if environ is None else environ
        tenant: dict[str, int] = {}
        global_: dict[str, int] = {}
        for kind in KINDS:
            tenant[kind] = _int(env.get(f"FORGEFLOW_QUOTA_{kind.upper()}", "0"))
            global_[kind] = _int(env.get(f"FORGEFLOW_GLOBAL_QUOTA_{kind.upper()}", "0"))
        return cls(tenant=tenant, global_=global_)

    def limit(self, kind: str, *, tenant_id: str | None = None) -> int:
        if kind not in KINDS:
            raise QuotaError("quota_kind_invalid")
        return self.tenant.get(kind, 0) if tenant_id is not None else self.global_.get(kind, 0)


class QuotaStore:
    def __init__(self, store: ControlStore, limits: QuotaLimits) -> None:
        self.store = store
        self.limits = limits

    def snapshot(self, tenant_id: str) -> list[dict[str, Any]]:
        day = datetime.now(UTC).date()
        rows = self.store.connection.execute(
            "SELECT tenant_id, kind, used FROM control_quota_usage "
            "WHERE window_start=%s AND tenant_id = ANY(%s) ORDER BY kind",
            (day, [tenant_id, GLOBAL_TENANT])).fetchall()
        self.store.connection.commit()
        return [dict(row) for row in rows]

    def check(self, tenant_id: str, kind: str, amount: int = 1, *,
              window_start: date | None = None) -> dict[str, Any]:
        return self._decide(tenant_id, kind, amount, consume=False,
                            window_start=window_start)

    def reserve_many(self, tenant_id: str, items: list[tuple[str, int]], *,
                     window_start: date | None = None) -> dict[str, Any]:
        """Reserve several kinds atomically: all succeed or nothing is charged.

        The returned ``window_start`` must be passed to :meth:`settle`; a task
        completing after UTC midnight still settles against the day it reserved.
        """
        normalized = [(kind, max(0, int(amount))) for kind, amount in items]
        for kind, _ in normalized:
            if kind not in KINDS:
                raise QuotaError("quota_kind_invalid")
        day = window_start or datetime.now(UTC).date()
        with self.store.transaction() as cursor:
            used: dict[tuple[str, str], int] = {}
            for scope in (GLOBAL_TENANT, tenant_id):
                for kind, _ in normalized:
                    cursor.execute(
                        "INSERT INTO control_quota_usage(tenant_id, kind, window_start, used) "
                        "VALUES (%s,%s,%s,0) ON CONFLICT DO NOTHING", (scope, kind, day))
                    row = cursor.execute(
                        "SELECT used FROM control_quota_usage WHERE tenant_id=%s AND kind=%s "
                        "AND window_start=%s FOR UPDATE", (scope, kind, day)).fetchone()
                    used[(scope, kind)] = int(row["used"])
            denied = []
            results = []
            for kind, amount in normalized:
                global_limit = self.limits.limit(kind)
                tenant_limit = self.limits.limit(kind, tenant_id=tenant_id)
                scope = None
                if global_limit and used[(GLOBAL_TENANT, kind)] + amount > global_limit:
                    scope = "global"
                elif tenant_limit and used[(tenant_id, kind)] + amount > tenant_limit:
                    scope = "tenant"
                results.append({
                    "allowed": scope is None, "kind": kind, "amount": amount,
                    "denied_scope": scope,
                    "global_used": used[(GLOBAL_TENANT, kind)],
                    "global_limit": global_limit,
                    "tenant_used": used[(tenant_id, kind)],
                    "tenant_limit": tenant_limit})
                if scope:
                    denied.append(scope)
            if not denied:
                for kind, amount in normalized:
                    if not amount:
                        continue
                    for scope in (GLOBAL_TENANT, tenant_id):
                        cursor.execute(
                            "UPDATE control_quota_usage SET used=used+%s, updated_at=now() "
                            "WHERE tenant_id=%s AND kind=%s AND window_start=%s",
                            (amount, scope, kind, day))
            return {"allowed": not denied, "items": results,
                    "window_start": day.isoformat(),
                    "denied_scope": denied[0] if denied else None}

    def reserve(self, tenant_id: str, kind: str, amount: int = 1) -> dict[str, Any]:
        """Atomically reserve budget before work starts."""
        return self._decide(tenant_id, kind, amount, consume=True)

    def consume(self, tenant_id: str, kind: str, amount: int = 1, *,
                force: bool = False, window_start: date | None = None) -> dict[str, Any]:
        return self._decide(tenant_id, kind, amount, consume=True, force=force,
                            window_start=window_start)

    def settle(self, tenant_id: str, kind: str, reserved: int,
               actual: int, *, window_start: date | str | None = None) -> dict[str, Any]:
        """Replace a reservation with measured spend in the reservation window."""
        reserved, actual = max(0, int(reserved)), max(0, int(actual))
        if isinstance(window_start, str):
            try:
                window_start = date.fromisoformat(window_start)
            except ValueError:
                window_start = None
        delta = actual - reserved
        if delta > 0:
            return self.consume(tenant_id, kind, delta, force=True,
                                window_start=window_start)
        if delta < 0:
            day = window_start or datetime.now(UTC).date()
            with self.store.transaction() as cursor:
                for scope in (GLOBAL_TENANT, tenant_id):
                    cursor.execute(
                        "UPDATE control_quota_usage SET used=GREATEST(0, used - %s), "
                        "updated_at=now() WHERE tenant_id=%s AND kind=%s AND window_start=%s",
                        (min(-delta, reserved), scope, kind, day))
            return self.check(tenant_id, kind, 0)
        return self.check(tenant_id, kind, 0)

    def _decide(self, tenant_id: str, kind: str, amount: int, *,
                consume: bool, force: bool = False,
                window_start: date | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise QuotaError("quota_kind_invalid")
        amount = max(0, int(amount))
        day = window_start or datetime.now(UTC).date()
        with self.store.transaction() as cursor:
            used: dict[str, int] = {}
            for scope in (GLOBAL_TENANT, tenant_id):
                cursor.execute(
                    "INSERT INTO control_quota_usage(tenant_id, kind, window_start, used) "
                    "VALUES (%s,%s,%s,0) ON CONFLICT DO NOTHING", (scope, kind, day))
                row = cursor.execute(
                    "SELECT used FROM control_quota_usage WHERE tenant_id=%s AND kind=%s "
                    "AND window_start=%s FOR UPDATE", (scope, kind, day)).fetchone()
                used[scope] = int(row["used"])
            denied = []
            if self.limits.limit(kind) and used[GLOBAL_TENANT] + amount > self.limits.limit(kind):
                denied.append("global")
            if self.limits.limit(kind, tenant_id=tenant_id) and (
                    used[tenant_id] + amount > self.limits.limit(kind, tenant_id=tenant_id)):
                denied.append("tenant")
            if consume and amount and (not denied or force):
                for scope in (GLOBAL_TENANT, tenant_id):
                    cursor.execute(
                        "UPDATE control_quota_usage SET used=used+%s, updated_at=now() "
                        "WHERE tenant_id=%s AND kind=%s AND window_start=%s",
                        (amount, scope, kind, day))
            return {
                "allowed": not denied, "kind": kind, "amount": amount,
                "over_limit": bool(denied) if force else False,
                "window_start": day.isoformat(), "denied_scope": denied[0] if denied else None,
                "global_used": used[GLOBAL_TENANT] + (amount if not denied and consume else 0),
                "global_limit": self.limits.limit(kind),
                "tenant_used": used[tenant_id] + (amount if not denied and consume else 0),
                "tenant_limit": self.limits.limit(kind, tenant_id=tenant_id),
            }


def _int(raw: str) -> int:
    try:
        return max(0, int(str(raw).strip() or "0"))
    except (TypeError, ValueError):
        return 0
