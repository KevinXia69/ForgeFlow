"""Watchdogs for processes, tasks and executions.

Three distinct jobs (plan section 12.1), previously collapsed into one generic
"recover expired leases" sweep:

* **process** watch -- is the service alive and its dependencies reachable;
* **task** watch -- has the work actually moved, or only the heartbeat;
* **execution** watch -- was an ERP write dispatched and is its result unknown.

The important correction is that a lost lease on a ``business_write`` is *not* a
retry.  It means the outcome is unknown, so it goes to reconciliation with a
human attention and is never replayed automatically.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from .contracts import (
    RECONCILE_SIDE_EFFECTS,
    REPLAYABLE_SIDE_EFFECTS,
    AttentionNeed,
    EffectState,
    TicketState,
    normalize_ticket_state,
)
from .heartbeat import beat
from .store import ControlStore

#: Ticket states that are legitimately parked waiting for someone else.
PARKED_STATES: frozenset[str] = frozenset({
    TicketState.WAITING_INPUT.value,
    TicketState.AWAITING_APPROVAL.value,
    TicketState.WAITING_EVENT.value,
})


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    max_attempts: int = 3
    backoff_seconds: int = 1
    #: A live ticket with no recorded progress for this long is stalled.
    stagnation_seconds: int = 900
    #: How long a ``reconcile_required`` business write may stay unresolved.
    reconcile_alert_seconds: int = 300


@dataclass(slots=True)
class SweepReport:
    recovered: int = 0
    failed: int = 0
    reconciling: int = 0
    stalled: int = 0
    budget_exhausted: int = 0
    mandates_expired: int = 0
    dependencies_released: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"recovered": self.recovered, "failed": self.failed,
                "reconciling": self.reconciling, "stalled": self.stalled,
                "budget_exhausted": self.budget_exhausted,
                "mandates_expired": self.mandates_expired,
                "dependencies_released": self.dependencies_released}

    def __iadd__(self, other: "SweepReport") -> "SweepReport":
        for key in self.as_dict():
            setattr(self, key, getattr(self, key) + getattr(other, key))
        return self


class EffectWatchdog:
    def __init__(self, store: ControlStore, *, max_attempts: int = 3,
                 backoff_seconds: int = 1, stagnation_seconds: int = 900,
                 reconcile_alert_seconds: int = 300) -> None:
        if max_attempts < 1 or backoff_seconds < 0:
            raise ValueError("invalid_watchdog_policy")
        if stagnation_seconds < 1 or reconcile_alert_seconds < 1:
            raise ValueError("invalid_watchdog_policy")
        self.store = store
        self.policy = RecoveryPolicy(max_attempts, backoff_seconds, stagnation_seconds,
                                     reconcile_alert_seconds)

    # -- execution / lease watch --------------------------------------------
    def reap_expired(self, *, tenant_id: str | None = None,
                     now: datetime | None = None) -> dict[str, int]:
        moment = now or datetime.now(UTC)
        report = SweepReport()
        with self.store.transaction() as cursor:
            query = (
                "SELECT id, tenant_id, ticket_id, attempt, kind, side_effect_class, "
                "total_deadline_at FROM control_effects "
                "WHERE state='running' AND lease_expires_at < %s "
                + ("AND tenant_id=%s " if tenant_id else "")
                + "ORDER BY lease_expires_at FOR UPDATE SKIP LOCKED LIMIT 100")
            params: list[Any] = [moment]
            if tenant_id:
                params.append(tenant_id)
            rows = cursor.execute(query, tuple(params)).fetchall()
            for row in rows:
                effect_tenant = str(row["tenant_id"])
                ticket_id = str(row["ticket_id"])
                side_effect = str(row["side_effect_class"])
                deadline_hit = (row["total_deadline_at"] is not None
                                and row["total_deadline_at"] < moment)
                if side_effect in {item.value for item in RECONCILE_SIDE_EFFECTS}:
                    state = EffectState.RECONCILE_REQUIRED.value
                    report.reconciling += 1
                elif int(row["attempt"]) >= self.policy.max_attempts or deadline_hit:
                    state = EffectState.FAILED.value
                    report.failed += 1
                elif side_effect in {item.value for item in REPLAYABLE_SIDE_EFFECTS}:
                    state = EffectState.PENDING.value
                    report.recovered += 1
                else:
                    # An unknown side effect class is not silently retried.
                    state = EffectState.FAILED.value
                    report.failed += 1
                self._release(cursor, row, state, moment, reason="lease_expired")
                if state == EffectState.RECONCILE_REQUIRED.value:
                    self._escalate_unknown_execution(cursor, effect_tenant, ticket_id, row)
                elif state == EffectState.FAILED.value:
                    self._escalate_failure(cursor, effect_tenant, ticket_id, row,
                                           deadline_hit=deadline_hit)
        return report.as_dict()

    def _release(self, cursor, row, state: str, moment: datetime, *, reason: str) -> None:
        tenant_id, ticket_id = str(row["tenant_id"]), str(row["ticket_id"])
        available = moment + timedelta(seconds=self.policy.backoff_seconds)
        cursor.execute(
            "UPDATE control_effects SET state=%s, lease_owner=NULL, lease_expires_at=NULL, "
            "fencing_token=NULL, available_at=CASE WHEN %s='pending' THEN %s "
            "ELSE available_at END, "
            "reconcile_state=CASE WHEN %s='reconcile_required' THEN 'unknown' "
            "ELSE reconcile_state END, updated_at=now() WHERE id=%s",
            (state, state, available, state, row["id"]))
        sequence = self._event(cursor, tenant_id, ticket_id, "lease_recovered", {
            "effect_id": str(row["id"]), "state": state, "attempt": int(row["attempt"]),
            "reason": reason, "side_effect_class": str(row["side_effect_class"])})
        return sequence

    def _event(self, cursor, tenant_id: str, ticket_id: str, kind: str,
               payload: dict) -> int:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                       (f"events:{tenant_id}:{ticket_id}",))
        row = cursor.execute(
            "INSERT INTO control_ticket_sequences(tenant_id, ticket_id, last_sequence) "
            "VALUES (%s, %s, 1) ON CONFLICT (tenant_id, ticket_id) DO UPDATE "
            "SET last_sequence = control_ticket_sequences.last_sequence + 1 "
            "RETURNING last_sequence", (tenant_id, ticket_id)).fetchone()
        sequence = int(row["last_sequence"])
        cursor.execute(
            "INSERT INTO control_events(tenant_id, ticket_id, sequence, kind, payload) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, ticket_id, sequence, kind, Jsonb(payload)))
        return sequence

    def _escalate_unknown_execution(self, cursor, tenant_id: str, ticket_id: str,
                                    row) -> None:
        self._open_attention(
            cursor, tenant_id, ticket_id, AttentionNeed.RESULT_UNKNOWN.value,
            "An ERP write lost its execution lease. Its result is unknown; reconcile "
            "it against the original receipt before retrying anything.",
            {"effect_id": str(row["id"]), "operation": str(row["kind"])})
        self._set_ticket_state(cursor, tenant_id, ticket_id, TicketState.BLOCKED.value,
                               "execution_result_unknown")

    def _escalate_failure(self, cursor, tenant_id: str, ticket_id: str, row, *,
                          deadline_hit: bool) -> None:
        need = AttentionNeed.BUDGET.value if deadline_hit else AttentionNeed.FAILURE.value
        question = ("This task passed its total execution deadline. Review whether to "
                    "re-authorize it." if deadline_hit else
                    "Work stopped after exhausting its retry budget. Review and retry?")
        self._open_attention(cursor, tenant_id, ticket_id, need, question,
                             {"effect_id": str(row["id"]),
                              "attempt": int(row["attempt"])})
        self._set_ticket_state(cursor, tenant_id, ticket_id, TicketState.BLOCKED.value,
                               "retry_budget_exhausted" if not deadline_hit
                               else "effect_deadline_exceeded")

    def _open_attention(self, cursor, tenant_id: str, ticket_id: str, need_type: str,
                        question: str, detail: dict) -> None:
        state = cursor.execute(
            "SELECT state FROM control_tickets WHERE tenant_id=%s AND id=%s",
            (tenant_id, ticket_id)).fetchone()
        if state is None or normalize_ticket_state(state["state"]) in {
                TicketState.CLOSED.value, TicketState.CANCELLED.value}:
            return
        existing = cursor.execute(
            "SELECT id FROM control_attentions WHERE tenant_id=%s AND ticket_id=%s "
            "AND need_type=%s AND state='open'", (tenant_id, ticket_id, need_type)).fetchone()
        if existing is not None:
            return
        cursor.execute(
            "INSERT INTO control_attentions(id, tenant_id, ticket_id, need_type, state, "
            "question, blocking, detail) VALUES (%s, %s, %s, %s, 'open', %s, true, %s)",
            (str(uuid.uuid4()), tenant_id, ticket_id, need_type, question[:500],
             Jsonb(detail)))

    def _set_ticket_state(self, cursor, tenant_id: str, ticket_id: str, state: str,
                          reason: str) -> None:
        current = cursor.execute(
            "SELECT state FROM control_tickets WHERE id=%s AND tenant_id=%s FOR UPDATE",
            (ticket_id, tenant_id)).fetchone()
        if current is None:
            return
        normalised = normalize_ticket_state(current["state"])
        if normalised == state or normalised in {TicketState.CLOSED.value,
                                                 TicketState.CANCELLED.value,
                                                 TicketState.COMPLETED.value}:
            return
        cursor.execute(
            "UPDATE control_tickets SET state=%s, version=version+1, stop_reason=%s, "
            "updated_at=now() WHERE id=%s AND tenant_id=%s",
            (state, reason, ticket_id, tenant_id))
        self._event(cursor, tenant_id, ticket_id, "ticket_state", {
            "from": normalised, "to": state, "reason": reason})

    # -- task / progress watch ----------------------------------------------
    def check_progress(self, *, tenant_id: str | None = None,
                       now: datetime | None = None) -> dict[str, int]:
        """Stop work that is alive but not advancing.

        A heartbeat proves a process exists.  It does not prove the investigation
        found anything, so progress is judged from the ticket's own progress
        marker and its budget ledger.
        """
        moment = now or datetime.now(UTC)
        stalled_before = moment - timedelta(seconds=self.policy.stagnation_seconds)
        report = SweepReport()
        with self.store.transaction() as cursor:
            query = (
                "SELECT id, tenant_id, state, spent_tokens, budget_tokens, progress_at, "
                "deadline_at FROM control_tickets "
                "WHERE state = ANY(%s) "
                + ("AND tenant_id=%s " if tenant_id else "")
                + "FOR UPDATE SKIP LOCKED LIMIT 200")
            params: list[Any] = [[state for state in PARKED_STATES | {
                TicketState.QUEUED.value, TicketState.RUNNING.value,
                TicketState.RESULT_READY.value, TicketState.EXECUTING.value,
                TicketState.VERIFYING.value}]]
            if tenant_id:
                params.append(tenant_id)
            rows = cursor.execute(query, tuple(params)).fetchall()
            for row in rows:
                ticket_tenant, ticket_id = str(row["tenant_id"]), str(row["id"])
                state = normalize_ticket_state(row["state"])
                budget = row["budget_tokens"]
                if budget is not None and int(row["spent_tokens"]) > int(budget):
                    report.budget_exhausted += 1
                    self._open_attention(
                        cursor, ticket_tenant, ticket_id, AttentionNeed.BUDGET.value,
                        "This task spent its whole token budget before finishing. "
                        "Review whether to re-authorize it with a larger budget.",
                        {"spent_tokens": int(row["spent_tokens"]),
                         "budget_tokens": int(budget)})
                    self._set_ticket_state(cursor, ticket_tenant, ticket_id,
                                           TicketState.BLOCKED.value, "task_budget_exhausted")
                    continue
                if state in PARKED_STATES:
                    continue
                if row["progress_at"] is None or row["progress_at"] > stalled_before:
                    continue
                report.stalled += 1
                self._open_attention(
                    cursor, ticket_tenant, ticket_id, AttentionNeed.BUDGET.value,
                    "This task has not advanced for longer than the progress window. "
                    "Review it, or let it run once with renewed authorization.",
                    {"stalled_since": row["progress_at"].isoformat()
                     if row["progress_at"] else None})
                self._set_ticket_state(cursor, ticket_tenant, ticket_id,
                                       TicketState.BLOCKED.value, "task_stalled")
        return report.as_dict()

    # -- recovery classification --------------------------------------------
    def reconcile_required(self, *, tenant_id: str | None = None,
                           now: datetime | None = None) -> list[dict]:
        """List executions whose outcome is still unknown; never replays them."""
        moment = now or datetime.now(UTC)
        alert_before = moment - timedelta(seconds=self.policy.reconcile_alert_seconds)
        query = (
            "SELECT id, tenant_id, ticket_id, kind, reconcile_state, updated_at "
            "FROM control_effects WHERE state='reconcile_required' "
            + ("AND tenant_id=%s " if tenant_id else "")
            + "ORDER BY updated_at LIMIT 100")
        rows = self.store.connection.execute(
            query, (tenant_id,) if tenant_id else ()).fetchall()
        self.store.connection.commit()
        return [{"effect_id": str(row["id"]), "tenant_id": str(row["tenant_id"]),
                 "ticket_id": str(row["ticket_id"]), "kind": str(row["kind"]),
                 "reconcile_state": row["reconcile_state"],
                 "overdue": bool(row["updated_at"] and row["updated_at"] < alert_before)}
                for row in rows]

    def resolve_reconcile(self, *, tenant_id: str, effect_id: str,
                          confirmed: bool, detail: dict | None = None) -> dict:
        """Record the reconciled outcome of an unknown ERP write."""
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "UPDATE control_effects SET reconcile_state=%s, reconcile_detail=%s, "
                "state='completed', updated_at=now() "
                "WHERE id=%s AND tenant_id=%s AND state='reconcile_required' "
                "RETURNING ticket_id, kind",
                ("confirmed_applied" if confirmed else "confirmed_not_applied",
                 Jsonb(detail or {}), effect_id, tenant_id)).fetchone()
            if row is None:
                raise LookupError("reconcile_effect_missing")
            ticket_id = str(row["ticket_id"])
            self._event(cursor, tenant_id, ticket_id, "execution_reconciled", {
                "effect_id": effect_id, "confirmed_applied": bool(confirmed),
                "detail": detail or {}})
            cursor.execute(
                "UPDATE control_attentions SET state='answered', updated_at=now() "
                "WHERE tenant_id=%s AND ticket_id=%s AND need_type='result_unknown' "
                "AND state='open'", (tenant_id, ticket_id))
            self._set_ticket_state(cursor, tenant_id, ticket_id,
                                   TicketState.VERIFYING.value if confirmed
                                   else TicketState.COMPLETED.value,
                                   "execution_reconciled")
        return {"effect_id": effect_id, "ticket_id": ticket_id,
                "confirmed_applied": bool(confirmed)}

    # -- dependencies and mandates ------------------------------------------
    def release_dependencies(self) -> int:
        """Fail subtasks whose parent finished without satisfying them."""
        with self.store.transaction() as cursor:
            rows = cursor.execute(
                "UPDATE control_task_dependencies d SET state='cancelled', "
                "satisfied_at=now() FROM control_tickets t "
                "WHERE t.id=d.depends_on_ticket_id AND t.tenant_id=d.tenant_id "
                "AND d.state='pending' "
                "AND t.state IN ('failed','cancelled','closed') RETURNING d.id"
            ).fetchall()
        return len(rows)

    def expire_mandates(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        with self.store.transaction() as cursor:
            rows = cursor.execute(
                "UPDATE control_mandates SET state='expired', revision=revision+1, "
                "revoked_at=%s, revoke_reason='deadline_passed' "
                "WHERE state='active' AND deadline_at IS NOT NULL AND deadline_at < %s "
                "RETURNING id", (moment, moment)).fetchall()
        return len(rows)

    # -- aggregate -----------------------------------------------------------
    def sweep(self, *, tenant_id: str | None = None,
              now: datetime | None = None) -> dict[str, int]:
        moment = now or datetime.now(UTC)
        report = SweepReport()
        for part in (self.reap_expired(tenant_id=tenant_id, now=moment),
                     self.check_progress(tenant_id=tenant_id, now=moment)):
            for key, value in part.items():
                setattr(report, key, getattr(report, key) + value)
        report.mandates_expired = self.expire_mandates(now=moment)
        report.dependencies_released = self.release_dependencies()
        return report.as_dict()


class ProcessWatchdog:
    """Process-level monitoring: is the service alive and is a dependency down?

    The in-process supervisor records thread errors; this adds a persisted
    liveness judgement that survives a restart.
    """

    def __init__(self, store: ControlStore, *, stale_seconds: int = 90) -> None:
        if stale_seconds < 1:
            raise ValueError("invalid_process_watchdog")
        self.store = store
        self.stale_seconds = int(stale_seconds)

    def status(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        query = ("SELECT service_id, tenant_id, kind, last_seen, detail, "
                 "(last_seen < now() - make_interval(secs => %s)) AS stale "
                 "FROM control_service_heartbeats")
        params: list[Any] = [self.stale_seconds]
        if tenant_id:
            query += " WHERE tenant_id=%s"
            params.append(tenant_id)
        query += " ORDER BY service_id"
        rows = self.store.connection.execute(query, tuple(params)).fetchall()
        self.store.connection.commit()
        services = [dict(row) for row in rows]
        stale = [row for row in services if row["stale"]]
        return {"ok": not stale, "services": services,
                "stale": [row["service_id"] for row in stale]}


class WatchdogLoop:
    def __init__(self, watchdog: EffectWatchdog, *, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("invalid_watchdog_interval")
        self.watchdog = watchdog
        self.interval_seconds = float(interval_seconds)

    def run(self, stop_event: threading.Event) -> dict[str, int]:
        totals: dict[str, int] = {}
        while not stop_event.is_set():
            beat(self.watchdog.store, service_id="watchdog", tenant_id=None, kind="watchdog")
            for key, value in self.watchdog.sweep().items():
                totals[key] = totals.get(key, 0) + value
            stop_event.wait(self.interval_seconds)
        return totals
