"""Explicit approval and execution commands for an Odoo 20 recovery plan.

The Control Engine deliberately does not execute a plan when an approval
attention is answered: answering means "a person decided", not "a business write
may now happen".  These two commands keep that separation:

* :func:`approve_plan` records the Odoo decision bound to the plan digest, then
  answers the approval attention;
* :func:`request_execution` asks for the durable ``apply_approved_plan`` effect,
  which a dispatcher can execute in a later process.

Both are idempotent: repeating an approval returns the existing decision, and
requesting execution twice returns the same queued effect.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from ..integrations.odoo20.business import Odoo20BusinessService, require_complete_review_snapshot
from .bindings import odoo_user_id_for
from .contracts import AttentionNeed, EffectKind, TicketState, normalize_ticket_state
from .engine import ControlEngine, ControlNotFound, StateTransitionRejected
from .identity import Role, TenantContext
from .store import ControlStore
from .tasks import answer_attention, plan_task


def _linked(store: ControlStore, tenant_id: str, plan_id: int):
    return plan_task(store, tenant_id=tenant_id, plan_id=int(plan_id))


def _require_current_approval(prepared) -> None:
    detail = getattr(prepared, "detail", {}) or {}
    # The real ERP loader always supplies this field. Older in-process business
    # implementations have no expiry metadata and still use the native gate.
    if "approval_expires_at" not in detail:
        return
    raw = detail["approval_expires_at"]
    try:
        expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        expires = expires.replace(tzinfo=expires.tzinfo or UTC)
    except (ValueError, TypeError):
        raise ValueError("approval_expiry_unverified") from None
    if expires <= datetime.now(UTC):
        raise ValueError("approval_expired")


def approve_plan(store: ControlStore, ctx: TenantContext,
                 business: Odoo20BusinessService, *, plan_id: int,
                 comment: str = "", expires_in_hours: int = 24,
                 expected_review: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record an approval by an authorised approver bound to an Odoo user."""
    ctx.require(Role.APPROVER, Role.ADMIN)
    linked = _linked(store, ctx.tenant_id, plan_id)
    if linked is None:
        raise LookupError("plan_task_missing")
    prepared = business.load_prepared(int(plan_id))
    require_complete_review_snapshot(prepared.facts)
    if expected_review is not None and any(
            not value or getattr(prepared, key, None) != value
            for key, value in expected_review.items()):
        raise ValueError("plan_review_stale")
    if prepared.execution_identity:
        _require_current_approval(prepared)
        # The plan already carries an approval.  Repeating the command must not
        # create a second decision or a second execution identity. Finish the
        # attention step if the original request stopped after the ERP approval.
        attention_state = linked.attention_state
        if attention_state == "open":
            try:
                answer_attention(store, ctx, plan_id=int(plan_id))
            except (ControlNotFound, LookupError):
                pass
            attention_state = "answered"
        return {"plan_id": int(plan_id), "decision_id": None,
                "attention": attention_state, "already_approved": True}
    approver_uid = odoo_user_id_for(store, principal_id=ctx.principal_id,
                                    tenant_id=ctx.tenant_id)
    if approver_uid is None:
        raise PermissionError("odoo_approval_binding_required")
    decision_id = business.approve(prepared, approver_uid=int(approver_uid),
                                   comment=comment,
                                   expires_in_hours=expires_in_hours)
    attention_state = linked.attention_state
    if linked.attention_state == "open":
        try:
            answer_attention(store, ctx, plan_id=int(plan_id))
        except (ControlNotFound, LookupError):
            # A concurrent approval already answered it; the Odoo decision above
            # is still the authoritative record.
            pass
        attention_state = "answered"
    ControlEngine(store).append_event(
        ctx, ticket_id=linked.ticket_id, kind="plan_approved",
        payload={"plan_id": int(plan_id), "decision_id": int(decision_id),
                 "approver": ctx.principal_id,
                 "odoo_user_id": int(approver_uid), "comment": comment[:500]})
    return {"plan_id": int(plan_id), "decision_id": int(decision_id),
            "attention": attention_state, "already_approved": False,
            "approver_odoo_user_id": int(approver_uid)}


