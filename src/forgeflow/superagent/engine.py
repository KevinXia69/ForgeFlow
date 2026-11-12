"""Authoritative control engine: the single writer of task-domain state.

Every state change in the Super-agent control layer goes through a command
method on this object.  ``tasks.py``, the dispatcher, the watchdog, the source
service and the Router submit *requests* here; none of them may write
``control_tickets``, ``control_assignments``, ``control_attentions``,
``control_effects`` or ``control_events`` directly.

What that buys, per the target architecture:

* a state change and its event commit in the same transaction;
* completing an effect, storing a worker result, moving a ticket and opening an
  attention can no longer half-apply;
* a late assignment result cannot win a race against a newer state, because the
  fencing token is checked inside the same transaction that writes the result;
* event sequence numbers are allocated atomically, so two processes never pick
  the same ``MAX(sequence)+1``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from uuid import uuid4

from psycopg.types.json import Jsonb

from .contracts import (
    ACTIVE_TICKET_STATES,
    TERMINAL_TICKET_STATES,
    Admission,
    AssignmentState,
    AttentionNeed,
    EffectKind,
    EffectState,
    Mandate,
    MandateGrant,
    MandateScopeMode,
    MandateState,
    TicketState,
    WorkerReport,
    normalize_ticket_state,
    phase_for,
    side_effect_for,
    user_state_for,
)
from .identity import Role, TenantContext, TenantError
from .store import ControlStore


class StalePolicy(RuntimeError):
    pass


class TriggerConflict(RuntimeError):
    pass


class FencingError(RuntimeError):
    pass


class ControlNotFound(LookupError):
    pass


class MandateDenied(TenantError):
    """A per-call authorization failure.  Raised before any work is dispatched."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StateTransitionRejected(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class LateResultRejected(RuntimeError):
    """A result arrived from an attempt that no longer owns the work."""


class BudgetExhausted(RuntimeError):
    def __init__(self, code: str = "task_budget_exhausted") -> None:
        super().__init__(code)
        self.code = code


