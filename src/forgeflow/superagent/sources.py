"""Business sources that turn *meaningful* business changes into triggers.

The previous scanner treated "a purchase order is due soon" as an exception.  It
is not: it is the normal state of an open order, and turning it into work is how
a system trains its users to ignore it.  A trigger is only emitted when a real
business condition holds, and every condition carries the facts and the rule it
was evaluated against.

Scanning is paged and incremental.  State about what has already been observed
lives in the control store, never in a process-local ``_last`` field, so a
restart neither loses events nor replays them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Iterable

from .contracts import EffectKind, SourceState
from .engine import ControlEngine
from .identity import TenantContext
from .store import ControlStore

#: Business conditions that justify creating work (plan section 8.1).
CONDITIONS: dict[str, str] = {
    "supply_cancelled": "A confirmed supply commitment was cancelled",
    "commitment_date_changed": "A committed delivery date moved and affects downstream dates",
    "past_due_receipt": "The committed date passed without the matching receipt",
    "component_shortage": "A component shortage affects a confirmed manufacturing order",
    "workcenter_unavailable": "A workcenter is unavailable or its capacity conflicts",
    "business_rule_changed": "A formal rule or contract changed and invalidates an existing plan",
}

CONDITION_GOALS: dict[str, str] = {
    "supply_cancelled": "Recover supply after a cancelled purchase commitment",
    "commitment_date_changed": "Assess the impact of a changed supplier commitment date",
    "past_due_receipt": "Recover the missing receipt for a past-due purchase commitment",
    "component_shortage": "Recover a manufacturing order affected by a component shortage",
    "workcenter_unavailable": "Recover manufacturing affected by an unavailable workcenter",
    "business_rule_changed": "Re-assess an existing plan against a changed formal rule",
}


class SourceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceEvent:
    source_id: str
    external_event_id: str
    payload_hash: str
    object_ref: str
    goal: str
    occurred_at: str | None = None
    facts: dict[str, Any] | None = None
    condition: str = "event"
    severity: str = "medium"
    goal_kind: str | None = None

    @property
    def durable_goal_kind(self) -> str:
        return self.goal_kind or self.condition


@dataclass(slots=True)
class ScanReport:
    """Evidence of what a scan actually covered; never a claim it was complete."""

    source_id: str
    pages: int = 0
    rows: int = 0
    events: int = 0
    truncated: bool = False
    cursor_before: str | None = None
    cursor_after: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"source_id": self.source_id, "pages": self.pages, "rows": self.rows,
                "events": self.events, "truncated": self.truncated,
                "cursor_before": self.cursor_before, "cursor_after": self.cursor_after,
                "errors": list(self.errors)}


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _as_date(value: Any) -> date | None:
    """Parse the Odoo ``YYYY-MM-DD HH:MM:SS`` shape without guessing time zones."""
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _numeric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class SourceStateStore:
    """Per-object observation state, persisted when a control store is supplied.

    Without a store the state lives in this object for the lifetime of the
    scanner.  That is enough for tests and single-shot scans, but it is *not*
    continuous monitoring -- :attr:`durable` says which mode is active so callers
    cannot claim restart-safe coverage they do not have.
    """

    def __init__(self, store: ControlStore | None, tenant_id: str | None = None) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self._memory: dict[str, dict] = {}

    @property
    def durable(self) -> bool:
        return self.store is not None and self.tenant_id is not None

    def load(self, source_id: str) -> dict:
        if not self.durable:
            return dict(self._memory.get(source_id) or {})
        row = self.store.connection.execute(
            "SELECT state FROM control_source_state WHERE tenant_id=%s AND source_id=%s",
            (self.tenant_id, source_id)).fetchone()
        self.store.connection.commit()
        return dict(row["state"]) if row else {}

    def save(self, source_id: str, state: dict, *, cursor_value: int = 0,
             last_event_at: datetime | None = None) -> None:
        if not self.durable:
            self._memory[source_id] = dict(state)
            return
        from psycopg.types.json import Jsonb

        with self.store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO control_source_state(tenant_id, source_id, state, cursor_value, "
                "last_event_at, updated_at) VALUES (%s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (tenant_id, source_id) DO UPDATE SET state=EXCLUDED.state, "
                "cursor_value=EXCLUDED.cursor_value, "
                "last_event_at=COALESCE(EXCLUDED.last_event_at, "
                "control_source_state.last_event_at), updated_at=now()",
                (self.tenant_id, source_id, Jsonb(state), int(cursor_value), last_event_at))

    def cursor(self, cursor_key: str) -> str | None:
        if not self.durable:
            return None
        row = self.store.connection.execute(
            "SELECT cursor_value FROM control_source_cursors "
            "WHERE tenant_id=%s AND cursor_key=%s", (self.tenant_id, cursor_key)).fetchone()
        self.store.connection.commit()
        return str(row["cursor_value"]) if row else None

    def set_cursor(self, cursor_key: str, value: str) -> None:
        if not self.durable:
            return
        with self.store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO control_source_cursors(tenant_id, cursor_key, cursor_value, "
                "updated_at) VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (tenant_id, cursor_key) DO UPDATE SET "
                "cursor_value=EXCLUDED.cursor_value, updated_at=now()",
                (self.tenant_id, cursor_key, str(value)))


class OdooPurchaseSource:
    """Detect real exceptions on purchase commitments, page by page.

    Conditions detected: a cancelled commitment, a moved commitment date that
    affects a downstream need, and a commitment that is past its date without the
    matching receipt.  Merely being "due soon" is not an exception.
    """

    SOURCE_ID = "odoo20.purchase_order"

    def __init__(self, adapter, *, horizon_days: int = 30,
                 state_store: SourceStateStore | None = None,
                 page_size: int = 200, max_pages: int = 50) -> None:
        self.adapter = adapter
        self.horizon_days = max(1, int(horizon_days))
        self.state = state_store or SourceStateStore(None)
        if page_size < 1 or max_pages < 1:
            raise SourceError("source_paging_invalid")
        self.page_size = int(page_size)
        self.max_pages = int(max_pages)
        self.last_report = ScanReport(self.SOURCE_ID)

    def scan(self, now: datetime | None = None) -> list[SourceEvent]:
        moment = now or datetime.now(UTC)
        today = moment.date()
        horizon = today + timedelta(days=self.horizon_days)
        report = ScanReport(self.SOURCE_ID)
        events: list[SourceEvent] = []
        offset = 0
        seen_orders: set[int] = set()
        for page in range(self.max_pages):
            rows = self._page(offset)
            report.pages += 1
            if not rows:
                break
            for row in rows:
                order_id = int(row["id"])
                seen_orders.add(order_id)
                report.rows += 1
                events.extend(self._evaluate(row, today=today, horizon=horizon,
                                             cursor=order_id))
            if len(rows) < self.page_size:
                break
            offset += self.page_size
        else:
            # The page budget ran out with a full page still returning rows.
            report.truncated = True
        report.events = len(events)
        self.last_report = report
        # A truncated scan must not look like full-enterprise coverage.
        return events

    def _page(self, offset: int) -> list[dict]:
        horizon = (datetime.now(UTC) + timedelta(days=self.horizon_days)).strftime("%Y-%m-%d")
        return self.adapter.search_read(
            "purchase.order",
            [("state", "in", ["purchase", "sent", "cancel"]),
             ("date_planned", "<=", horizon)],
            ["id", "name", "state", "date_planned", "partner_id", "company_id"],
            limit=self.page_size, order="id asc", offset=offset) or []

    def _evaluate(self, row: dict, *, today: date, horizon: date,
                  cursor: int) -> list[SourceEvent]:
        order_id = int(row["id"])
        order_ref = f"purchase.order:{order_id}"
        state_key = f"{self.SOURCE_ID}:{order_id}"
        previous = self.state.load(state_key)
        ordered, received = self._receipt_totals(order_id) if row.get("state") in {
            "purchase", "sent"} else (0.0, 0.0)
        current = {
            "state": str(row.get("state") or ""),
            "date_planned": str(row.get("date_planned") or ""),
            "name": str(row.get("name") or ""),
            "partner_id": row.get("partner_id"),
            "ordered": ordered,
            "received": received,
        }
        events: list[SourceEvent] = []
        if previous:
            events.extend(self._transitions(previous, current, order_ref=order_ref,
                                            order_id=order_id, row=row, today=today))
        self.state.save(state_key, {"current": current}, cursor_value=cursor)
        return events

    def _receipt_totals(self, order_id: int) -> tuple[float, float]:
        try:
            lines = self.adapter.search_read(
                "purchase.order.line", [("order_id", "=", int(order_id))],
                ["product_qty", "qty_received"], limit=500) or []
        except Exception:  # a read failure is "unknown", never "zero received"
            raise
        ordered = sum(_numeric(line.get("product_qty")) for line in lines)
        received = sum(_numeric(line.get("qty_received")) for line in lines)
        return ordered, received

    def _transitions(self, previous: dict, current: dict, *, order_ref: str,
                     order_id: int, row: dict, today: date) -> list[SourceEvent]:
        prior = previous.get("current") or {}
        prior_state = str(prior.get("state") or "")
        now_state = current["state"]
        facts = {"purchase_order": row, "previous": prior, "current": current}
        events: list[SourceEvent] = []
        if prior_state in {"purchase", "sent"} and now_state == "cancel":
            events.append(self._event("supply_cancelled", order_ref, facts, row,
                                      severity="high"))
        prior_date = _as_date(prior.get("date_planned"))
        current_date = _as_date(current.get("date_planned"))
        if (prior_date is not None and current_date is not None and prior_date != current_date
                and now_state in {"purchase", "sent"}):
            facts["date_shift_days"] = (current_date - prior_date).days
            events.append(self._event("commitment_date_changed", order_ref, facts, row,
                                      severity="high" if current_date > prior_date else "medium"))
        if (now_state in {"purchase", "sent"} and current_date is not None
                and current_date < today and current["received"] + 1e-9 < current["ordered"]):
            facts["days_late"] = (today - current_date).days
            events.append(self._event("past_due_receipt", order_ref, facts, row,
                                      severity="high"))
        return events

    def _event(self, condition: str, order_ref: str, facts: dict, row: dict, *,
               severity: str) -> SourceEvent:
        digest = _digest({**facts, "condition": condition})
        return SourceEvent(
            source_id=self.SOURCE_ID,
            external_event_id=f"purchase:{row['id']}:{condition}:{digest[:16]}",
            payload_hash=digest,
            object_ref=order_ref,
            goal=CONDITION_GOALS[condition],
            occurred_at=str(row.get("date_planned") or "") or None,
            facts={**facts, "condition": condition, "severity": severity},
            condition=condition, severity=severity, goal_kind=condition)

    def admit(self, engine: ControlEngine, ctx: TenantContext,
              events: Iterable[SourceEvent], *, new_only: bool = False,
              policy: str = SourceState.AUTO_INVESTIGATE.value) -> list[str]:
        return admit_events(engine, ctx, events, new_only=new_only, policy=policy)


def admit_events(engine: ControlEngine, ctx: TenantContext,
                 events: Iterable[SourceEvent], *, new_only: bool = False,
                 policy: str = SourceState.AUTO_INVESTIGATE.value,
                 capability: str = "assess") -> list[str]:
    """Admit source events under the configured continuous-watch policy.

    ``observe`` records the work that *would* be triggered without spending model
    budget on it.  ``auto_investigate`` starts the investigation.  Neither ever
    approves an ERP write.
    """
    if policy not in {state.value for state in SourceState}:
        raise SourceError("source_policy_invalid")
    if policy == SourceState.OFF.value:
        return []
    tickets: list[str] = []
    for event in events:
        investigate = policy == SourceState.AUTO_INVESTIGATE.value
        admission = engine.admit_trigger(
            ctx,
            source_id=event.source_id,
            external_event_id=event.external_event_id,
            payload_hash=event.payload_hash,
            object_ref=event.object_ref,
            goal=event.goal,
            goal_kind=event.durable_goal_kind,
            occurred_at=(datetime.fromisoformat(event.occurred_at)
                         if event.occurred_at and "T" in event.occurred_at else None),
            initial_effect_kind=EffectKind.LAUNCH_WORKER.value if investigate else None,
            capability=capability if investigate else None,
            initial_payload={"capability": capability, "severity": event.severity,
                             "condition": event.condition,
                             "input": {"source": event.source_id,
                                       "object_ref": event.object_ref,
                                       "condition": event.condition,
                                       "severity": event.severity,
                                       "facts": event.facts or {}}},
        )
        if new_only and admission.duplicate:
            continue
        tickets.append(admission.ticket_id)
    return tickets


class SourceScanner:
    """Route a subscription's source type to its scanner and admit policy."""

    def __init__(self, adapter, store: ControlStore | None = None, *,
                 policy: str = SourceState.OBSERVE.value,
                 tenant_id: str | None = None, page_size: int = 200,
                 max_pages: int = 50) -> None:
        self.adapter = adapter
        self.store = store
        self.tenant_id = tenant_id
        if policy not in {state.value for state in SourceState}:
            raise SourceError("source_policy_invalid")
        self.policy = policy
        self.page_size = page_size
        self.max_pages = max_pages
        self.last_report = ScanReport("none")

    def _state_store(self, tenant_id: str) -> SourceStateStore:
        return SourceStateStore(self.store, tenant_id)

    def scan(self, source_type: str, *, horizon_days: int = 30,
             now: datetime | None = None, tenant_id: str | None = None) -> list[SourceEvent]:
        if source_type != OdooPurchaseSource.SOURCE_ID:
            raise SourceError(f"source_type_unsupported:{source_type}")
        source = OdooPurchaseSource(
            self.adapter, horizon_days=horizon_days,
            state_store=self._state_store(tenant_id or self.tenant_id or ""),
            page_size=self.page_size, max_pages=self.max_pages)
        events = source.scan(now=now)
        self.last_report = source.last_report
        return events

    def admit(self, engine: ControlEngine, ctx: TenantContext,
              events: Iterable[SourceEvent], *, new_only: bool = False,
              policy: str | None = None) -> list[str]:
        return admit_events(engine, ctx, events, new_only=new_only,
                            policy=policy or self.policy)