def request_execution(store: ControlStore, ctx: TenantContext,
                      business: Odoo20BusinessService, *, plan_id: int) -> dict[str, Any]:
    """Queue the durable execution effect for an approved plan.

    Only an approved plan may be requested: the approval's execution identity is
    read from Odoo, never supplied by the caller.
    """
    ctx.require(Role.APPROVER, Role.ADMIN)
    linked = _linked(store, ctx.tenant_id, plan_id)
    if linked is None:
        raise LookupError("plan_task_missing")
    prepared = business.load_prepared(int(plan_id))
    require_complete_review_snapshot(prepared.facts)
    if not prepared.execution_identity:
        raise PermissionError("approval_required")
    _require_current_approval(prepared)
    approval_hash = hashlib.sha256(prepared.execution_identity.encode()).hexdigest()
    key = f"apply:{int(plan_id)}:{prepared.plan_hash}:{approval_hash}"
    legacy_key = f"apply:{int(plan_id)}:{str(prepared.plan_hash)[:12]}"
    engine = ControlEngine(store)
    with store.transaction() as cursor:
        engine._authorize(cursor, ctx)  # noqa: SLF001
        # The ticket lock also serializes revocation. Lookup and enqueue must be
        # one transaction: two concurrent requests must promise only one effect.
        ticket = cursor.execute(
            "SELECT state, mandate_id FROM control_tickets WHERE tenant_id=%s AND id=%s "
            "FOR UPDATE", (ctx.tenant_id, linked.ticket_id)).fetchone()
        if ticket is None:
            raise ControlNotFound("ticket_not_found")
        if normalize_ticket_state(ticket["state"]) in {
                TicketState.CANCELLED.value, TicketState.CLOSED.value}:
            raise StateTransitionRejected("ticket_not_active")
        attention = cursor.execute(
            "SELECT a.state FROM control_odoo_plan_links l JOIN control_attentions a "
            "ON a.tenant_id=l.tenant_id AND a.id=l.attention_id "
            "WHERE l.tenant_id=%s AND l.plan_id=%s",
            (ctx.tenant_id, int(plan_id))).fetchone()
        if attention is None or attention["state"] != "answered":
            raise PermissionError("approval_attention_required")
        row = cursor.execute(
            "SELECT e.id, e.state FROM control_effects e WHERE e.tenant_id=%s AND e.ticket_id=%s "
            "AND (e.idempotency_key=%s OR (e.idempotency_key=%s AND e.state<>'cancelled' "
            "AND NOT EXISTS (SELECT 1 FROM control_events v WHERE v.tenant_id=e.tenant_id "
            "AND v.ticket_id=e.ticket_id AND v.kind='plan_approval_revoked' "
            "AND v.created_at>=e.created_at))) ORDER BY e.created_at DESC LIMIT 1",
            (ctx.tenant_id, linked.ticket_id, key, legacy_key)).fetchone()
        if row is not None:
            return {"effect_id": str(row["id"]), "state": str(row["state"]),
                    "reused": True, "ticket_id": linked.ticket_id}
        effect_id = engine.promise_effect(
            cursor, tenant_id=ctx.tenant_id, ticket_id=linked.ticket_id,
            kind=EffectKind.APPLY_APPROVED_PLAN.value,
            payload={"plan_id": int(plan_id),
                     "execution_identity": prepared.execution_identity},
            idempotency_key=key,
            mandate_id=str(ticket["mandate_id"]) if ticket["mandate_id"] else None)
        engine._append_event(cursor, ctx.tenant_id, linked.ticket_id, "effect_requested", {  # noqa: SLF001
            "effect_id": effect_id, "kind": EffectKind.APPLY_APPROVED_PLAN.value,
            "plan_id": int(plan_id), "execution_identity": prepared.execution_identity})
    return {"effect_id": effect_id, "state": "pending", "reused": False,
            "ticket_id": linked.ticket_id}


