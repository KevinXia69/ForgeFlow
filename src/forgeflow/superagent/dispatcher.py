"""Persistent effect dispatcher bridging Control Engine leases to Router workers.

The dispatcher no longer fabricates an administrator identity.  For each claimed
effect it resolves the ticket's persisted mandate and runs the work as a service
acting *for* that principal.  It also no longer turns every successful worker
result into a human approval prompt: a completed investigation becomes a result,
and only a genuine decision, missing fact or unknown outcome asks for a person.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from .contracts import (
    AttentionNeed,
    EffectKind,
    EffectState,
    TicketState,
)
from .engine import (
    ControlEngine,
    FencingError,
    LateResultRejected,
    MandateDenied,
    StateTransitionRejected,
)
from .heartbeat import beat
from .identity import TenantContext
from .lease import LeaseKeeper
from .store import ControlStore
from .workers import Router, WorkerCancelled, WorkerRequest, WorkerResult

#: Needs that may interrupt a human.  Everything else is stored, not escalated.
ESCALATING_NEEDS = {need.value for need in AttentionNeed}


@dataclass(slots=True)
class EffectContext:
    """Everything an effect handler is allowed to know."""

    effect: dict[str, Any]
    ticket: dict[str, Any]
    payload: dict[str, Any]
    ctx: TenantContext
    tenant_id: str
    ticket_id: str
    fencing_token: str
    cancel_check: Callable[[], bool]
    engine: ControlEngine = field(repr=False, default=None)  # type: ignore[assignment]


class EffectHandler(Protocol):
    def __call__(self, context: EffectContext) -> dict[str, Any]:
        ...


#: A notification interrupts a person only for these needs; ordinary progress
#: stays in the ticket timeline (v3 plan section 13.1).
NOTIFY_NEEDS = frozenset({
    AttentionNeed.FACT.value,
    AttentionNeed.RULE.value,
    AttentionNeed.APPROVAL.value,
    AttentionNeed.FAILURE.value,
    AttentionNeed.RESULT_UNKNOWN.value,
})


class Dispatcher:
    def __init__(self, store: ControlStore, router: Router, *,
                 handlers: dict[str, EffectHandler] | None = None,
                 notifier: Callable[[dict], None] | None = None,
                 resolver: Any | None = None,
                 quotas: Any | None = None) -> None:
        self.store = store
        self.engine = ControlEngine(store)
        self.router = router
        self.notifier = notifier
        # The resolver is the only way a free-text reference becomes a record.
        # It is injected rather than looked up here: a dispatcher without a
        # business connection must report "unavailable", never guess an id.
        self.resolver = resolver
        # Optional quota guard: absent keeps existing deployments unlimited.
        self.quotas = quotas
        if self.router.store is None:
            self.router.store = store
        self.handlers: dict[str, EffectHandler] = {
            EffectKind.LAUNCH_WORKER.value: self._handle_launch_worker,
            EffectKind.ROUTE_DECISION.value: self._handle_route_decision,
            EffectKind.SEND_NOTIFICATION.value: self._handle_notification,
        }
        if handlers:
            self.handlers.update(handlers)

    # -- public API ----------------------------------------------------------
    def register_handler(self, kind: str, handler: EffectHandler) -> None:
        self.handlers[str(kind)] = handler

    def tick(self, worker_id: str, *, tenant_id: str | None = None,
             lease_seconds: int = 60) -> dict[str, Any] | None:
        effect = self.engine.claim_effect(worker_id, lease_seconds=lease_seconds,
                                          tenant_id=tenant_id)
        if effect is None:
            return None
        kind = str(effect["kind"])
        handler = self.handlers.get(kind)
        if handler is None:
            self.engine.complete_effect(
                effect_id=effect["id"], tenant_id=effect["tenant_id"],
                fencing_token=effect["fencing_token"], state=EffectState.FAILED.value,
                detail={"error": "unsupported_effect", "kind": kind})
            return {"effect_id": effect["id"], "status": "failed",
                    "error": "unsupported_effect", "kind": kind}
        return self._run_effect(effect, handler, worker_id=worker_id,
                                lease_seconds=lease_seconds)

    # -- effect execution ----------------------------------------------------
    def _run_effect(self, effect: dict, handler: EffectHandler, *, worker_id: str,
                    lease_seconds: int) -> dict[str, Any]:
        tenant_id = str(effect["tenant_id"])
        ticket_id = str(effect["ticket_id"])
        payload = dict(effect.get("payload") or {})
        try:
            ctx = self.engine.service_context_for_ticket(
                tenant_id=tenant_id, ticket_id=ticket_id,
                service_id=f"dispatcher:{worker_id}")
        except (MandateDenied, LookupError) as error:
            # The claim-time precondition should have caught this; if it did not,
            # fail closed rather than inventing authority.
            code = getattr(error, "code", "mandate_unavailable")
            self._fail(effect, code)
            return {"effect_id": effect["id"], "status": "blocked", "error": code}

        keeper = LeaseKeeper(
            self.store, effect_id=effect["id"], tenant_id=tenant_id,
            fencing_token=str(effect["fencing_token"]),
            interval_seconds=max(1.0, lease_seconds / 3),
            lease_seconds=float(lease_seconds))
        keeper.start()
        context = EffectContext(
            effect=effect, ticket=self._ticket(tenant_id, ticket_id), payload=payload,
            ctx=ctx, tenant_id=tenant_id, ticket_id=ticket_id,
            fencing_token=str(effect["fencing_token"]),
            cancel_check=lambda: keeper.lost.is_set() or self._cancelled(
                tenant_id, ticket_id, str(effect["id"])),
            engine=self.engine)
        try:
            outcome = handler(context)
        except WorkerCancelled:
            keeper.stop()
            return {"effect_id": effect["id"], "status": "cancelled",
                    "error": "worker_cancelled"}
        except (LateResultRejected, StateTransitionRejected) as error:
            keeper.stop()
            return {"effect_id": effect["id"], "status": "discarded",
                    "error": getattr(error, "code", str(error))}
        except Exception as error:  # noqa: BLE001 - classified below
            keeper.stop()
            detail = {"error": type(error).__name__ + ":" + str(error)[:180]}
            self._fail(effect, detail["error"])
            return {"effect_id": effect["id"], "status": "failed", **detail}
        finally:
            keeper.stop()
        if keeper.lost.is_set():
            return {"effect_id": effect["id"], "status": "cancelled",
                    "error": "lease_lost"}
        state = EffectState.COMPLETED.value
        if str(outcome.get("status", "completed")) in {"failed", "blocked"}:
            state = EffectState.FAILED.value
        try:
            self.engine.complete_effect(
                effect_id=effect["id"], tenant_id=tenant_id,
                fencing_token=str(effect["fencing_token"]), state=state,
                detail=outcome)
        except FencingError:
            return {"effect_id": effect["id"], "status": "discarded",
                    "error": "fencing_token_rejected"}
        return {"effect_id": effect["id"], **outcome}

    def _ticket(self, tenant_id: str, ticket_id: str) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT * FROM control_tickets WHERE id=%s AND tenant_id=%s",
            (ticket_id, tenant_id)).fetchone()
        self.store.connection.commit()
        return dict(row) if row else {}

    def _cancelled(self, tenant_id: str, ticket_id: str, effect_id: str) -> bool:
        row = self.store.connection.execute(
            "SELECT state FROM control_effects WHERE id=%s AND tenant_id=%s",
            (effect_id, tenant_id)).fetchone()
        self.store.connection.commit()
        if row is None or str(row["state"]) != EffectState.RUNNING.value:
            return True
        ticket = self.store.connection.execute(
            "SELECT state FROM control_tickets WHERE id=%s AND tenant_id=%s",
            (ticket_id, tenant_id)).fetchone()
        self.store.connection.commit()
        return ticket is None or str(ticket["state"]) in {
            TicketState.CANCELLED.value, TicketState.CLOSED.value}

    def _fail(self, effect: dict, code: str) -> None:
        try:
            self.engine.complete_effect(
                effect_id=effect["id"], tenant_id=effect["tenant_id"],
                fencing_token=effect["fencing_token"],
                state=EffectState.FAILED.value, detail={"error": code})
        except FencingError:
            pass

    # -- built-in handlers ---------------------------------------------------
    def _consume_inputs(self, context: EffectContext, assignment_id: str) -> tuple[dict, ...]:
        rows = self.engine.consume_inputs(context.ctx, ticket_id=context.ticket_id,
                                          assignment_id=assignment_id)
        return tuple(rows)

    def _handle_launch_worker(self, context: EffectContext) -> dict[str, Any]:
        payload = context.payload
        capability = str(payload.get("capability") or payload.get("capability_hint")
                         or "assess")
        request = WorkerRequest(
            tenant_id=context.tenant_id, ticket_id=context.ticket_id,
            goal=str(payload.get("goal") or context.ticket.get("goal") or "investigate"),
            input=dict(payload.get("input") or {}),
            capability=capability,
            budget_seconds=int(payload.get("budget_seconds", 120)),
            budget_tokens=int(payload.get("budget_tokens", 16000)),
            mandate_id=(str(context.ticket["mandate_id"])
                        if context.ticket.get("mandate_id") else None),
            run_token=context.fencing_token,
            object_ref=payload.get("object_ref"),
            principal_id=context.ctx.principal_id,
            tools=tuple(payload.get("tools") or ()))
        # Reserve quota *before* consuming inputs: a blocked launch must not
        # swallow facts that could never be delivered again.
        blocked, quota_window = self._reserve_quota(context, request)
        if blocked is not None:
            return blocked
        assignment_id = self.engine.start_assignment(
            context.ctx, ticket_id=context.ticket_id, capability=capability,
            runner=self._runner_name(capability), fencing_token=context.fencing_token,
            mandate_id=request.mandate_id)
        request = replace(request, assignment_id=assignment_id,
                          inputs=self._consume_inputs(context, assignment_id))
        started = time.monotonic()
        try:
            result = self.router.dispatch(context.ctx, request, record=False)
        except WorkerCancelled:
            with self.store.transaction() as cursor:
                cursor.execute(
                    "UPDATE control_assignments SET state='cancelled', ceased_at=now(), "
                    "updated_at=now() WHERE id=%s", (assignment_id,))
            self._settle_failed(context, request, time.monotonic() - started, quota_window)
            raise
        except Exception:
            # No measured token usage exists; keep the token reservation (unknown
            # spend) but release the unused wall-clock over-reservation.
            self._settle_failed(context, request, time.monotonic() - started, quota_window)
            raise
        return self._apply_worker_result(context, request, result, assignment_id,
                                         elapsed=time.monotonic() - started,
                                         quota_window=quota_window)

    def _quota_block(self, context, request) -> dict[str, Any] | None:
        blocked, _window = self._reserve_quota(context, request)
        return blocked

    def _reserve_quota(self, context, request) -> tuple[dict[str, Any] | None, str | None]:
        """Atomically reserve before starting; return the reservation window."""
        if self.quotas is None:
            return None, None
        reservation = self.quotas.reserve_many(context.tenant_id, [
            ("wall_seconds", request.budget_seconds),
            ("model_tokens", request.budget_tokens)])
        checks = reservation.get("items") or []
        denied = [item for item in checks if not item.get("allowed")]
        if not denied:
            return None, reservation.get("window_start")
        try:
            self.engine.open_attention(
                context.ctx, ticket_id=context.ticket_id,
                need_type=AttentionNeed.BUDGET.value,
                question=("The model or wall-clock quota for this tenant is exhausted. "
                          "Review the quota or wait for the next UTC day."),
                detail={"quota": denied})
        except StateTransitionRejected:
            pass
        return ({"status": "blocked", "reason": "quota_exhausted", "quota": denied},
                None)

    def _settle_failed(self, context, request, elapsed: float,
                       window_start: str | None) -> None:
        self._consume_usage(context, request, WorkerResult("failed", ""),
                            elapsed=elapsed, window_start=window_start)

    def _consume_usage(self, context, request, result, *, elapsed: float = 0.0,
                       window_start: str | None = None) -> None:
        if self.quotas is None:
            return
        usage = result.usage or {}
        if result.usage_complete:
            tokens = int(usage.get("tokens") or usage.get("total_tokens") or 0)
            self.quotas.settle(context.tenant_id, "model_tokens",
                               request.budget_tokens, tokens,
                               window_start=window_start)
        self.quotas.settle(context.tenant_id, "wall_seconds",
                           request.budget_seconds, int(max(0.0, elapsed) + 0.999),
                           window_start=window_start)

    def _runner_name(self, capability: str) -> str:
        worker = self.router.registry.get(capability)
        return worker.name if worker is not None else capability

    def _apply_worker_result(self, context: EffectContext, request: WorkerRequest,
                             result: WorkerResult, assignment_id: str, *,
                             elapsed: float = 0.0,
                             quota_window: str | None = None) -> dict[str, Any]:
        report = result.as_report(assignment_id=assignment_id,
                                  fencing_token=context.fencing_token,
                                  ticket_id=context.ticket_id)
        self.engine.record_worker_result(
            context.ctx, ticket_id=context.ticket_id, assignment_id=assignment_id,
            report=report, fencing_token=context.fencing_token)
        self._escalate(context, result, assignment_id)
        self._chain_next_effect(context, result)
        self._consume_usage(context, request, result, elapsed=elapsed,
                            window_start=quota_window)
        return {"status": result.status, "assignment_id": assignment_id,
                "worker": request.capability, "summary": result.summary,
                "usage": dict(result.usage), "usage_complete": result.usage_complete}

    def _escalate(self, context: EffectContext, result: WorkerResult,
                  assignment_id: str) -> None:
        """Open a human attention only for a genuine reason (plan section 13.1).

        A task stopped between the result being stored and this projection is a
        normal race, not a failure: the attention is skipped and the stop wins.
        """
        try:
            self._escalate_locked(context, result, assignment_id)
        except StateTransitionRejected:
            return

    def _escalate_locked(self, context: EffectContext, result: WorkerResult,
                         assignment_id: str) -> None:
        opened: list[str] = []
        for item in result.attention:
            need = str(item.get("need", AttentionNeed.FACT.value))
            if need == AttentionNeed.APPROVAL.value:
                # A worker may recommend a decision, but it cannot create an
                # approval: approval is bound to a prepared plan digest.  The
                # plan layer opens it after the plan actually exists.
                continue
            if need not in ESCALATING_NEEDS:
                continue
            attention_id = self.engine.open_attention(
                context.ctx, ticket_id=context.ticket_id, need_type=need,
                question=str(item.get("question", "The investigation needs a decision.")),
                detail={"assignment_id": assignment_id})
            if attention_id:
                opened.append(attention_id)
        if result.status == "failed" and not opened:
            attention_id = self.engine.open_attention(
                context.ctx, ticket_id=context.ticket_id,
                need_type=AttentionNeed.FAILURE.value,
                question=f"The investigation could not finish: {result.summary}"[:500],
                detail={"assignment_id": assignment_id})
            if attention_id:
                opened.append(attention_id)
        if result.status == "needs_input" and not opened:
            question = (result.unknowns[0] if result.unknowns
                        else "The investigation is missing a business fact.")
            attention_id = self.engine.open_attention(
                context.ctx, ticket_id=context.ticket_id,
                need_type=AttentionNeed.FACT.value,
                question=question if isinstance(question, str) else str(question),
                detail={"assignment_id": assignment_id,
                        "unknowns": list(result.unknowns)})
            if attention_id:
                opened.append(attention_id)
        if not opened and result.status == "completed":
            # A normal successful result is a result, not a request for approval.
            try:
                self.engine.record_progress(context.ctx, ticket_id=context.ticket_id,
                                            kind="result_ready")
            except StateTransitionRejected:
                return

    def _chain_next_effect(self, context: EffectContext, result: WorkerResult) -> None:
        next_kind = result.next_effect
        if (result.plan_request is not None and next_kind is None
                and result.status == "completed"):
            # Planning from an incomplete investigation would turn a known
            # missing fact into an executable purchase.  Only a completed
            # report may request a plan; a worker-explicit next effect is
            # still honoured.
            next_kind = EffectKind.PREPARE_PLAN.value
        if not next_kind:
            return
        if next_kind not in {item.value for item in EffectKind}:
            return
        current = self._ticket(context.tenant_id, context.ticket_id).get("state")
        if current in {TicketState.CANCELLED.value, TicketState.CLOSED.value}:
            return
        try:
            self.engine.request_effect(
                context.ctx, ticket_id=context.ticket_id, kind=next_kind,
                payload={"plan_request": result.plan_request or {},
                         "object_ref": context.payload.get("object_ref"),
                         "capability": context.payload.get("capability")},
                idempotency_key=f"{context.ticket_id}:{next_kind}:"
                                f"{result.usage.get('tokens', 0)}")
        except StateTransitionRejected:
            return

    def _handle_route_decision(self, context: EffectContext) -> dict[str, Any]:
        from .workers import RouterRequest

        directive = None
        message = str((context.payload.get("input") or {}).get("message")
                      or context.payload.get("message") or "")
        if not message:
            return {"status": "completed", "skipped": "no_message"}
        directive = self.router.decide(RouterRequest(
            tenant_id=context.tenant_id, principal_id=context.ctx.principal_id,
            message=message, object_ref=context.payload.get("object_ref"),
            ticket_id=context.ticket_id,
            active_tickets=tuple(self._active_tickets(context.tenant_id))))
        resolution = None
        if directive.object_reference:
            resolution = self._resolve_reference(context, directive.object_reference)
        self.engine.append_event(
            context.ctx, ticket_id=context.ticket_id, kind="router_decision",
            payload={"decision": directive.decision,
                     "rationale": directive.rationale,
                     "confidence": directive.confidence,
                     "object_reference": directive.object_reference,
                     "resolution": resolution})
        return {"status": "completed", "decision": directive.decision,
                "resolution": resolution}

    def _resolve_reference(self, context: EffectContext,
                           reference: str) -> dict[str, Any] | None:
        """Turn "PO-123" into a record id, or say why that is impossible."""
        if self.resolver is None:
            return {"status": "unavailable", "reference": reference,
                    "reason": "no_business_adapter"}
        try:
            resolution = self.resolver.resolve(reference)
        except Exception as error:  # noqa: BLE001 - a lookup failure is reported
            return {"status": "error", "reference": reference,
                    "error": type(error).__name__}
        if resolution.ambiguous:
            self.engine.open_attention(
                context.ctx, ticket_id=context.ticket_id, need_type="fact",
                question=(f"{reference} matches {len(resolution.candidates)} records; "
                          "which one do you mean?"),
                detail={"reference": reference,
                        "options": [item.as_dict() for item in resolution.candidates]})
            return {"status": "ambiguous", **resolution.as_dict()}
        if resolution.resolved is None:
            return {"status": "not_found", **resolution.as_dict()}
        return {"status": "resolved", **resolution.as_dict()}

    def _active_tickets(self, tenant_id: str) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT t.id, t.state, th.object_ref FROM control_tickets t "
            "JOIN control_threads th ON th.id=t.thread_id AND th.tenant_id=t.tenant_id "
            "WHERE t.tenant_id=%s AND t.state NOT IN ('cancelled','closed') "
            "ORDER BY t.updated_at DESC LIMIT 50", (tenant_id,)).fetchall()
        self.store.connection.commit()
        return [{"ticket_id": str(row["id"]), "state": str(row["state"]),
                 "object_ref": str(row["object_ref"])} for row in rows]

    def _handle_notification(self, context: EffectContext) -> dict[str, Any]:
        need = context.payload.get("need")
        if need not in NOTIFY_NEEDS:
            # Progress, retries and ordinary model output remain timeline events.
            return {"status": "suppressed", "reason": "notification_policy",
                    "need": need}
        message = {
            "ticket_id": context.ticket_id,
            "need": need,
            "body": context.payload.get("body"),
            "message_id": self._stable_message_id(context),
        }
        if self.notifier is not None:
            # Notification failure must not roll back an already-committed
            # business fact; the effect records the attempt.
            try:
                self.notifier(message)
            except Exception as error:  # noqa: BLE001
                return {"status": "failed", "notification_error": type(error).__name__,
                        "message_id": message["message_id"]}
        return {"status": "completed", "message_id": message["message_id"]}

    def _stable_message_id(self, context: EffectContext) -> str:
        raw = f"{context.tenant_id}:{context.ticket_id}:{context.payload.get('need')}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


class DispatcherLoop:
    """Bounded single-process dispatcher loop; a supervisor can run one per host."""

    def __init__(self, dispatcher: Dispatcher, worker_id: str, *,
                 tenant_id: str | None = None, interval_seconds: float = 1.0) -> None:
        if not worker_id or interval_seconds <= 0:
            raise ValueError("invalid_dispatcher_loop")
        self.dispatcher = dispatcher
        self.worker_id = worker_id
        self.tenant_id = tenant_id
        self.interval_seconds = float(interval_seconds)

    def run(self, stop_event) -> int:
        processed = 0
        while not stop_event.is_set():
            store = getattr(self.dispatcher, "store", None)
            if store is not None:
                beat(store, service_id=f"dispatcher:{self.worker_id}",
                     tenant_id=self.tenant_id, kind="dispatcher")
            result = self.dispatcher.tick(self.worker_id, tenant_id=self.tenant_id)
            if result is None:
                stop_event.wait(self.interval_seconds)
                continue
            processed += 1
        return processed