class ConditionSource:
    """Evaluates one bounded condition over supplied facts and emits on change.

    Production use passes a ``SourceStateStore`` so the last state survives a
    restart.  Without one the source is explicitly non-durable and callers must
    not use it for continuous monitoring.
    """

    def __init__(self, name: str, predicate: Callable[[dict], bool], goal: str,
                 *, store: ControlStore | None = None, tenant_id: str | None = None,
                 condition: str = "condition", severity: str = "medium") -> None:
        if not name or not goal:
            raise SourceError("condition_source_invalid")
        self.source_id = name
        self.predicate = predicate
        self.goal = goal
        self._last = False
        self.condition = condition
        self.severity = severity
        self.state = SourceStateStore(store, tenant_id)

    @property
    def durable(self) -> bool:
        return self.state.durable

    def scan(self, facts: dict) -> list[SourceEvent]:
        try:
            active = bool(self.predicate(facts))
        except Exception:
            raise SourceError("condition_source_evaluation_failed") from None
        if self.durable:
            stored = self.state.load(self.source_id)
            previously = bool(stored.get("active", False))
        else:
            previously = self._last
        if active == previously:
            return []
        self._last = active
        self.state.save(self.source_id, {"active": active}, cursor_value=int(active),
                        last_event_at=datetime.now(UTC) if active else None)
        if not active:
            return []
        digest = _digest(facts)
        return [SourceEvent(self.source_id, f"{self.source_id}:{digest[:16]}", digest,
                            str(facts.get("object_ref", self.source_id)), self.goal,
                            facts=facts, condition=self.condition,
                            severity=self.severity, goal_kind=self.condition)]

    def admit(self, engine: ControlEngine, ctx: TenantContext,
              events: Iterable[SourceEvent], *, new_only: bool = False,
              policy: str = SourceState.AUTO_INVESTIGATE.value) -> list[str]:
        return admit_events(engine, ctx, events, new_only=new_only, policy=policy)


