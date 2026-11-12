"""Per-tenant Document Mind usage and cost accounting.

The vendor bills per submitted job and per parsed page.  Prices are deployment
configuration (``FORGEFLOW_DOCMIND_USD_PER_PAGE``, ``..._PER_CALL``); when they
are unset the counters are still recorded but the monetary totals are zero and
no alert fires, so a deployment never invents a price.

An alert is an observation, not a business quota: it reports that today's
estimated spend crossed ``FORGEFLOW_DOCMIND_DAILY_ALERT_USD`` and never blocks a
parse.  The product deliberately has no per-tenant spend cap for parsing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping

from ..superagent.store import ControlStore

MICROS = 1_000_000


@dataclass(frozen=True, slots=True)
class DocMindPrices:
    usd_per_page: float = 0.0
    usd_per_call: float = 0.0
    daily_alert_usd: float = 0.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DocMindPrices":
        env = os.environ if environ is None else environ
        return cls(
            usd_per_page=_money(env.get("FORGEFLOW_DOCMIND_USD_PER_PAGE")),
            usd_per_call=_money(env.get("FORGEFLOW_DOCMIND_USD_PER_CALL")),
            daily_alert_usd=_money(env.get("FORGEFLOW_DOCMIND_DAILY_ALERT_USD")),
        )

    def cost_micros(self, *, calls: int = 0, pages: int = 0) -> int:
        value = (max(0, int(calls)) * max(0.0, self.usd_per_call)
                 + max(0, int(pages)) * max(0.0, self.usd_per_page))
        return int(round(value * MICROS))


def _money(raw: str | None) -> float:
    try:
        value = float(str(raw or "0").strip() or "0")
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


class DocMindUsageStore:
    """Accumulate what a tenant spent on parsing, per UTC day."""

    def __init__(self, store: ControlStore, prices: DocMindPrices | None = None) -> None:
        self.store = store
        self.prices = prices or DocMindPrices.from_env()

    def record(self, tenant_id: str, *, calls: int = 0, pages: int = 0,
               blocks: int = 0, tokens: int = 0, when: date | None = None) -> dict[str, Any]:
        if not tenant_id:
            raise ValueError("tenant_required")
        calls, pages = max(0, int(calls)), max(0, int(pages))
        if not any((calls, pages, blocks, tokens)):
            return {"recorded": False}
        day = when or datetime.now(UTC).date()
        cost = self.prices.cost_micros(calls=calls, pages=pages)
        with self.store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO knowledge_docmind_usage(tenant_id, window_start, "
                "vendor_calls, pages, blocks, tokens, cost_micros) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, window_start) DO UPDATE SET "
                "vendor_calls = knowledge_docmind_usage.vendor_calls + EXCLUDED.vendor_calls, "
                "pages = knowledge_docmind_usage.pages + EXCLUDED.pages, "
                "blocks = knowledge_docmind_usage.blocks + EXCLUDED.blocks, "
                "tokens = knowledge_docmind_usage.tokens + EXCLUDED.tokens, "
                "cost_micros = knowledge_docmind_usage.cost_micros + EXCLUDED.cost_micros, "
                "updated_at = now()",
                (tenant_id, day, calls, pages, max(0, int(blocks)),
                 max(0, int(tokens)), cost))
        return {"recorded": True, "window_start": day.isoformat(),
                "cost_micros": cost}

    def summary(self, tenant_id: str, *, days: int = 30,
                now: date | None = None) -> dict[str, Any]:
        window = max(1, min(int(days), 366))
        today = now or datetime.now(UTC).date()
        first = today - timedelta(days=window - 1)
        rows = self.store.connection.execute(
            "SELECT window_start, vendor_calls, pages, blocks, tokens, cost_micros "
            "FROM knowledge_docmind_usage WHERE tenant_id=%s AND window_start >= %s "
            "AND window_start <= %s ORDER BY window_start DESC",
            (tenant_id, first, today)).fetchall()
        self.store.connection.commit()
        days_rows = [dict(row) for row in rows]
        totals = {
            "vendor_calls": sum(int(row["vendor_calls"]) for row in days_rows),
            "pages": sum(int(row["pages"]) for row in days_rows),
            "blocks": sum(int(row["blocks"]) for row in days_rows),
            "tokens": sum(int(row["tokens"]) for row in days_rows),
            "cost_micros": sum(int(row["cost_micros"]) for row in days_rows),
        }
        today_row = next((row for row in days_rows
                          if str(row["window_start"]) == today.isoformat()), None)
        today_cost = int(today_row["cost_micros"]) if today_row else 0
        alerts = []
        if self.prices.daily_alert_usd and today_cost >= int(round(
                self.prices.daily_alert_usd * MICROS)):
            alerts.append({
                "code": "docmind_daily_cost_alert",
                "window_start": today.isoformat(),
                "cost_micros": today_cost,
                "threshold_micros": int(round(self.prices.daily_alert_usd * MICROS)),
            })
        return {
            "tenant_id": tenant_id,
            "window_days": window,
            "as_of": today.isoformat(),
            "totals": totals,
            "today": ({"window_start": str(today_row["window_start"]),
                       "vendor_calls": int(today_row["vendor_calls"]),
                       "pages": int(today_row["pages"]),
                       "blocks": int(today_row["blocks"]),
                       "cost_micros": today_cost}
                      if today_row else {"window_start": today.isoformat(),
                                         "vendor_calls": 0, "pages": 0, "blocks": 0,
                                         "cost_micros": 0}),
            "prices": {"usd_per_page": self.prices.usd_per_page,
                       "usd_per_call": self.prices.usd_per_call,
                       "daily_alert_usd": self.prices.daily_alert_usd,
                       "priced": bool(self.prices.usd_per_page
                                      or self.prices.usd_per_call)},
            "alerts": alerts,
            "daily": [{"window_start": str(row["window_start"]),
                       "vendor_calls": int(row["vendor_calls"]),
                       "pages": int(row["pages"]),
                       "blocks": int(row["blocks"]),
                       "cost_micros": int(row["cost_micros"])}
                      for row in days_rows],
        }
