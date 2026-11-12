"""Run enabled source subscriptions once against the allowlisted Odoo adapter."""
from __future__ import annotations

from .engine import ControlEngine
from .identity import TenantContext
from .sources import SourceScanner
from .store import ControlStore
from .subscriptions import list_subscriptions


class SourceRunner:
    def __init__(self, store: ControlStore, adapter, ctx: TenantContext) -> None:
        self.store = store
        self.adapter = adapter
        self.ctx = ctx
        self.engine = ControlEngine(store)

    def scan_once(self) -> list[dict]:
        results = []
        for subscription in list_subscriptions(self.store, self.ctx.tenant_id):
            if not subscription["enabled"]:
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
            tickets = source.admit(self.engine, self.ctx, events, new_only=True,
                                   policy=str(config.get("policy", "observe")))
            with self.store.transaction() as cursor:
                cursor.execute(
                    "UPDATE control_source_subscriptions SET last_scan_at=now() "
                    "WHERE tenant_id=%s AND id=%s",
                    (self.ctx.tenant_id, subscription["id"]))
            results.append({"source_type": subscription["source_type"],
                            "events": len(events), "tickets": len(tickets)})
        return results
