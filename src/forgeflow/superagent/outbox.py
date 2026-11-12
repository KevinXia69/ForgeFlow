"""Consume the Odoo addon's transactional outbox without losing events.

The addon writes one row per applied execution inside the same transaction as
the business change.  A consumer that marks the row consumed *before* admitting
the work can lose an event; one that admits before marking can re-admit after a
crash.  Admission is idempotent by ``external_event_id``, so this consumer admits
first and marks consumed only after that commit.  A repeated delivery is a
duplicate admission, not a duplicate task.

The system's own ``execution.applied`` event is linked to the original operation
and ticket; it does not start a new investigation.  A later external change to
the same object still arrives as its own event.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .engine import ControlEngine
from .heartbeat import beat
from .identity import TenantContext
from .sources import SourceEvent, _digest, admit_events
from .store import ControlStore
from .tasks import plan_task

SOURCE_ID = "odoo20.outbox"

#: Outbox event types that represent our own committed write.
SELF_EVENTS = frozenset({"execution.applied"})

#: Business event types this consumer can turn into an investigation.
WORK_EVENTS = {
    "exception.reported": "A business exception was reported",
    "purchase.cancelled": "A purchase commitment was cancelled",
    "purchase.delayed": "A purchase commitment moved its delivery date",
    "component.shortage": "A component shortage affects a manufacturing order",
    "workcenter.unavailable": "A workcenter became unavailable",
    "rule.changed": "A formal rule changed and invalidates an existing plan",
}


class OutboxError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload_json")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _object_ref(row: dict[str, Any], payload: dict[str, Any]) -> str:
    explicit = payload.get("object_ref")
    if isinstance(explicit, str) and explicit:
        return explicit
    model = str(row.get("aggregate_model") or "forgeflow.execution_operation")
    identifier = row.get("aggregate_id")
    return f"{model}:{int(identifier or 0)}"


class OutboxConsumer:
    """Drain pending outbox rows into control-engine inputs."""

    def __init__(self, store: ControlStore, adapter, ctx: TenantContext, *,
                 limit: int = 100, tick_seconds: float = 5.0) -> None:
        self.store = store
        self.adapter = adapter
        self.ctx = ctx
        self.engine = ControlEngine(store)
        self.limit = max(1, min(int(limit), 500))
        self.tick_seconds = float(tick_seconds)

    def tick(self) -> dict[str, Any]:
        beat(self.store, service_id=f"outbox:{self.ctx.tenant_id}",
             tenant_id=self.ctx.tenant_id, kind="outbox")
        rows = self.adapter.search_read(
            "forgeflow.event_outbox", [("state", "=", "pending")],
            ["id", "event_type", "aggregate_model", "aggregate_id",
             "external_event_id", "payload_json", "created_at"],
            limit=self.limit, order="id asc")
        consumed: list[int] = []
        self_linked: list[dict[str, Any]] = []
        admitted: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for row in rows:
            event_type = str(row.get("event_type") or "")
            payload = _row_payload(row)
            if event_type in SELF_EVENTS:
                try:
                    link = self._record_self_event(row, payload)
                except Exception as error:  # noqa: BLE001 - keep the row pending
                    failures.append({"id": int(row["id"]),
                                     "error": type(error).__name__})
                    continue
                consumed.append(int(row["id"]))
                self_linked.append({"id": int(row["id"]), "ticket": link})
                continue
            if event_type not in WORK_EVENTS:
                # Keep the row pending: an unknown event must not be dropped.
                unsupported.append({"id": int(row["id"]), "event_type": event_type})
                continue
            event = self._event(row, payload, event_type)
            try:
                tickets = admit_events(self.engine, self.ctx, [event],
                                       policy="auto_investigate")
            except Exception as error:  # noqa: BLE001 - the row stays pending
                failures.append({"id": int(row["id"]),
                                 "error": type(error).__name__})
                continue
            consumed.append(int(row["id"]))
            admitted.append({"id": int(row["id"]), "tickets": tickets})
        if consumed:
            try:
                self.adapter.write("forgeflow.event_outbox", consumed,
                                   {"state": "consumed",
                                    "processed_at": datetime.now(UTC).strftime(
                                        "%Y-%m-%d %H:%M:%S")})
            except Exception as error:  # noqa: BLE001 - re-delivery is safe
                failures.append({"mark_consumed": type(error).__name__,
                                 "ids": consumed})
        return {"read": len(rows), "consumed": len(consumed),
                "self_linked": self_linked, "admitted": admitted,
                "unsupported": unsupported, "failures": failures}

    def _event(self, row: dict[str, Any], payload: dict[str, Any],
               event_type: str) -> SourceEvent:
        reference = _object_ref(row, payload)
        goal = str(payload.get("goal") or WORK_EVENTS[event_type])
        condition = str(payload.get("condition") or event_type.replace(".", "_"))
        occurred = payload.get("occurred_at") or row.get("created_at")
        facts = {
            "event_type": event_type,
            "object_ref": reference,
            "outbox_id": int(row["id"]),
            "payload": payload,
        }
        digest = _digest({"event_type": event_type, "payload": payload,
                          "object_ref": reference})
        return SourceEvent(
            source_id=SOURCE_ID,
            external_event_id=str(row.get("external_event_id") or f"outbox:{row['id']}"),
            payload_hash=digest, object_ref=reference, goal=goal,
            occurred_at=(str(occurred)[:19].replace(" ", "T")
                         if occurred else None),
            facts=facts, condition=condition,
            severity=str(payload.get("severity") or "medium"),
            goal_kind=condition)

    def _record_self_event(self, row: dict[str, Any],
                           payload: dict[str, Any]) -> str | None:
        """Link our own write to its ticket without opening new work."""
        plan_id = payload.get("plan_id")
        if not plan_id:
            return None
        linked = plan_task(self.store, tenant_id=self.ctx.tenant_id,
                           plan_id=int(plan_id))
        if linked is None:
            return None
        # A transient timeline failure must leave the outbox row pending so the
        # next tick retries it; only an absent link is safely consumable.
        self.engine.append_event(
            self.ctx, ticket_id=linked.ticket_id, kind="execution_applied",
            payload={"outbox_id": int(row["id"]),
                     "operation_id": payload.get("operation_id"),
                     "action_id": payload.get("action_id"),
                     "plan_id": int(plan_id)})
        return str(linked.ticket_id)

    def run(self, stop_event) -> int:
        ticks = 0
        while not stop_event.is_set():
            self.tick()
            ticks += 1
            stop_event.wait(self.tick_seconds)
        return ticks


def outbox_loop(consumer: OutboxConsumer):
    """Compatibility helper for supervisors that expect a callable service."""
    import threading

    stop = threading.Event()

    def run() -> int:
        return consumer.run(stop)

    run.stop = stop  # type: ignore[attr-defined]
    return run