class KnowledgeGenerationSource:
    """Emits when the tenant's published knowledge generation changes."""

    def __init__(self, tenant_id: str, catalog, *, store: ControlStore | None = None) -> None:
        self.tenant_id = tenant_id
        self.catalog = catalog
        self._last: str | None = None
        self.state = SourceStateStore(store, tenant_id)

    def scan(self) -> list[SourceEvent]:
        generation = self.catalog.published(self.tenant_id)
        if self.state.durable:
            stored = self.state.load("knowledge.published_generation")
            previously = stored.get("generation")
        else:
            previously = self._last
        if generation == previously:
            return []
        self._last = generation
        self.state.save("knowledge.published_generation", {"generation": generation},
                        last_event_at=datetime.now(UTC))
        if not generation:
            return []
        return [SourceEvent("knowledge.published_generation",
                            f"knowledge:{generation}", generation,
                            f"knowledge:{self.tenant_id}",
                            "Review the newly published knowledge generation",
                            condition="business_rule_changed",
                            goal_kind="knowledge_generation")]

    def admit(self, engine: ControlEngine, ctx: TenantContext,
              events: Iterable[SourceEvent], *, new_only: bool = False,
              policy: str = SourceState.AUTO_INVESTIGATE.value) -> list[str]:
        return admit_events(engine, ctx, events, new_only=new_only, policy=policy)


