"""Subscription-driven source service; one process can own a tenant's polling."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .engine import ControlEngine
from .heartbeat import beat
from .identity import TenantContext
from .sources import SourceScanner
from .store import ControlStore
from .subscriptions import list_subscriptions


class SourceService:
    def __init__(self, store: ControlStore, adapter, ctx: TenantContext,
                 *, tick_seconds: float = 5.0, outbox=None) -> None:
        if tick_seconds <= 0:
            raise ValueError("invalid_source_tick")
        self.store = store
        self.adapter = adapter
        self.ctx = ctx
        self.engine = ControlEngine(store)
        self.tick_seconds = float(tick_seconds)
        # Optional transactional-outbox drain; kept separate so a slow outbox
        # never delays scanning a subscription.
        self.outbox = outbox

    def tick(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        beat(self.store, service_id=f"source:{self.ctx.tenant_id}",
             tenant_id=self.ctx.tenant_id, kind="source")
        results = []
        for subscription in list_subscriptions(self.store, self.ctx.tenant_id):
            if not subscription["enabled"]:
                continue
            last = subscription.get("last_scan_at")
            if last is not None and last + timedelta(
                    seconds=int(subscription["interval_seconds"])) > now:
                continue
            if subscription["source_type"] != "odoo20.purchase_order":
                continue
            config = subscription.get("config") or {}
            source = SourceScanner(self.adapter, self.store,
                                   policy=str(config.get("policy", "observe")),
                                   tenant_id=self.ctx.tenant_id)
            events = source.scan(subscription["source_type"],
                                 horizon_days=int(config.get("horizon_days", 30)),
                                 tenant_id=self.ctx.tenant_id)
            # A normal business change is admitted and investigated; it is not a
            # reason to ask a human to confirm the next step (plan section 8.4).
            tickets = source.admit(self.engine, self.ctx, events, new_only=True,
                                   policy=str(config.get("policy", "observe")))
            with self.store.transaction() as cursor:
                cursor.execute(
                    "UPDATE control_source_subscriptions SET last_scan_at=now() "
                    "WHERE tenant_id=%s AND id=%s",
                    (self.ctx.tenant_id, subscription["id"]))
            results.append({"source_type": subscription["source_type"],
                            "events": len(events), "tickets": len(tickets)})
        if self.outbox is not None:
            results.append({"source_type": "odoo20.outbox", **self.outbox.tick()})
        return results

    def run(self, stop_event) -> int:
        ticks = 0
        while not stop_event.is_set():
            self.tick()
            ticks += 1
            stop_event.wait(self.tick_seconds)
        return ticks