#: Legal ticket transitions.  Everything else is a programming error and is
#: rejected instead of silently persisted.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    TicketState.QUEUED.value: frozenset({
        TicketState.RUNNING.value, TicketState.WAITING_INPUT.value,
        TicketState.WAITING_EVENT.value, TicketState.RESULT_READY.value,
        TicketState.AWAITING_APPROVAL.value,
        # A plan may be prepared and approved before any investigation effect runs.
        TicketState.EXECUTING.value,
        TicketState.BLOCKED.value, TicketState.FAILED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    TicketState.RUNNING.value: frozenset({
        TicketState.QUEUED.value, TicketState.RESULT_READY.value,
        TicketState.WAITING_INPUT.value, TicketState.WAITING_EVENT.value,
        TicketState.AWAITING_APPROVAL.value, TicketState.EXECUTING.value,
        TicketState.BLOCKED.value, TicketState.FAILED.value,
        TicketState.CANCELLED.value, TicketState.COMPLETED.value,
        TicketState.CLOSED.value}),
    TicketState.WAITING_INPUT.value: frozenset({
        TicketState.QUEUED.value, TicketState.RUNNING.value,
        TicketState.WAITING_EVENT.value, TicketState.RESULT_READY.value,
        TicketState.BLOCKED.value, TicketState.FAILED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    TicketState.WAITING_EVENT.value: frozenset({
        TicketState.QUEUED.value, TicketState.RUNNING.value,
        TicketState.WAITING_INPUT.value, TicketState.RESULT_READY.value,
        TicketState.BLOCKED.value, TicketState.FAILED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    TicketState.RESULT_READY.value: frozenset({
        TicketState.QUEUED.value, TicketState.RUNNING.value,
        TicketState.WAITING_EVENT.value, TicketState.AWAITING_APPROVAL.value,
        TicketState.EXECUTING.value, TicketState.COMPLETED.value,
        TicketState.BLOCKED.value, TicketState.FAILED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    TicketState.AWAITING_APPROVAL.value: frozenset({
        TicketState.QUEUED.value, TicketState.RUNNING.value,
        TicketState.RESULT_READY.value, TicketState.EXECUTING.value,
        TicketState.VERIFYING.value, TicketState.BLOCKED.value,
        TicketState.FAILED.value, TicketState.CANCELLED.value,
        TicketState.CLOSED.value}),
    TicketState.EXECUTING.value: frozenset({
        TicketState.VERIFYING.value, TicketState.BLOCKED.value,
        TicketState.FAILED.value, TicketState.COMPLETED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    TicketState.VERIFYING.value: frozenset({
        TicketState.EXECUTING.value, TicketState.BLOCKED.value,
        TicketState.FAILED.value, TicketState.COMPLETED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    TicketState.BLOCKED.value: frozenset({
        TicketState.QUEUED.value, TicketState.RUNNING.value,
        TicketState.WAITING_INPUT.value, TicketState.WAITING_EVENT.value,
        TicketState.RESULT_READY.value, TicketState.FAILED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    # A failed task may be retried by an operator, never by a normal event.
    TicketState.FAILED.value: frozenset({
        TicketState.QUEUED.value, TicketState.BLOCKED.value,
        TicketState.CANCELLED.value, TicketState.CLOSED.value}),
    TicketState.COMPLETED.value: frozenset({TicketState.CLOSED.value}),
    TicketState.CANCELLED.value: frozenset({TicketState.CLOSED.value}),
    TicketState.CLOSED.value: frozenset(),
}


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _goal_kind(goal: str) -> str:
    return " ".join(str(goal).lower().split())[:120]


class ControlEngine:
    def __init__(self, store: ControlStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ auth
    def _authorize(self, cursor, ctx: TenantContext) -> None:
        row = cursor.execute(
            "SELECT policy_version FROM control_tenants WHERE id=%s", (ctx.tenant_id,)).fetchone()
        if row is None:
            raise TenantError("tenant_not_found")
        if row["policy_version"] != ctx.policy_version:
            raise StalePolicy("policy_version_stale")
        membership = cursor.execute(
            "SELECT 1 FROM control_principals WHERE tenant_id=%s AND id=%s AND role=ANY(%s)",
            (ctx.tenant_id, ctx.principal_id, [role.value for role in ctx.roles])).fetchone()
        if membership is None:
            raise TenantError("principal_scope_denied")

    def _policy_version(self, cursor, tenant_id: str) -> int:
        row = cursor.execute(
            "SELECT policy_version FROM control_tenants WHERE id=%s", (tenant_id,)).fetchone()
        if row is None:
            raise TenantError("tenant_not_found")
        return int(row["policy_version"])

    # ------------------------------------------------------------- sequences
    def _next_sequence(self, cursor, tenant_id: str, ticket_id: str) -> int:
        """Allocate the next per-ticket event sequence atomically.

        ``ON CONFLICT DO UPDATE`` serialises concurrent writers on the ticket's
        sequence row, which the previous ``MAX(sequence)+1`` read could not.
        """
        # Serialise every writer for this ticket, not only the sequence row update:
        # a transaction-scoped advisory lock also covers a writer whose sequence
        # row was inserted by another path.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                       (f"events:{tenant_id}:{ticket_id}",))
        row = cursor.execute(
            "INSERT INTO control_ticket_sequences(tenant_id, ticket_id, last_sequence) "
            "VALUES (%s, %s, 1) "
            "ON CONFLICT (tenant_id, ticket_id) DO UPDATE "
            "SET last_sequence = control_ticket_sequences.last_sequence + 1 "
            "RETURNING last_sequence",
            (tenant_id, ticket_id)).fetchone()
        return int(row["last_sequence"])

    def _append_event(self, cursor, tenant_id: str, ticket_id: str, kind: str,
                      payload: dict | None = None) -> int:
        sequence = self._next_sequence(cursor, tenant_id, ticket_id)
        cursor.execute(
            "INSERT INTO control_events(tenant_id, ticket_id, sequence, kind, payload) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, ticket_id, sequence, kind, Jsonb(payload or {})))
        return sequence

    # --------------------------------------------------------------- mandates
    def _mandate_from_row(self, row) -> Mandate:
        return Mandate(
            id=str(row["id"]), tenant_id=str(row["tenant_id"]),
            principal_id=str(row["principal_id"]),
            policy_version=int(row["policy_version"]),
            capabilities=frozenset(row["capabilities"] or ()),
            tools=frozenset(row["tools"] or ()),
            knowledge_scopes=frozenset(row["knowledge_scopes"] or ()),
            scope_mode=str(row["scope_mode"]),
            object_refs=frozenset(row["object_refs"] or ()),
            budget_tokens=(int(row["budget_tokens"]) if row["budget_tokens"] is not None else None),
            budget_effects=(int(row["budget_effects"])
                            if row["budget_effects"] is not None else None),
            deadline_at=row["deadline_at"], state=str(row["state"]),
            revision=int(row["revision"]))

    def issue_mandate(self, ctx: TenantContext, *, capabilities: Iterable[str] = (),
                      tools: Iterable[str] = (), knowledge_scopes: Iterable[str] = (),
                      scope_mode: str = MandateScopeMode.OBJECTS.value,
                      object_refs: Iterable[str] = (), budget_tokens: int | None = None,
                      budget_effects: int | None = None, ttl_seconds: int | None = None,
                      metadata: dict | None = None,
                      ticket_id: str | None = None) -> Mandate:
        ctx.require(Role.ADMIN, Role.PLANNER)
        if scope_mode not in {mode.value for mode in MandateScopeMode}:
            raise ValueError("invalid_mandate_scope")
        capabilities = tuple(str(item) for item in capabilities)
        tools = tuple(str(item) for item in tools)
        knowledge_scopes = tuple(str(item) for item in knowledge_scopes)
        object_refs = tuple(str(item) for item in object_refs)
        if scope_mode == MandateScopeMode.OBJECTS.value and not object_refs:
            # An object-scoped mandate with no objects authorises nothing; that
            # is a caller bug, not a way to obtain tenant-wide access.
            raise ValueError("mandate_object_refs_required")
        if budget_tokens is not None and int(budget_tokens) <= 0:
            raise ValueError("invalid_mandate_budget")
        if budget_effects is not None and int(budget_effects) <= 0:
            raise ValueError("invalid_mandate_budget")
        deadline = _now() + timedelta(seconds=int(ttl_seconds)) if ttl_seconds else None
        mandate_id = _uuid()
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            policy_version = self._policy_version(cursor, ctx.tenant_id)
            cursor.execute(
                "INSERT INTO control_mandates(id, tenant_id, principal_id, policy_version, "
                "capabilities, tools, knowledge_scopes, scope_mode, object_refs, "
                "budget_tokens, budget_effects, deadline_at, metadata, source_ticket_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (mandate_id, ctx.tenant_id, ctx.principal_id, policy_version,
                 list(capabilities), list(tools), list(knowledge_scopes), scope_mode,
                 list(object_refs), budget_tokens, budget_effects, deadline,
                 Jsonb(metadata or {}), ticket_id))
        return Mandate(
            mandate_id, ctx.tenant_id, ctx.principal_id, policy_version,
            frozenset(capabilities), frozenset(tools), frozenset(knowledge_scopes),
            scope_mode, frozenset(object_refs), budget_tokens, budget_effects,
            deadline, MandateState.ACTIVE.value, 1)

    def get_mandate(self, ctx: TenantContext, mandate_id: str) -> Mandate:
        ctx.assert_tenant(ctx.tenant_id)
        row = self.store.connection.execute(
            "SELECT * FROM control_mandates WHERE id=%s AND tenant_id=%s",
            (mandate_id, ctx.tenant_id)).fetchone()
        if row is None:
            raise ControlNotFound("mandate_not_found")
        return self._mandate_from_row(row)

    def revoke_mandate(self, ctx: TenantContext, mandate_id: str, *,
                       reason: str = "revoked") -> int:
        """Revoke a mandate server-side.

        Running effects whose side effect is a business write are moved to
        reconciliation instead of being allowed to drift; everything else stops
        being dispatched.
        """
        ctx.require(Role.ADMIN)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            row = cursor.execute(
                "UPDATE control_mandates SET state='revoked', revoked_at=now(), "
                "revoke_reason=%s, revision=revision+1 "
                "WHERE id=%s AND tenant_id=%s AND state='active' RETURNING id",
                (str(reason)[:200], mandate_id, ctx.tenant_id)).fetchone()
            if row is None:
                raise ControlNotFound("mandate_not_active")
            tickets = cursor.execute(
                "SELECT id FROM control_tickets WHERE tenant_id=%s AND mandate_id=%s AND state <> 'closed'",
                (ctx.tenant_id, mandate_id)).fetchall()
            for ticket in tickets:
                held = cursor.execute(
                    "UPDATE control_effects SET state='pending', lease_owner=NULL, "
                    "lease_expires_at=NULL, fencing_token=NULL, updated_at=now() "
                    "WHERE tenant_id=%s AND ticket_id=%s AND state='pending' RETURNING id",
                    (ctx.tenant_id, ticket["id"])).fetchall()
                reconciled = cursor.execute(
                    "UPDATE control_effects SET state='reconcile_required', "
                    "reconcile_state='unknown', updated_at=now() "
                    "WHERE tenant_id=%s AND ticket_id=%s AND state='running' "
                    "AND side_effect_class='business_write' RETURNING id",
                    (ctx.tenant_id, ticket["id"])).fetchall()
                cursor.execute(
                    "DELETE FROM control_task_dependencies WHERE tenant_id=%s AND ticket_id=%s "
                    "AND state='pending'", (ctx.tenant_id, ticket["id"]))
                self._append_event(cursor, ctx.tenant_id, ticket["id"], "mandate_revoked", {
                    "mandate_id": mandate_id, "reason": str(reason)[:200],
                    "held_effects": [str(item["id"]) for item in held],
                    "reconciling_effects": [str(item["id"]) for item in reconciled]})
                self._transition(cursor, ctx.tenant_id, ticket["id"],
                                 TicketState.BLOCKED.value, reason="mandate_revoked")
                if reconciled:
                    self._open_attention(cursor, ctx.tenant_id, ticket["id"],
                                         AttentionNeed.RESULT_UNKNOWN.value,
                                         "Authorization was revoked during an ERP write. "
                                         "The operation result is unknown and must be "
                                         "reconciled against the original receipt.",
                                         detail={"mandate_id": mandate_id})
                else:
                    self._open_attention(cursor, ctx.tenant_id, ticket["id"],
                                         AttentionNeed.RULE.value,
                                         "Authorization for this work was revoked. Review "
                                         "whether the task should be re-authorized.",
                                         detail={"mandate_id": mandate_id})
            return len(tickets)

    def expire_mandates(self, *, now: datetime | None = None) -> int:
        moment = now or _now()
        with self.store.transaction() as cursor:
            rows = cursor.execute(
                "UPDATE control_mandates SET state='expired', revision=revision+1, "
                "revoked_at=%s, revoke_reason='deadline_passed' "
                "WHERE state='active' AND deadline_at IS NOT NULL AND deadline_at < %s "
                "RETURNING id", (moment, moment)).fetchall()
        return len(rows)

    def authorize_tool_call(self, ctx: TenantContext, *, mandate_id: str, capability: str,
                            tool: str, object_ref: str | None = None,
                            ticket_id: str | None = None,
                            cost_tokens: int = 0) -> MandateGrant:
        """Re-validate authority for one tool call against the persisted mandate.

        Called on *every* tool invocation, so revocation, policy changes and
        budget exhaustion take effect immediately rather than at the next
        session boundary.
        """
        ctx.assert_tenant(ctx.tenant_id)
        if not tool:
            raise MandateDenied("tool_required")
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "SELECT * FROM control_mandates WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (mandate_id, ctx.tenant_id)).fetchone()
            if row is None:
                raise MandateDenied("mandate_not_found")
            mandate = self._mandate_from_row(row)
            if mandate.state != MandateState.ACTIVE.value:
                raise MandateDenied("mandate_not_active")
            current_policy = self._policy_version(cursor, ctx.tenant_id)
            if mandate.policy_version != current_policy:
                raise MandateDenied("mandate_policy_stale")
            if mandate.deadline_at is not None and mandate.deadline_at < _now():
                raise MandateDenied("mandate_expired")
            if mandate.principal_id != ctx.principal_id:
                raise MandateDenied("mandate_principal_mismatch")
            if not mandate.permits_capability(capability):
                raise MandateDenied("mandate_capability_denied")
            if not mandate.permits_tool(tool):
                raise MandateDenied("mandate_tool_denied")
            if object_ref is not None and not mandate.permits_object(object_ref):
                raise MandateDenied("mandate_object_denied")
            if ticket_id is not None:
                ticket = cursor.execute(
                    "SELECT mandate_id, state FROM control_tickets "
                    "WHERE id=%s AND tenant_id=%s", (ticket_id, ctx.tenant_id)).fetchone()
                if ticket is None:
                    raise MandateDenied("ticket_not_found")
                if ticket["mandate_id"] is not None and str(ticket["mandate_id"]) != mandate.id:
                    raise MandateDenied("mandate_ticket_mismatch")
                if normalize_ticket_state(ticket["state"]) in {
                        TicketState.CANCELLED.value, TicketState.CLOSED.value}:
                    raise MandateDenied("ticket_not_active")
            if mandate.budget_effects is not None and ticket_id is not None:
                used = cursor.execute(
                    "SELECT count(*) AS count FROM control_assignments "
                    "WHERE tenant_id=%s AND ticket_id=%s", (ctx.tenant_id, ticket_id)).fetchone()
                if int(used["count"]) >= int(mandate.budget_effects):
                    raise MandateDenied("mandate_effect_budget_exhausted")
            if mandate.budget_tokens is not None and ticket_id is not None:
                spent = cursor.execute(
                    "SELECT COALESCE(sum(spent_tokens), 0) AS tokens FROM control_tickets "
                    "WHERE tenant_id=%s AND id=%s", (ctx.tenant_id, ticket_id)).fetchone()
                if int(spent["tokens"]) + max(0, int(cost_tokens)) > int(mandate.budget_tokens):
                    raise MandateDenied("mandate_token_budget_exhausted")
        return MandateGrant(mandate.id, mandate.tenant_id, mandate.principal_id,
                            capability, object_ref, mandate.policy_version,
                            mandate.deadline_at)

    # ------------------------------------------------------------- admission
    def admit_trigger(self, ctx: TenantContext, *, source_id: str, external_event_id: str,
                      payload_hash: str, object_ref: str, goal: str,
                      initial_effect_kind: str | None = EffectKind.ASSESS.value,
                      initial_payload: dict[str, Any] | None = None,
                      goal_kind: str | None = None, mandate_id: str | None = None,
                      merge: bool = True, occurred_at: datetime | None = None,
                      trust_level: str = "untrusted",
                      capability: str | None = None,
                      idempotency_key: str | None = None) -> Admission:
        ctx.require(Role.ADMIN, Role.PLANNER)
        ctx.assert_tenant(ctx.tenant_id)
        if not source_id or not external_event_id or not payload_hash or not object_ref or not goal:
            raise ValueError("trigger_fields_required")
        resolved_kind = goal_kind or _goal_kind(goal)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            trigger_id = _uuid()
            inserted = cursor.execute(
                "INSERT INTO control_triggers(id, tenant_id, source_id, external_event_id, "
                "payload_hash, occurred_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, source_id, external_event_id) DO NOTHING RETURNING id",
                (trigger_id, ctx.tenant_id, source_id, external_event_id, payload_hash,
                 occurred_at)).fetchone()
            if inserted is None:
                existing = cursor.execute(
                    "SELECT id, payload_hash FROM control_triggers "
                    "WHERE tenant_id=%s AND source_id=%s AND external_event_id=%s",
                    (ctx.tenant_id, source_id, external_event_id)).fetchone()
                if existing is None:
                    raise RuntimeError("trigger_admission_race")
                if existing["payload_hash"] != payload_hash:
                    # Same stable event id, different body: alarm, never overwrite.
                    raise TriggerConflict("external_event_payload_changed")
                admission = cursor.execute(
                    "SELECT ticket_id FROM control_trigger_admissions WHERE trigger_id=%s",
                    (existing["id"],)).fetchone()
                if admission is None:
                    raise RuntimeError("trigger_admission_missing")
                return Admission(str(existing["id"]), str(admission["ticket_id"]),
                                 duplicate=True, thread_id=None, mandate_id=None)

            thread_id = self._resolve_thread(cursor, ctx.tenant_id, object_ref)
            resolved_mandate = self._resolve_mandate(
                cursor, ctx, mandate_id=mandate_id, object_ref=object_ref,
                capability=capability, goal=goal)

            attached_ticket = (self._active_ticket_in_thread(cursor, ctx.tenant_id, thread_id,
                                                            resolved_kind)
                               if merge else None)
            if attached_ticket is not None:
                ticket_id = str(attached_ticket["id"])
                input_id = self._insert_input(
                    cursor, ctx.tenant_id, ticket_id, trigger_id=trigger_id,
                    body={"source_id": source_id, "external_event_id": external_event_id,
                          "object_ref": object_ref, "payload_hash": payload_hash,
                          "facts": (initial_payload or {}).get("input", {})},
                    kind="event", dedupe_key=f"trigger:{trigger_id}",
                    payload_hash=payload_hash, trust_level=trust_level)
                cursor.execute(
                    "INSERT INTO control_trigger_admissions(trigger_id, ticket_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING", (trigger_id, ticket_id))
                cursor.execute(
                    "UPDATE control_threads SET revision=revision+1 WHERE id=%s", (thread_id,))
                self._append_event(cursor, ctx.tenant_id, ticket_id, "input_attached", {
                    "trigger_id": trigger_id, "source_id": source_id,
                    "object_ref": object_ref, "input_id": input_id})
                current = normalize_ticket_state(attached_ticket["state"])
                if current == TicketState.WAITING_EVENT.value:
                    queued = cursor.execute(
                        "SELECT count(*) AS count FROM control_effects WHERE tenant_id=%s "
                        "AND ticket_id=%s AND state='pending'",
                        (ctx.tenant_id, ticket_id)).fetchone()["count"]
                    # An observed-only ticket has no pending work; a new event
                    # promotes it into an actual investigation.
                    self._transition(cursor, ctx.tenant_id, ticket_id,
                                     TicketState.QUEUED.value,
                                     reason="new_input" if int(queued) else "observe_promoted")
                self._wake_ticket(cursor, ctx.tenant_id, ticket_id)
                return Admission(str(trigger_id), ticket_id, duplicate=False,
                                 thread_id=thread_id, mandate_id=resolved_mandate,
                                 attached=True, input_id=input_id)

            ticket_id = _uuid()
            cursor.execute(
                "INSERT INTO control_tickets(id, tenant_id, thread_id, goal, goal_kind, state, "
                "policy_version, mandate_id, created_by, progress_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                (ticket_id, ctx.tenant_id, thread_id, goal, resolved_kind,
                 TicketState.QUEUED.value, ctx.policy_version, resolved_mandate,
                 ctx.principal_id))
            cursor.execute(
                "INSERT INTO control_trigger_admissions(trigger_id, ticket_id) VALUES (%s, %s)",
                (trigger_id, ticket_id))
            cursor.execute(
                "INSERT INTO control_subscriptions(id, tenant_id, object_ref, ticket_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, object_ref, ticket_id) "
                "DO NOTHING", (_uuid(), ctx.tenant_id, object_ref, ticket_id))
            self._append_event(cursor, ctx.tenant_id, ticket_id, "ticket_admitted", {
                "trigger_id": trigger_id, "object_ref": object_ref,
                "mandate_id": resolved_mandate, "goal_kind": resolved_kind,
                "observed_only": initial_effect_kind is None})
            if initial_effect_kind is None:
                # "observe" policy: record the work that would have been triggered
                # without spending model budget or disturbing a user.
                self._transition(cursor, ctx.tenant_id, ticket_id,
                                 TicketState.WAITING_EVENT.value, reason="observe_only")
            else:
                self.promise_effect(
                    cursor, tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                    kind=initial_effect_kind,
                    payload={"source_id": source_id, "external_event_id": external_event_id,
                             "goal": goal, "object_ref": object_ref,
                             "mandate_id": resolved_mandate,
                             "capability": capability or initial_payload_get(initial_payload),
                             **(initial_payload or {})},
                    idempotency_key=idempotency_key,
                    mandate_id=resolved_mandate)
            return Admission(str(trigger_id), ticket_id, duplicate=False,
                             thread_id=thread_id, mandate_id=resolved_mandate)

    def _resolve_thread(self, cursor, tenant_id: str, object_ref: str) -> str:
        row = cursor.execute(
            "SELECT id FROM control_threads WHERE tenant_id=%s AND object_ref=%s",
            (tenant_id, object_ref)).fetchone()
        if row is not None:
            return str(row["id"])
        thread_id = _uuid()
        inserted = cursor.execute(
            "INSERT INTO control_threads(id, tenant_id, object_ref) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id, object_ref) DO NOTHING RETURNING id",
            (thread_id, tenant_id, object_ref)).fetchone()
        if inserted is not None:
            return thread_id
        existing = cursor.execute(
            "SELECT id FROM control_threads WHERE tenant_id=%s AND object_ref=%s",
            (tenant_id, object_ref)).fetchone()
        return str(existing["id"])

    def _active_ticket_in_thread(self, cursor, tenant_id: str, thread_id: str,
                                 goal_kind: str) -> dict | None:
        row = cursor.execute(
            "SELECT id, state FROM control_tickets "
            "WHERE tenant_id=%s AND thread_id=%s AND goal_kind=%s AND state = ANY(%s) "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant_id, thread_id, goal_kind,
             [state.value for state in ACTIVE_TICKET_STATES])).fetchone()
        return dict(row) if row is not None else None

    def _resolve_mandate(self, cursor, ctx: TenantContext, *, mandate_id: str | None,
                         object_ref: str, capability: str | None, goal: str) -> str | None:
        if mandate_id is not None:
            row = cursor.execute(
                "SELECT * FROM control_mandates WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (mandate_id, ctx.tenant_id)).fetchone()
            if row is None:
                raise MandateDenied("mandate_not_found")
            mandate = self._mandate_from_row(row)
            if mandate.state != MandateState.ACTIVE.value:
                raise MandateDenied("mandate_not_active")
            if mandate.principal_id != ctx.principal_id:
                raise MandateDenied("mandate_principal_mismatch")
            if mandate.policy_version != self._policy_version(cursor, ctx.tenant_id):
                raise MandateDenied("mandate_policy_stale")
            if not mandate.permits_object(object_ref):
                raise MandateDenied("mandate_object_denied")
            if capability and not mandate.permits_capability(capability):
                raise MandateDenied("mandate_capability_denied")
            return mandate.id
        # An admitted trigger is itself a bounded mandate request from the
        # authenticated principal: exactly this object, exactly this goal.
        new_id = _uuid()
        cursor.execute(
            "INSERT INTO control_mandates(id, tenant_id, principal_id, policy_version, "
            "capabilities, scope_mode, object_refs, metadata) "
            "VALUES (%s, %s, %s, %s, %s, 'objects', %s, %s)",
            (new_id, ctx.tenant_id, ctx.principal_id,
             self._policy_version(cursor, ctx.tenant_id),
             [capability] if capability else [], [object_ref],
             Jsonb({"goal": goal, "origin": "trigger_admission"})))
        return new_id

    def _insert_input(self, cursor, tenant_id: str, ticket_id: str, *, body: dict,
                      kind: str = "event", dedupe_key: str | None = None,
                      payload_hash: str | None = None, trigger_id: str | None = None,
                      trust_level: str = "untrusted") -> str | None:
        if dedupe_key is not None:
            existing = cursor.execute(
                "SELECT id FROM control_inputs WHERE tenant_id=%s AND ticket_id=%s "
                "AND dedupe_key=%s", (tenant_id, ticket_id, dedupe_key)).fetchone()
            if existing is not None:
                return str(existing["id"])
        input_id = _uuid()
        cursor.execute(
            "INSERT INTO control_inputs(id, tenant_id, ticket_id, trigger_id, trust_level, "
            "body, kind, dedupe_key, payload_hash, state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
            (input_id, tenant_id, ticket_id, trigger_id, trust_level, Jsonb(body),
             kind, dedupe_key, payload_hash))
        return input_id

    def attach_input(self, ctx: TenantContext, *, ticket_id: str, body: dict,
                     kind: str = "user_message", dedupe_key: str | None = None,
                     trust_level: str = "untrusted",
                     payload_hash: str | None = None) -> str:
        """Attach a new piece of context to an existing ticket.

        Context is data, never authority: attaching a message does not widen the
        ticket's mandate, tenant or object scope (plan section 4.2).
        """
        ctx.require(Role.ADMIN, Role.PLANNER, Role.APPROVER)
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            ticket = cursor.execute(
                "SELECT id, state FROM control_tickets WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (ticket_id, ctx.tenant_id)).fetchone()
            if ticket is None:
                raise ControlNotFound("ticket_not_found")
            state = normalize_ticket_state(ticket["state"])
            if state in {TicketState.CANCELLED.value, TicketState.CLOSED.value}:
                raise StateTransitionRejected("ticket_not_active",
                                              f"cannot attach input to {state}")
            input_id = self._insert_input(
                cursor, ctx.tenant_id, ticket_id, body=body, kind=kind,
                dedupe_key=dedupe_key, payload_hash=payload_hash,
                trust_level=trust_level)
            self._append_event(cursor, ctx.tenant_id, ticket_id, "input_attached", {
                "input_id": input_id, "kind": kind})
            if state == TicketState.WAITING_EVENT.value:
                self._transition(cursor, ctx.tenant_id, ticket_id,
                                 TicketState.QUEUED.value, reason="new_input")
            self._wake_ticket(cursor, ctx.tenant_id, ticket_id)
            return str(input_id)

    def consume_inputs(self, ctx: TenantContext, *, ticket_id: str,
                       assignment_id: str) -> list[dict]:
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            rows = cursor.execute(
                "UPDATE control_inputs SET state='consumed', consumed_at=now(), "
                "consumed_by=%s WHERE tenant_id=%s AND ticket_id=%s AND state='pending' "
                "RETURNING id, kind, trust_level, body, payload_hash, created_at",
                (assignment_id, ctx.tenant_id, ticket_id)).fetchall()
            return [dict(row) for row in rows]

    # ----------------------------------------------------------- state machine
    def _transition(self, cursor, tenant_id: str, ticket_id: str, to_state: str, *,
                    reason: str | None = None, expected: str | None = None) -> str:
        row = cursor.execute(
            "SELECT state, version FROM control_tickets WHERE id=%s AND tenant_id=%s FOR UPDATE",
            (ticket_id, tenant_id)).fetchone()
        if row is None:
            raise ControlNotFound("ticket_not_found")
        current = normalize_ticket_state(row["state"])
        target = normalize_ticket_state(to_state)
        if expected is not None and current != normalize_ticket_state(expected):
            raise StateTransitionRejected("unexpected_ticket_state",
                                          f"expected {expected}, found {current}")
        if current == target:
            return current
        # Any live state may fall back into a "choke" state: it needs a person,
        # it failed, or it was stopped.  Forward progress and reopening are still
        # governed by the explicit table.
        choke = {TicketState.WAITING_INPUT.value, TicketState.BLOCKED.value,
                 TicketState.FAILED.value, TicketState.CANCELLED.value}
        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            reopening = (target in choke and current not in TERMINAL_TICKET_STATES
                         and current != TicketState.CLOSED.value)
            if not reopening:
                raise StateTransitionRejected(
                    "illegal_ticket_transition", f"{current} -> {target}")
        cursor.execute(
            "UPDATE control_tickets SET state=%s, version=version+1, updated_at=now(), "
            "stop_reason=COALESCE(%s, stop_reason), "
            "closed_at=CASE WHEN %s IN ('completed','cancelled','closed') THEN now() "
            "ELSE closed_at END WHERE id=%s AND tenant_id=%s",
            (target, reason, target, ticket_id, tenant_id))
        self._append_event(cursor, tenant_id, ticket_id, "ticket_state", {
            "from": current, "to": target, "reason": reason})
        if target in {TicketState.COMPLETED.value, TicketState.CANCELLED.value,
                      TicketState.CLOSED.value, TicketState.FAILED.value}:
            self._deactivate_subscriptions(cursor, tenant_id, ticket_id)
        return target

    def transition_ticket(self, ctx: TenantContext, *, ticket_id: str, to_state: str,
                          reason: str | None = None,
                          expected: str | None = None) -> dict:
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            state = self._transition(cursor, ctx.tenant_id, ticket_id, to_state,
                                     reason=reason, expected=expected)
        return self.get_ticket(ctx, ticket_id) | {"state": state}

    def _deactivate_subscriptions(self, cursor, tenant_id: str, ticket_id: str) -> None:
        cursor.execute(
            "UPDATE control_subscriptions SET active=false, deactivated_at=now() "
            "WHERE tenant_id=%s AND ticket_id=%s AND active", (tenant_id, ticket_id))

    def _wake_ticket(self, cursor, tenant_id: str, ticket_id: str) -> None:
        """Release pending effects whose ticket was parked for an attention."""
        cursor.execute(
            "UPDATE control_effects SET available_at=now(), updated_at=now() "
            "WHERE tenant_id=%s AND ticket_id=%s AND state='pending' "
            "AND available_at > now()", (tenant_id, ticket_id))

    def record_progress(self, ctx: TenantContext, *, ticket_id: str, kind: str,
                        tokens: int = 0, effects: int = 0) -> None:
        """Record that a ticket actually advanced.

        Heartbeats prove a process is alive; this proves the *work* moved, which
        is what the progress watchdog watches.
        """
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE control_tickets SET progress_at=now(), progress_kind=%s, "
                "spent_tokens=spent_tokens + %s, spent_effects=spent_effects + %s, "
                "updated_at=now() WHERE id=%s AND tenant_id=%s",
                (str(kind)[:120], max(0, int(tokens)), max(0, int(effects)),
                 ticket_id, ctx.tenant_id))

    # -------------------------------------------------------------- effects
    def promise_effect(self, cursor, *, tenant_id: str, ticket_id: str, kind: str,
                       payload: dict | None = None, available_at: datetime | None = None,
                       total_deadline_at: datetime | None = None,
                       idempotency_key: str | None = None,
                       mandate_id: str | None = None) -> str:
        """Queue one effect.  Callable inside a caller-owned transaction."""
        side_effect = side_effect_for(kind)
        effect_id = _uuid()
        cursor.execute(
            "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload, state, "
            "available_at, side_effect_class, total_deadline_at, idempotency_key, mandate_id) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', "
            "COALESCE(%s::timestamptz, now()), %s, %s, %s, %s)",
            (effect_id, tenant_id, ticket_id, str(kind), Jsonb(payload or {}),
             available_at, side_effect.value, total_deadline_at, idempotency_key, mandate_id))
        return effect_id

    def request_effect(self, ctx: TenantContext, *, ticket_id: str, kind: str,
                       payload: dict | None = None,
                       idempotency_key: str | None = None) -> str:
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            ticket = cursor.execute(
                "SELECT mandate_id, state FROM control_tickets WHERE id=%s AND tenant_id=%s",
                (ticket_id, ctx.tenant_id)).fetchone()
            if ticket is None:
                raise ControlNotFound("ticket_not_found")
            if normalize_ticket_state(ticket["state"]) in {
                    TicketState.CANCELLED.value, TicketState.CLOSED.value}:
                raise StateTransitionRejected("ticket_not_active")
            effect_id = self.promise_effect(
                cursor, tenant_id=ctx.tenant_id, ticket_id=ticket_id, kind=kind,
                payload=payload, idempotency_key=idempotency_key,
                mandate_id=(str(ticket["mandate_id"]) if ticket["mandate_id"] else None))
            self._append_event(cursor, ctx.tenant_id, ticket_id, "effect_requested", {
                "effect_id": effect_id, "kind": str(kind)})
        return effect_id

    def claim_effect(self, worker_id: str, lease_seconds: int = 60,
                     tenant_id: str | None = None,
                     ticket_id: str | None = None) -> dict[str, Any] | None:
        """Claim one runnable effect, refusing work that is no longer authorised."""
        if not worker_id or lease_seconds < 1:
            raise ValueError("invalid_lease")
        token = _uuid()
        with self.store.transaction() as cursor:
            query = (
                "SELECT e.* FROM control_effects e "
                "JOIN control_tickets t ON t.id=e.ticket_id AND t.tenant_id=e.tenant_id "
                "WHERE e.state='pending' AND e.available_at <= now() "
                "AND t.state NOT IN ('cancelled','closed') "
                "AND (t.deadline_at IS NULL OR t.deadline_at > now()) "
                "AND NOT EXISTS (SELECT 1 FROM control_task_dependencies d "
                "                WHERE d.tenant_id=e.tenant_id AND d.ticket_id=e.ticket_id "
                "                AND d.state='pending') "
                + ("AND e.tenant_id=%s " if tenant_id else "")
                + ("AND e.ticket_id=%s " if ticket_id else "")
                + "ORDER BY e.available_at, e.created_at FOR UPDATE OF e SKIP LOCKED LIMIT 1")
            params: list[Any] = []
            if tenant_id:
                params.append(tenant_id)
            if ticket_id:
                params.append(ticket_id)
            row = cursor.execute(query, tuple(params)).fetchone()
            if row is None:
                return None
            blocked = self._claim_preconditions(cursor, row)
            if blocked is not None:
                return None
            cursor.execute(
                "UPDATE control_effects SET state='running', lease_owner=%s, "
                "lease_expires_at=%s, attempt=attempt+1, fencing_token=%s, updated_at=now() "
                "WHERE id=%s",
                (worker_id, _now() + timedelta(seconds=lease_seconds), token, row["id"]))
            row = cursor.execute(
                "SELECT * FROM control_effects WHERE id=%s", (row["id"],)).fetchone()
            result = dict(row)
            result.update(id=str(row["id"]), ticket_id=str(row["ticket_id"]),
                          state=EffectState.RUNNING.value, lease_owner=worker_id,
                          fencing_token=token,
                          lease_expires_at=_now() + timedelta(seconds=lease_seconds),
                          attempt=int(row["attempt"]))
            return result

    def _claim_preconditions(self, cursor, effect_row) -> str | None:
        """Fail closed when a claim would run unauthorised or stale work."""
        tenant_id = str(effect_row["tenant_id"])
        ticket_id = str(effect_row["ticket_id"])
        ticket = cursor.execute(
            "SELECT mandate_id, deadline_at FROM control_tickets "
            "WHERE id=%s AND tenant_id=%s", (ticket_id, tenant_id)).fetchone()
        if ticket is None:
            return "ticket_missing"
        if ticket["deadline_at"] is not None and ticket["deadline_at"] < _now():
            cursor.execute(
                "UPDATE control_effects SET state='failed', updated_at=now() WHERE id=%s",
                (effect_row["id"],))
            self._append_event(cursor, tenant_id, ticket_id, "task_deadline_exceeded", {
                "effect_id": str(effect_row["id"])})
            self._transition(cursor, tenant_id, ticket_id, TicketState.BLOCKED.value,
                             reason="task_deadline_exceeded")
            self._open_attention(cursor, tenant_id, ticket_id, AttentionNeed.BUDGET.value,
                                 "This task passed its total deadline before the work "
                                 "finished. Review whether it should be re-authorized.")
            return "task_deadline_exceeded"
        mandate_ref = ticket["mandate_id"] or effect_row["mandate_id"]
        if not mandate_ref:
            return None
        mandate = cursor.execute(
            "SELECT * FROM control_mandates WHERE id=%s AND tenant_id=%s",
            (mandate_ref, tenant_id)).fetchone()
        if mandate is None:
            self._abort_effect(cursor, tenant_id, ticket_id, str(effect_row["id"]),
                               "mandate_unavailable")
            return "mandate_unavailable"
        if str(mandate["state"]) != MandateState.ACTIVE.value:
            self._abort_effect(cursor, tenant_id, ticket_id, str(effect_row["id"]),
                               "mandate_revoked")
            return "mandate_revoked"
        if int(mandate["policy_version"]) != self._policy_version(cursor, tenant_id):
            cursor.execute(
                "UPDATE control_mandates SET state='expired', revision=revision+1, "
                "revoked_at=now(), revoke_reason='policy_changed' "
                "WHERE id=%s AND state='active'", (mandate["id"],))
            self._abort_effect(cursor, tenant_id, ticket_id, str(effect_row["id"]),
                               "mandate_policy_stale")
            return "mandate_policy_stale"
        return None

    def _abort_effect(self, cursor, tenant_id: str, ticket_id: str, effect_id: str,
                      code: str) -> None:
        cursor.execute(
            "UPDATE control_effects SET state='failed', updated_at=now() WHERE id=%s",
            (effect_id,))
        self._append_event(cursor, tenant_id, ticket_id, "effect_aborted", {
            "effect_id": effect_id, "reason": code})
        self._transition(cursor, tenant_id, ticket_id, TicketState.BLOCKED.value,
                         reason=code)
        self._open_attention(cursor, tenant_id, ticket_id, AttentionNeed.RULE.value,
                             "Work stopped because its authorization is no longer valid "
                             f"({code}). Re-authorize the task to continue.",
                             detail={"reason": code})

    def complete_effect(self, *, effect_id: str, tenant_id: str, fencing_token: str,
                        state: str = EffectState.COMPLETED.value,
                        detail: dict[str, Any] | None = None) -> None:
        if state not in {EffectState.COMPLETED.value, EffectState.FAILED.value,
                         EffectState.CANCELLED.value,
                         EffectState.RECONCILE_REQUIRED.value}:
            raise ValueError("invalid_effect_state")
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "UPDATE control_effects SET state=%s, updated_at=now() "
                "WHERE id=%s AND tenant_id=%s AND state='running' AND fencing_token=%s "
                "RETURNING ticket_id, kind, side_effect_class",
                (state, effect_id, tenant_id, fencing_token)).fetchone()
            if row is None:
                raise FencingError("fencing_token_rejected")
            ticket_id = str(row["ticket_id"])
            self._append_event(cursor, tenant_id, ticket_id, "effect_completed", {
                "effect_id": effect_id, "state": state, "detail": detail or {}})
            cursor.execute(
                "UPDATE control_tickets SET spent_effects=spent_effects+1, "
                "progress_at=now(), progress_kind=%s, updated_at=now() "
                "WHERE id=%s AND tenant_id=%s",
                (f"effect:{row['kind']}", ticket_id, tenant_id))

    def release_effect(self, *, effect_id: str, tenant_id: str, fencing_token: str,
                       delay_seconds: float = 0.0, code: str = "released") -> None:
        """Return an effect to the queue without counting a failure."""
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "UPDATE control_effects SET state='pending', lease_owner=NULL, "
                "lease_expires_at=NULL, fencing_token=NULL, "
                "available_at=now() + make_interval(secs => %s), updated_at=now() "
                "WHERE id=%s AND tenant_id=%s AND state='running' AND fencing_token=%s "
                "RETURNING ticket_id",
                (float(delay_seconds), effect_id, tenant_id, fencing_token)).fetchone()
            if row is None:
                raise FencingError("fencing_token_rejected")
            self._append_event(cursor, tenant_id, str(row["ticket_id"]), "effect_released", {
                "effect_id": effect_id, "reason": code})

    def pending_effect_count(self, tenant_id: str) -> int:
        row = self.store.connection.execute(
            "SELECT count(*) AS count FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant_id,)).fetchone()
        return int(row["count"])

    # ---------------------------------------------------------- assignments
    def start_assignment(self, ctx: TenantContext, *, ticket_id: str, capability: str,
                         runner: str, fencing_token: str | None = None,
                         mandate_id: str | None = None) -> str:
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            ticket = cursor.execute(
                "SELECT state, mandate_id FROM control_tickets "
                "WHERE id=%s AND tenant_id=%s FOR UPDATE", (ticket_id, ctx.tenant_id)).fetchone()
            if ticket is None:
                raise ControlNotFound("ticket_not_found")
            if normalize_ticket_state(ticket["state"]) in {
                    TicketState.CANCELLED.value, TicketState.CLOSED.value}:
                raise StateTransitionRejected("ticket_not_active")
            attempt = cursor.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM control_assignments "
                "WHERE tenant_id=%s AND ticket_id=%s", (ctx.tenant_id, ticket_id)).fetchone()["next"]
            assignment_id = _uuid()
            cursor.execute(
                "INSERT INTO control_assignments(id, tenant_id, ticket_id, runner, capability, "
                "attempt, state, fencing_token, mandate_id) VALUES (%s, %s, %s, %s, %s, %s, "
                "'running', %s, %s)",
                (assignment_id, ctx.tenant_id, ticket_id, runner, capability, attempt,
                 fencing_token, mandate_id or ticket["mandate_id"]))
            self._append_event(cursor, ctx.tenant_id, ticket_id, "assignment_started", {
                "assignment_id": assignment_id, "runner": runner,
                "capability": capability, "attempt": int(attempt)})
        return assignment_id

    def record_worker_result(self, ctx: TenantContext, *, ticket_id: str,
                             assignment_id: str, report: WorkerReport | dict,
                             fencing_token: str | None = None) -> str:
        """Store a worker result, rejecting results from attempts that lost the work.

        Both the assignment state and the ticket/effect fencing token are checked
        inside the writing transaction, so a late result cannot overwrite a newer
        state or resurrect a cancelled task.  A rejection is *recorded* before the
        caller sees the exception: the bookkeeping must not live in the
        transaction that the exception rolls back.
        """
        payload = report.as_dict() if isinstance(report, WorkerReport) else dict(report)
        status = str(payload.get("status", "failed"))
        if status not in {"completed", "needs_input", "waiting_event", "failed"}:
            raise ValueError("invalid_worker_status")
        rejection: str | None = None
        with self.store.transaction() as cursor:
            ticket = cursor.execute(
                "SELECT state, mandate_id FROM control_tickets "
                "WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (ticket_id, ctx.tenant_id)).fetchone()
            if ticket is None:
                raise ControlNotFound("ticket_not_found")
            state = normalize_ticket_state(ticket["state"])
            assignment = cursor.execute(
                "SELECT id, state, fencing_token FROM control_assignments "
                "WHERE id=%s AND tenant_id=%s AND ticket_id=%s FOR UPDATE",
                (assignment_id, ctx.tenant_id, ticket_id)).fetchone()
            if assignment is None:
                raise ControlNotFound("assignment_not_found")
            if state in {TicketState.CANCELLED.value, TicketState.CLOSED.value} or \
                    assignment["state"] == AssignmentState.CANCELLED.value:
                rejection = "ticket_cancelled"
            elif assignment["state"] in {AssignmentState.COMPLETED.value,
                                         AssignmentState.FAILED.value,
                                         AssignmentState.REJECTED.value,
                                         AssignmentState.EXPIRED.value}:
                rejection = "assignment_already_finalized"
            else:
                recorded_token = (assignment["fencing_token"]
                                  or payload.get("fencing_token") or fencing_token)
                if recorded_token and assignment["fencing_token"] is not None:
                    owned = cursor.execute(
                        "SELECT 1 FROM control_effects WHERE tenant_id=%s AND ticket_id=%s "
                        "AND state='running' AND fencing_token=%s",
                        (ctx.tenant_id, ticket_id, recorded_token)).fetchone()
                    if owned is None:
                        rejection = "fencing_token_stale"
            if rejection is None:
                return self._commit_worker_result(
                    cursor, ctx, ticket_id=ticket_id, assignment_id=assignment_id,
                    payload=payload, status=status, state=state)
        self._record_rejection(ctx.tenant_id, ticket_id=ticket_id,
                               assignment_id=assignment_id, reason=rejection,
                               payload=payload,
                               mark_assignment=rejection != "assignment_already_finalized")
        raise LateResultRejected(rejection)

    def _commit_worker_result(self, cursor, ctx: TenantContext, *, ticket_id: str,
                              assignment_id: str, payload: dict, status: str,
                              state: str) -> str:
        final_state = ("completed" if status in {"completed", "needs_input", "waiting_event"}
                       else "failed")
        cursor.execute(
            "UPDATE control_assignments SET state=%s, result_json=%s, ceased_at=now(), "
            "updated_at=now() WHERE id=%s",
            (final_state, Jsonb(payload), assignment_id))
        usage = payload.get("usage") or {}
        tokens = int(usage.get("tokens") or 0) if isinstance(usage, dict) else 0
        cursor.execute(
            "UPDATE control_tickets SET spent_tokens=spent_tokens+%s, progress_at=now(), "
            "progress_kind='worker_result', updated_at=now() "
            "WHERE id=%s AND tenant_id=%s", (max(0, tokens), ticket_id, ctx.tenant_id))
        self._append_event(cursor, ctx.tenant_id, ticket_id, "worker_result", {
            "assignment_id": assignment_id, "status": status,
            "usage": usage, "usage_complete": bool(payload.get("usage_complete")),
            "next_effect": payload.get("next_effect")})
        if status == "completed":
            target = TicketState.RESULT_READY.value
        elif status in {"needs_input", "waiting_event"}:
            target = TicketState.WAITING_INPUT.value
        else:
            target = TicketState.BLOCKED.value
        if state not in {TicketState.CANCELLED.value, TicketState.CLOSED.value}:
            self._transition(cursor, ctx.tenant_id, ticket_id, target,
                             reason=f"worker_{status}")
        return final_state

    def _record_rejection(self, tenant_id: str, *, ticket_id: str, assignment_id: str,
                          reason: str, payload: dict, mark_assignment: bool) -> None:
        with self.store.transaction() as cursor:
            if mark_assignment:
                cursor.execute(
                    "UPDATE control_assignments SET state='rejected', rejected_reason=%s, "
                    "result_json=%s, ceased_at=now(), updated_at=now() WHERE id=%s "
                    "AND tenant_id=%s AND state IN ('leased','running')",
                    (reason, Jsonb(payload), assignment_id, tenant_id))
            self._append_event(cursor, tenant_id, ticket_id, "worker_result_rejected", {
                "assignment_id": assignment_id, "reason": reason})

    # ------------------------------------------------------------ attentions
    def _open_attention(self, cursor, tenant_id: str, ticket_id: str, need_type: str,
                        question: str, *, blocking: bool = True,
                        detail: dict | None = None,
                        assignee_principal_id: str | None = None) -> str | None:
        if need_type not in {need.value for need in AttentionNeed}:
            raise ValueError("invalid_attention_type")
        # Lock the ticket so opening an attention cannot interleave with a cancel:
        # cancel takes the same row lock first, so either the attention is created
        # before the stop (and is dismissed by it) or it is refused after it.
        ticket = cursor.execute(
            "SELECT state FROM control_tickets WHERE tenant_id=%s AND id=%s "
            "FOR UPDATE", (tenant_id, ticket_id)).fetchone()
        if ticket is None:
            raise ControlNotFound("ticket_not_found")
        if normalize_ticket_state(ticket["state"]) in {
                TicketState.CLOSED.value, TicketState.CANCELLED.value}:
            return None
        existing = cursor.execute(
            "SELECT id FROM control_attentions WHERE tenant_id=%s AND ticket_id=%s "
            "AND state='open' AND need_type=%s ORDER BY created_at LIMIT 1",
            (tenant_id, ticket_id, need_type)).fetchone()
        if existing is not None:
            if blocking:
                self._park_ticket(cursor, tenant_id, ticket_id)
            return str(existing["id"])
        attention_id = _uuid()
        cursor.execute(
            "INSERT INTO control_attentions(id, tenant_id, ticket_id, need_type, state, "
            "question, blocking, detail, assignee_principal_id) "
            "VALUES (%s, %s, %s, %s, 'open', %s, %s, %s, %s)",
            (attention_id, tenant_id, ticket_id, need_type, str(question)[:500],
             bool(blocking), Jsonb(detail or {}), assignee_principal_id))
        self._append_event(cursor, tenant_id, ticket_id, "attention_opened", {
            "attention_id": attention_id, "need_type": need_type,
            "blocking": bool(blocking)})
        if blocking:
            self._park_ticket(cursor, tenant_id, ticket_id)
            current = normalize_ticket_state(ticket["state"])
            target = (TicketState.AWAITING_APPROVAL.value
                      if need_type == AttentionNeed.APPROVAL.value
                      else TicketState.WAITING_INPUT.value)
            if current not in {TicketState.BLOCKED.value, TicketState.CANCELLED.value,
                               TicketState.CLOSED.value} and current != target:
                self._transition(cursor, tenant_id, ticket_id, target,
                                 reason=f"attention:{need_type}")
        return attention_id

    def _park_ticket(self, cursor, tenant_id: str, ticket_id: str) -> None:
        """Hold pending effects, but keep them pending so an answer can resume."""
        cursor.execute(
            "UPDATE control_effects SET available_at=now() + interval '100 years', "
            "updated_at=now() WHERE tenant_id=%s AND ticket_id=%s AND state='pending'",
            (tenant_id, ticket_id))

    def open_attention(self, ctx: TenantContext, *, ticket_id: str, need_type: str,
                       question: str, blocking: bool = True,
                       detail: dict | None = None) -> str | None:
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            return self._open_attention(cursor, ctx.tenant_id, ticket_id, need_type,
                                        question, blocking=blocking, detail=detail)

    def answer_attention(self, ctx: TenantContext, *, attention_id: str,
                         answer: dict | None = None) -> dict:
        ctx.require(Role.ADMIN, Role.PLANNER, Role.APPROVER)
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            row = cursor.execute(
                "UPDATE control_attentions SET state='answered', answer=%s, updated_at=now() "
                "WHERE tenant_id=%s AND id=%s AND state='open' "
                "RETURNING ticket_id, need_type, blocking, question",
                (Jsonb(answer or {}), ctx.tenant_id, attention_id)).fetchone()
            if row is None:
                raise ControlNotFound("attention_missing")
            ticket_id = str(row["ticket_id"])
            need_type = str(row["need_type"])
            # The answer becomes durable input: a resumed worker must be able to
            # read it, not just learn that someone clicked.
            input_id = self._insert_input(
                cursor, ctx.tenant_id, ticket_id,
                body={"attention_id": attention_id, "need_type": need_type,
                      "answer": answer or {}},
                kind="attention_answer", dedupe_key=f"attention:{attention_id}",
                trust_level="operator")
            self._append_event(cursor, ctx.tenant_id, ticket_id, "attention_answered", {
                "attention_id": attention_id, "need_type": need_type,
                "input_id": input_id, "answer": answer or {}})
            remaining = cursor.execute(
                "SELECT count(*) AS count FROM control_attentions "
                "WHERE tenant_id=%s AND ticket_id=%s AND state='open' AND blocking",
                (ctx.tenant_id, ticket_id)).fetchone()["count"]
            if int(remaining) == 0:
                self._wake_ticket(cursor, ctx.tenant_id, ticket_id)
                current = cursor.execute(
                    "SELECT state FROM control_tickets WHERE id=%s AND tenant_id=%s",
                    (ticket_id, ctx.tenant_id)).fetchone()
                state = normalize_ticket_state(current["state"])
                if state == TicketState.AWAITING_APPROVAL.value:
                    # An approved plan becomes eligible for execution, but only
                    # the execution command may claim it.
                    self._transition(cursor, ctx.tenant_id, ticket_id,
                                     TicketState.QUEUED.value, reason="approval_answered")
                elif state in {TicketState.WAITING_INPUT.value, TicketState.BLOCKED.value}:
                    self._transition(cursor, ctx.tenant_id, ticket_id,
                                     TicketState.QUEUED.value, reason="attention_answered")
                if need_type != AttentionNeed.APPROVAL.value:
                    # Answering a clarification means "continue": without a new
                    # effect the task would sit queued forever.  An approval is
                    # different -- execution is a separate, explicit command.
                    self._resume_after_answer(cursor, ctx.tenant_id, ticket_id,
                                              input_id=input_id, need_type=need_type)
            return {"attention_id": attention_id, "ticket_id": ticket_id,
                    "need_type": str(row["need_type"])}

    def _resume_after_answer(self, cursor, tenant_id: str, ticket_id: str, *,
                             input_id: str | None, need_type: str) -> str | None:
        """Queue one continuation effect if nothing is already about to run."""
        pending = cursor.execute(
            "SELECT count(*) AS count FROM control_effects "
            "WHERE tenant_id=%s AND ticket_id=%s AND state IN ('pending','running')",
            (tenant_id, ticket_id)).fetchone()["count"]
        if int(pending):
            return None
        ticket = cursor.execute(
            "SELECT mandate_id FROM control_tickets WHERE id=%s AND tenant_id=%s",
            (ticket_id, tenant_id)).fetchone()
        if ticket is None or ticket["mandate_id"] is None:
            return None
        last = cursor.execute(
            "SELECT capability FROM control_assignments WHERE tenant_id=%s "
            "AND ticket_id=%s AND capability IS NOT NULL ORDER BY created_at DESC "
            "LIMIT 1", (tenant_id, ticket_id)).fetchone()
        capability = str(last["capability"]) if last is not None else "assess"
        effect_id = self.promise_effect(
            cursor, tenant_id=tenant_id, ticket_id=ticket_id,
            kind=EffectKind.LAUNCH_WORKER.value,
            payload={"capability": capability,
                     "input": {"resumed_from_attention": need_type,
                               "input_id": input_id}},
            mandate_id=str(ticket["mandate_id"]))
        self._append_event(cursor, tenant_id, ticket_id, "work_resumed", {
            "effect_id": effect_id, "capability": capability,
            "input_id": input_id, "need_type": need_type})
        return effect_id

    def list_attentions(self, ctx: TenantContext, *, state: str = "open",
                        limit: int = 100) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT a.id, a.ticket_id, a.need_type, a.question, a.blocking, a.state, "
            "a.detail, a.created_at, t.goal, t.state AS ticket_state "
            "FROM control_attentions a JOIN control_tickets t "
            "ON t.id=a.ticket_id AND t.tenant_id=a.tenant_id "
            "WHERE a.tenant_id=%s AND a.state=%s ORDER BY a.created_at LIMIT %s",
            (ctx.tenant_id, state, max(1, min(int(limit), 200)))).fetchall()
        return [dict(row) for row in rows]

    def open_task(self, ctx: TenantContext, *, object_ref: str, goal: str,
                  state: str = TicketState.QUEUED.value,
                  attention: dict | None = None,
                  mandate_id: str | None = None,
                  policy_version: int | None = None) -> dict:
        """Open a ticket for an object without an incoming trigger.

        Used when a plan already exists (the plan, not an event, is the work
        item) and by operators starting work from the workbench.
        """
        ctx.require(Role.ADMIN, Role.PLANNER, Role.APPROVER)
        ctx.assert_tenant(ctx.tenant_id)
        resolved_state = normalize_ticket_state(state)
        if resolved_state not in {item.value for item in TicketState}:
            raise ValueError("invalid_ticket_state")
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            thread_id = self._resolve_thread(cursor, ctx.tenant_id, object_ref)
            resolved_mandate = self._resolve_mandate(
                cursor, ctx, mandate_id=mandate_id, object_ref=object_ref,
                capability=None, goal=goal)
            ticket_id = _uuid()
            cursor.execute(
                "INSERT INTO control_tickets(id, tenant_id, thread_id, goal, goal_kind, state, "
                "policy_version, mandate_id, created_by, progress_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                (ticket_id, ctx.tenant_id, thread_id, goal, _goal_kind(goal), resolved_state,
                 policy_version or ctx.policy_version, resolved_mandate, ctx.principal_id))
            cursor.execute(
                "INSERT INTO control_subscriptions(id, tenant_id, object_ref, ticket_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, object_ref, ticket_id) "
                "DO NOTHING", (_uuid(), ctx.tenant_id, object_ref, ticket_id))
            self._append_event(cursor, ctx.tenant_id, ticket_id, "ticket_opened", {
                "object_ref": object_ref, "state": resolved_state,
                "mandate_id": resolved_mandate})
            attention_id = None
            if attention:
                attention_id = self._open_attention(
                    cursor, ctx.tenant_id, ticket_id,
                    str(attention.get("need_type", AttentionNeed.APPROVAL.value)),
                    str(attention.get("question", "Review this task.")),
                    blocking=bool(attention.get("blocking", True)),
                    detail=attention.get("detail"))
        return {"ticket_id": ticket_id, "thread_id": thread_id,
                "mandate_id": resolved_mandate, "attention_id": attention_id}

    def service_context_for_ticket(self, *, tenant_id: str, ticket_id: str,
                                   service_id: str) -> TenantContext:
        """Build a background-service context strictly from the ticket's mandate.

        This replaces the previous fixed ``worker-dispatcher`` administrator
        context.  A revoked, expired or policy-stale mandate yields no context at
        all, and the caller must stop rather than fall back to something wider.
        """
        from .identity import service_context

        row = self.store.connection.execute(
            "SELECT t.mandate_id, m.* FROM control_tickets t "
            "LEFT JOIN control_mandates m ON m.id = t.mandate_id "
            "WHERE t.id=%s AND t.tenant_id=%s", (ticket_id, tenant_id)).fetchone()
        self.store.connection.commit()
        if row is None:
            raise ControlNotFound("ticket_not_found")
        if row["mandate_id"] is None or row["id"] is None:
            raise MandateDenied("mandate_missing")
        mandate = self._mandate_from_row(row)
        if mandate.state != MandateState.ACTIVE.value:
            raise MandateDenied("mandate_not_active")
        if mandate.deadline_at is not None and mandate.deadline_at < _now():
            raise MandateDenied("mandate_expired")
        with self.store.transaction() as cursor:
            current_policy = self._policy_version(cursor, tenant_id)
        if mandate.policy_version != current_policy:
            raise MandateDenied("mandate_policy_stale")
        # The principal's real membership decides which commands the service may
        # issue on its behalf.
        with self.store.transaction() as cursor:
            membership = cursor.execute(
                "SELECT role FROM control_principals WHERE tenant_id=%s AND id=%s",
                (tenant_id, mandate.principal_id)).fetchone()
        roles = frozenset({Role(str(membership["role"]))}) if membership else None
        return service_context(mandate, service_id=service_id, roles=roles)

    # ------------------------------------------------------------- lifecycle
    def cancel_ticket(self, ctx: TenantContext, *, ticket_id: str,
                      reason: str = "cancelled_by_operator") -> int:
        """Stop a task now.

        In-flight business writes are moved to reconciliation rather than being
        reported as rolled back (plan section 13.3).
        """
        ctx.require(Role.ADMIN, Role.PLANNER, Role.APPROVER)
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            ticket = cursor.execute(
                "SELECT state FROM control_tickets WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (ticket_id, ctx.tenant_id)).fetchone()
            if ticket is None:
                raise ControlNotFound("ticket_not_found")
            if normalize_ticket_state(ticket["state"]) == TicketState.CLOSED.value:
                raise StateTransitionRejected("ticket_closed")
            reconciled = cursor.execute(
                "UPDATE control_effects SET state='reconcile_required', "
                "reconcile_state='unknown', updated_at=now() "
                "WHERE tenant_id=%s AND ticket_id=%s AND state='running' "
                "AND side_effect_class='business_write' RETURNING id",
                (ctx.tenant_id, ticket_id)).fetchall()
            effects = cursor.execute(
                "UPDATE control_effects SET state='cancelled', updated_at=now() "
                "WHERE tenant_id=%s AND ticket_id=%s AND state IN ('pending','running') "
                "RETURNING id", (ctx.tenant_id, ticket_id)).fetchall()
            cursor.execute(
                "UPDATE control_attentions SET state='dismissed', updated_at=now() "
                "WHERE tenant_id=%s AND ticket_id=%s AND state='open'",
                (ctx.tenant_id, ticket_id))
            cursor.execute(
                "UPDATE control_assignments SET state='cancelled', ceased_at=now(), "
                "updated_at=now() WHERE tenant_id=%s AND ticket_id=%s "
                "AND state IN ('leased','running')", (ctx.tenant_id, ticket_id))
            cursor.execute(
                "DELETE FROM control_task_dependencies WHERE tenant_id=%s AND ticket_id=%s "
                "AND state='pending'", (ctx.tenant_id, ticket_id))
            self._deactivate_subscriptions(cursor, ctx.tenant_id, ticket_id)
            self._append_event(cursor, ctx.tenant_id, ticket_id, "task_cancelled", {
                "reason": reason,
                "effects": [str(row["id"]) for row in effects],
                "reconciling_effects": [str(row["id"]) for row in reconciled]})
            if reconciled:
                # A stop button must not imply a rollback.  The business write was
                # already on its way; the only honest state is "needs reconciliation".
                self._transition(cursor, ctx.tenant_id, ticket_id, TicketState.BLOCKED.value,
                                 reason="execution_result_unknown")
                self._open_attention(cursor, ctx.tenant_id, ticket_id,
                                     AttentionNeed.RESULT_UNKNOWN.value,
                                     "The task was stopped during an ERP write. The "
                                     "operation result is unknown; reconcile it against "
                                     "the original receipt before doing anything else.",
                                     detail={"reason": reason})
            else:
                self._transition(cursor, ctx.tenant_id, ticket_id, TicketState.CANCELLED.value,
                                 reason=reason)
            return len(effects)

    def mark_execution(self, ctx: TenantContext, *, ticket_id: str, stage: str,
                       reason: str | None = None) -> dict:
        """Move a task through executing / verifying / completed.

        Kept separate from the business executor on purpose: the ERP write is a
        different authority from the task lifecycle, and neither may fake the
        other's state.
        """
        allowed = {"executing": TicketState.EXECUTING.value,
                   "verifying": TicketState.VERIFYING.value,
                   "completed": TicketState.COMPLETED.value,
                   "failed": TicketState.FAILED.value,
                   "blocked": TicketState.BLOCKED.value}
        if stage not in allowed:
            raise ValueError("invalid_execution_stage")
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            self._transition(cursor, ctx.tenant_id, ticket_id, allowed[stage],
                             reason=reason or f"execution_{stage}")
        return self.get_ticket(ctx, ticket_id)

    def retry_ticket(self, ctx: TenantContext, *, ticket_id: str) -> int:
        """Operator-initiated retry of a failed task.  Never replays a business write."""
        ctx.require(Role.ADMIN, Role.PLANNER)
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            attention = cursor.execute(
                "SELECT id FROM control_attentions WHERE tenant_id=%s AND ticket_id=%s "
                "AND need_type IN ('failure','budget','rule','system') AND state='open' "
                "ORDER BY created_at LIMIT 1 FOR UPDATE",
                (ctx.tenant_id, ticket_id)).fetchone()
            if attention is None:
                raise ControlNotFound("failure_attention_missing")
            cursor.execute(
                "UPDATE control_attentions SET state='answered', answer=%s, updated_at=now() "
                "WHERE tenant_id=%s AND id=%s",
                (Jsonb({"action": "retry"}), ctx.tenant_id, attention["id"]))
            retried = cursor.execute(
                "UPDATE control_effects SET state='pending', lease_owner=NULL, "
                "lease_expires_at=NULL, fencing_token=NULL, attempt=0, "
                "available_at=now(), updated_at=now() "
                "WHERE tenant_id=%s AND ticket_id=%s AND state='failed' "
                "AND side_effect_class <> 'business_write' RETURNING id",
                (ctx.tenant_id, ticket_id)).fetchall()
            self._append_event(cursor, ctx.tenant_id, ticket_id, "operator_retry", {
                "effects": [str(row["id"]) for row in retried]})
            self._transition(cursor, ctx.tenant_id, ticket_id, TicketState.QUEUED.value,
                             reason="operator_retry")
            self._wake_ticket(cursor, ctx.tenant_id, ticket_id)
            return len(retried)

    # ------------------------------------------------------------ dependencies
    def open_subtask(self, ctx: TenantContext, *, parent_ticket_id: str, goal: str,
                     object_ref: str, capability: str | None = None,
                     depends_on: Iterable[str] = (), max_children: int = 8,
                     max_depth: int = 3) -> str:
        """Create one dependent subtask, bounded by server-side limits.

        Workers may *propose* a decomposition; the server decides how many and
        how deep.
        """
        ctx.require(Role.ADMIN, Role.PLANNER)
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            parent = cursor.execute(
                "SELECT thread_id, goal_kind, mandate_id, state FROM control_tickets "
                "WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (parent_ticket_id, ctx.tenant_id)).fetchone()
            if parent is None:
                raise ControlNotFound("parent_ticket_missing")
            children = cursor.execute(
                "SELECT count(*) AS count FROM control_task_dependencies "
                "WHERE tenant_id=%s AND depends_on_ticket_id=%s",
                (ctx.tenant_id, parent_ticket_id)).fetchone()["count"]
            if int(children) >= int(max_children):
                raise BudgetExhausted("subtask_breadth_exceeded")
            depth = cursor.execute(
                "WITH RECURSIVE chain AS ("
                "  SELECT ticket_id, depends_on_ticket_id, 1 AS depth "
                "  FROM control_task_dependencies WHERE tenant_id=%s AND ticket_id=%s "
                "  UNION ALL "
                "  SELECT d.ticket_id, d.depends_on_ticket_id, chain.depth + 1 "
                "  FROM control_task_dependencies d JOIN chain ON d.ticket_id = chain.depends_on_ticket_id "
                "  WHERE d.tenant_id=%s AND chain.depth < 32) "
                "SELECT COALESCE(MAX(depth), 0) AS depth FROM chain",
                (ctx.tenant_id, parent_ticket_id, ctx.tenant_id)).fetchone()["depth"]
            if int(depth) >= int(max_depth):
                raise BudgetExhausted("subtask_depth_exceeded")
            ticket_id = _uuid()
            cursor.execute(
                "INSERT INTO control_tickets(id, tenant_id, thread_id, goal, goal_kind, state, "
                "policy_version, mandate_id, created_by) "
                "VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s)",
                (ticket_id, ctx.tenant_id, parent["thread_id"], goal, _goal_kind(goal),
                 ctx.policy_version, parent["mandate_id"], ctx.principal_id))
            cursor.execute(
                "INSERT INTO control_task_dependencies(id, tenant_id, ticket_id, "
                "depends_on_ticket_id, reason) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (_uuid(), ctx.tenant_id, ticket_id, parent_ticket_id, "parent"))
            for dependency in depends_on:
                cursor.execute(
                    "INSERT INTO control_task_dependencies(id, tenant_id, ticket_id, "
                    "depends_on_ticket_id, reason) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (_uuid(), ctx.tenant_id, ticket_id, str(dependency), "sibling"))
            self._append_event(cursor, ctx.tenant_id, parent_ticket_id, "subtask_opened", {
                "subtask_id": ticket_id, "goal": goal, "object_ref": object_ref})
            if capability:
                self.promise_effect(
                    cursor, tenant_id=ctx.tenant_id, ticket_id=ticket_id,
                    kind=EffectKind.LAUNCH_WORKER.value,
                    payload={"capability": capability, "goal": goal,
                             "object_ref": object_ref,
                             "mandate_id": str(parent["mandate_id"])
                             if parent["mandate_id"] else None},
                    mandate_id=(str(parent["mandate_id"]) if parent["mandate_id"] else None))
            return ticket_id

    def satisfy_dependencies(self, *, tenant_id: str, dependency_ticket_id: str,
                             state: str = "satisfied") -> int:
        if state not in {"satisfied", "failed", "cancelled"}:
            raise ValueError("invalid_dependency_state")
        with self.store.transaction() as cursor:
            rows = cursor.execute(
                "UPDATE control_task_dependencies SET state=%s, satisfied_at=now() "
                "WHERE tenant_id=%s AND depends_on_ticket_id=%s AND state='pending' RETURNING id",
                (state, tenant_id, dependency_ticket_id)).fetchall()
        return len(rows)

    def pending_dependencies(self, tenant_id: str, ticket_id: str) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT id, depends_on_ticket_id, state FROM control_task_dependencies "
            "WHERE tenant_id=%s AND ticket_id=%s", (tenant_id, ticket_id)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ reads
    def get_ticket(self, ctx: TenantContext, ticket_id: str) -> dict[str, Any]:
        ctx.assert_tenant(ctx.tenant_id)
        row = self.store.connection.execute(
            "SELECT * FROM control_tickets WHERE id=%s AND tenant_id=%s",
            (ticket_id, ctx.tenant_id)).fetchone()
        if row is None:
            raise ControlNotFound("ticket_not_found")
        ticket = dict(row)
        for key in ("id", "thread_id", "case_ref", "mandate_id"):
            if ticket.get(key) is not None:
                ticket[key] = str(ticket[key])
        ticket["state"] = normalize_ticket_state(ticket["state"])
        ticket["user_state"] = user_state_for(ticket["state"])
        ticket["phase"] = phase_for(ticket["state"])
        return ticket

    def append_event(self, ctx: TenantContext, *, ticket_id: str, kind: str,
                     payload: dict | None = None) -> int:
        """Append a ticket event through the single writer."""
        ctx.assert_tenant(ctx.tenant_id)
        with self.store.transaction() as cursor:
            self._authorize(cursor, ctx)
            return self._append_event(cursor, ctx.tenant_id, ticket_id, str(kind)[:120],
                                      payload or {})

    def ticket_events(self, ctx: TenantContext, ticket_id: str) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            "SELECT sequence, kind, payload, created_at FROM control_events "
            "WHERE tenant_id=%s AND ticket_id=%s ORDER BY sequence",
            (ctx.tenant_id, ticket_id)).fetchall()
        return [dict(row) for row in rows]

    def tickets_for_object(self, ctx: TenantContext, object_ref: str) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT t.id, t.goal, t.state, t.goal_kind, t.created_at, s.active "
            "FROM control_tickets t JOIN control_subscriptions s "
            "ON s.ticket_id=t.id AND s.tenant_id=t.tenant_id "
            "WHERE t.tenant_id=%s AND s.object_ref=%s ORDER BY t.created_at DESC LIMIT 50",
            (ctx.tenant_id, object_ref)).fetchall()
        return [dict(row) for row in rows]


def initial_payload_get(payload: dict | None) -> str | None:
    """Read the requested worker capability from a trigger's initial payload."""
    if not isinstance(payload, dict):
        return None
    value = payload.get("capability")
    if not value:
        inner = payload.get("input")
        value = inner.get("capability") if isinstance(inner, dict) else None
    return str(value) if value else None