class SourceLoop:
    """Stop-aware polling loop; the control engine supplies idempotency."""

    def __init__(self, source, *, interval_seconds: float = 60.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("invalid_source_interval")
        self.source = source
        self.interval_seconds = float(interval_seconds)

    def run_once(self, engine: ControlEngine, ctx: TenantContext) -> list[str]:
        return self.source.admit(engine, ctx, self.source.scan())

    def run(self, engine: ControlEngine, ctx: TenantContext, stop_event) -> int:
        admitted = 0
        while not stop_event.is_set():
            admitted += len(self.run_once(engine, ctx))
            stop_event.wait(self.interval_seconds)
        return admitted


class ConditionSourceLoop(SourceLoop):
    def __init__(self, source: ConditionSource, facts, *,
                 interval_seconds: float = 60.0) -> None:
        self.facts = facts
        super().__init__(source, interval_seconds=interval_seconds)

    def run_once(self, engine: ControlEngine, ctx: TenantContext) -> list[str]:
        return self.source.admit(engine, ctx, self.source.scan(self.facts))


def refresh_observed_state(store: ControlStore, tenant_id: str, adapter, *,
                           object_refs: Iterable[str]) -> int:
    """Record the current state of just-executed objects as already observed.

    Without this, the source scanner would see the change *we* made and raise a
    second exception for it: cancelling a purchase order as part of a recovery
    would immediately produce "a supplier cancelled your purchase order".  This
    is the feedback-loop suppression the plan asks for, implemented by resetting
    the observation baseline for the objects an approved execution touched rather
    than by guessing from timestamps.
    """
    state = SourceStateStore(store, tenant_id)
    refreshed = 0
    for object_ref in object_refs:
        model, _, raw_id = str(object_ref).partition(":")
        if model not in {"purchase.order", "mrp.production"}:
            continue
        try:
            order_id = int(raw_id)
        except ValueError:
            continue
        try:
            rows = adapter.search_read(
                model, [("id", "=", order_id)],
                ["id", "name", "state", "date_planned", "partner_id"], limit=1)
        except Exception:  # the object is no longer readable; leave the baseline alone
            continue
        if not rows:
            continue
        row = rows[0]
        if model == "purchase.order":
            source_id = f"{OdooPurchaseSource.SOURCE_ID}:{order_id}"
            current = {
                "state": str(row.get("state") or ""),
                "date_planned": str(row.get("date_planned") or ""),
                "name": str(row.get("name") or ""),
                "partner_id": row.get("partner_id"),
                "ordered": 0.0,
                "received": 0.0,
            }
            try:
                lines = adapter.search_read(
                    "purchase.order.line", [("order_id", "=", order_id)],
                    ["product_qty", "qty_received"], limit=500) or []
                current["ordered"] = sum(float(item.get("product_qty") or 0)
                                         for item in lines)
                current["received"] = sum(float(item.get("qty_received") or 0)
                                          for item in lines)
            except Exception:
                pass
        else:
            continue
        state.save(source_id, {"current": current}, cursor_value=order_id)
        refreshed += 1
    return refreshed