def revoke_approval(store: ControlStore, ctx: TenantContext,
                    business: Odoo20BusinessService, *, plan_id: int,
                    reason: str = "") -> dict[str, Any]:
    """Withdraw an approval that has not produced an execution operation.

    Revocation is not a weaker approval: the Odoo decision is marked revoked,
    pending execution effects are cancelled, and the task returns to approval so
    a person must decide again.  A plan that already dispatched a business write
    cannot be revoked here; it needs reconciliation instead.
    """
    ctx.require(Role.APPROVER, Role.ADMIN)
    linked = _linked(store, ctx.tenant_id, plan_id)
    if linked is None:
        raise LookupError("plan_task_missing")
    prepared = business.load_prepared(int(plan_id))
    if not prepared.execution_identity:
        raise PermissionError("approval_not_found")
    if str(prepared.plan_state) not in {"approved", "awaiting_approval"}:
        raise PermissionError("plan_not_revocable")
    operations = business.adapter.search_read(
        "forgeflow.execution_operation", [("plan_id", "=", int(plan_id))],
        ["id"], limit=1)
    if operations:
        raise PermissionError("plan_already_executing")
    result = business.revoke(prepared, reason=reason)
    engine = ControlEngine(store)
    with store.transaction() as cursor:
        engine._authorize(cursor, ctx)  # noqa: SLF001
        cursor.execute("SELECT id FROM control_tickets WHERE tenant_id=%s AND id=%s "
                       "FOR UPDATE", (ctx.tenant_id, linked.ticket_id)).fetchone()
        cancelled = cursor.execute(
            "UPDATE control_effects SET state='cancelled', updated_at=now() "
            "WHERE tenant_id=%s AND ticket_id=%s AND kind=%s "
            "AND state IN ('pending','running') RETURNING id",
            (ctx.tenant_id, linked.ticket_id, EffectKind.APPLY_APPROVED_PLAN.value)
        ).fetchall()
        attention = engine._open_attention(  # noqa: SLF001
            cursor, ctx.tenant_id, linked.ticket_id, AttentionNeed.APPROVAL.value,
            question=("The previous approval was withdrawn. Review this exact plan "
                      "version again before execution."),
            detail={"plan_id": int(plan_id), "revoked": True,
                    "revoked_execution_identity": prepared.execution_identity})
        if attention is not None:
            cursor.execute(
                "UPDATE control_odoo_plan_links SET attention_id=%s "
                "WHERE tenant_id=%s AND plan_id=%s AND ticket_id=%s",
                (attention, ctx.tenant_id, int(plan_id), linked.ticket_id))
        engine._append_event(  # noqa: SLF001
            cursor, ctx.tenant_id, linked.ticket_id, "plan_approval_revoked",
            {"plan_id": int(plan_id), "reason": reason[:500],
             "revoked_execution_identity": prepared.execution_identity,
             "cancelled_effects": [str(row["id"]) for row in cancelled],
             "attention_id": attention})
    return {"plan_id": int(plan_id), "revoked": not result.get("already_revoked", False),
            "already_revoked": bool(result.get("already_revoked")),
            "cancelled_effects": len(cancelled), "attention_id": attention,
            "ticket_id": linked.ticket_id}


def reconcile_execution(store: ControlStore, ctx: TenantContext,
                        business: Odoo20BusinessService, *, plan_id: int,
                        idempotency_key: str) -> dict[str, Any]:
    """Read an in-flight operation back; never dispatch it again."""
    ctx.require(Role.PLANNER, Role.APPROVER, Role.ADMIN)
    linked = _linked(store, ctx.tenant_id, plan_id)
    if linked is None:
        raise LookupError("plan_task_missing")
    prepared = business.load_prepared(int(plan_id))
    if not idempotency_key:
        operations = business.adapter.search_read(
            "forgeflow.execution_operation", [("plan_id", "=", int(plan_id))],
            ["idempotency_key", "state"], limit=100)
        idempotency_key = next(
            (str(row["idempotency_key"]) for row in operations
             if str(row["state"]) in {"unknown", "dispatched", "reconciling"}
             and row.get("idempotency_key")), "")
        if not idempotency_key:
            raise ValueError("reconcile_identity_missing")
    result = business.reconcile(prepared, idempotency_key=idempotency_key)
    state = str(result.state)
    engine = ControlEngine(store)
    verify_effect = None
    if state in {"succeeded", "verified"}:
        engine.mark_execution(ctx, ticket_id=linked.ticket_id, stage="verifying",
                              reason="execution_reconciled")
        verify_effect = engine.request_effect(
            ctx, ticket_id=linked.ticket_id,
            kind=EffectKind.VERIFY_EXECUTION.value,
            payload={"plan_id": int(plan_id)},
            idempotency_key=f"verify:{plan_id}:{prepared.plan_hash[:12]}")
        action = "continue_verification"
    elif state in {"unknown", "dispatched", "reconciling"}:
        from .engine import StateTransitionRejected
        try:
            engine.mark_execution(ctx, ticket_id=linked.ticket_id, stage="blocked",
                                  reason="execution_result_unknown")
            engine.open_attention(
                ctx, ticket_id=linked.ticket_id,
                need_type=AttentionNeed.RESULT_UNKNOWN.value,
                question=("The reconciliation could not confirm whether the ERP write "
                          "was applied. Check the original receipt before retrying."),
                detail={"plan_id": int(plan_id), "idempotency_key": idempotency_key,
                        "operation_id": int(result.operation_id)})
            action = "needs_investigation"
        except StateTransitionRejected:
            # A completed ticket cannot be moved back to blocked. The read-only
            # reconciliation is still returned; callers must not replay the write.
            action = "needs_investigation_completed_ticket"
    else:
        engine.mark_execution(ctx, ticket_id=linked.ticket_id, stage="blocked",
                              reason=f"execution_reconcile_{state}")
        action = "blocked"
    engine.append_event(
        ctx, ticket_id=linked.ticket_id, kind="execution_reconciled",
        payload={"plan_id": int(plan_id), "operation_id": int(result.operation_id),
                 "state": state, "next": action})
    ticket = engine.get_ticket(ctx, linked.ticket_id)
    return {"plan_id": int(plan_id), "operation_id": int(result.operation_id),
            "state": state, "next": action, "ticket_state": ticket["state"],
            "verify_effect": verify_effect, "readback": result.readback}
