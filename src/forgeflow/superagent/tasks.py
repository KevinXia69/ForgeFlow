"""Read projections plus thin command facades for Odoo 20 plan tasks.

This module no longer writes task-domain state.  Every mutation is delegated to
``ControlEngine`` so that a state change and its event always commit together
(plan section 7.3).  The functions here exist because the HTTP layer and tests
speak in plan ids and attention ids rather than engine commands.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg.types.json import Jsonb

from .contracts import AttentionNeed, TicketState
from .engine import ControlEngine
from .identity import TenantContext
from .store import ControlStore

APPROVAL_NEED = AttentionNeed.APPROVAL.value


@dataclass(frozen=True, slots=True)
class PlanTask:
    plan_id: int
    ticket_id: str
    attention_id: str
    attention_state: str
    need_type: str


def link_plan(store: ControlStore, ctx: TenantContext, *, plan_id: int, goal: str) -> PlanTask:
    """Attach an existing Odoo 20 recovery plan to a waiting approval task."""
    engine = ControlEngine(store)
    with store.transaction() as cursor:
        existing = cursor.execute(
            "SELECT ticket_id, attention_id FROM control_odoo_plan_links "
            "WHERE tenant_id=%s AND plan_id=%s", (ctx.tenant_id, int(plan_id))).fetchone()
    if existing is not None:
        return _task(store, ctx.tenant_id, int(plan_id))
    opened = engine.open_task(
        ctx, object_ref=f"forgeflow.recovery_plan:{int(plan_id)}", goal=goal,
        state=TicketState.AWAITING_APPROVAL.value,
        attention={"need_type": AttentionNeed.APPROVAL.value,
                   "question": "Approve the prepared Odoo 20 recovery plan.",
                   "detail": {"plan_id": int(plan_id)}})
    with store.transaction() as cursor:
        cursor.execute(
            "INSERT INTO control_odoo_plan_links(plan_id, tenant_id, ticket_id, thread_id, "
            "attention_id, principal_id) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, plan_id) DO NOTHING",
            (int(plan_id), ctx.tenant_id, opened["ticket_id"], opened["thread_id"],
             opened["attention_id"], ctx.principal_id))
    return _task(store, ctx.tenant_id, int(plan_id))


def answer_attention(store: ControlStore, ctx: TenantContext, *, plan_id: int) -> PlanTask:
    row = store.connection.execute(
        "SELECT attention_id FROM control_odoo_plan_links WHERE tenant_id=%s AND plan_id=%s",
        (ctx.tenant_id, int(plan_id))).fetchone()
    store.connection.commit()
    if row is None:
        raise LookupError("plan_task_missing")
    ControlEngine(store).answer_attention(ctx, attention_id=str(row["attention_id"]))
    return _task(store, ctx.tenant_id, int(plan_id))


def plan_task(store: ControlStore, *, tenant_id: str, plan_id: int) -> PlanTask | None:
    try:
        return _task(store, tenant_id, int(plan_id))
    except LookupError:
        return None


def _task(store: ControlStore, tenant_id: str, plan_id: int) -> PlanTask:
    row = store.connection.execute(
        "SELECT l.ticket_id, l.attention_id, a.state, a.need_type "
        "FROM control_odoo_plan_links l JOIN control_attentions a "
        "ON a.id=l.attention_id AND a.tenant_id=l.tenant_id "
        "WHERE l.tenant_id=%s AND l.plan_id=%s", (tenant_id, plan_id)).fetchone()
    store.connection.commit()
    if row is None:
        raise LookupError("plan_task_missing")
    return PlanTask(plan_id, str(row["ticket_id"]), str(row["attention_id"]),
                    str(row["state"]), str(row["need_type"]))


def list_plan_tasks(store: ControlStore, *, tenant_id: str, principal_id: str,
                    limit: int = 50) -> list[dict]:
    from .contracts import normalize_ticket_state, phase_for, user_state_for

    rows = store.connection.execute(
        "SELECT l.plan_id, l.ticket_id, l.attention_id, a.state AS attention_state, "
        "a.need_type, a.blocking, t.goal, t.state AS ticket_state, l.created_at "
        "FROM control_odoo_plan_links l "
        "JOIN control_attentions a ON a.id=l.attention_id AND a.tenant_id=l.tenant_id "
        "JOIN control_tickets t ON t.id=l.ticket_id AND t.tenant_id=l.tenant_id "
        "WHERE l.tenant_id=%s AND l.principal_id=%s "
        "ORDER BY l.created_at DESC, l.plan_id DESC LIMIT %s",
        (tenant_id, principal_id, max(1, min(int(limit), 100)))).fetchall()
    values = [{"plan_id": int(row["plan_id"]), "ticket_id": str(row["ticket_id"]),
               "goal": str(row["goal"]),
               "attention": str(row["attention_state"]), "attention_id": str(row["attention_id"]),
               "need": str(row["need_type"]), "blocking": bool(row["blocking"]),
               "state": normalize_ticket_state(row["ticket_state"]),
               "user_state": user_state_for(row["ticket_state"]),
               "phase": phase_for(row["ticket_state"]),
               } for row in rows]
    store.connection.commit()
    return values


def retry_failed_ticket(store: ControlStore, ctx: TenantContext, *, ticket_id: str) -> int:
    return ControlEngine(store).retry_ticket(ctx, ticket_id=ticket_id)


def list_failure_attentions(store: ControlStore, *, tenant_id: str) -> list[dict]:
    rows = store.connection.execute(
        "SELECT a.ticket_id, a.id AS attention_id, t.goal, a.state, a.question, a.need_type "
        "FROM control_attentions a JOIN control_tickets t "
        "ON t.id=a.ticket_id AND t.tenant_id=a.tenant_id "
        "WHERE a.tenant_id=%s AND a.need_type IN ('failure','result_unknown','budget','rule','system') "
        "AND a.state='open' ORDER BY a.created_at LIMIT 50", (tenant_id,)).fetchall()
    values = [{"ticket_id": str(row["ticket_id"]), "attention_id": str(row["attention_id"]),
               "goal": str(row["goal"]), "question": str(row["question"]),
               "need": str(row["need_type"])} for row in rows]
    store.connection.commit()
    return values


def list_tickets(store: ControlStore, *, tenant_id: str, limit: int = 50) -> list[dict]:
    from .contracts import normalize_ticket_state, user_state_for

    rows = store.connection.execute(
        "SELECT id, goal, state, goal_kind, progress_at, spent_tokens, spent_effects, "
        "deadline_at, created_at, updated_at FROM control_tickets "
        "WHERE tenant_id=%s AND state <> 'closed' ORDER BY updated_at DESC LIMIT %s",
        (tenant_id, max(1, min(int(limit), 100)))).fetchall()
    values = [{"task_id": str(row["id"]), "goal": str(row["goal"]),
               "state": normalize_ticket_state(row["state"]),
               "user_state": user_state_for(row["state"]),
               "goal_kind": row["goal_kind"], "spent_tokens": int(row["spent_tokens"] or 0),
               "spent_effects": int(row["spent_effects"] or 0)} for row in rows]
    store.connection.commit()
    return values


def hold_ticket_effects(store: ControlStore, ctx: TenantContext, *, ticket_id: str) -> int:
    """Park pending effects behind a blocking attention."""
    engine = ControlEngine(store)
    with store.transaction() as cursor:
        engine._park_ticket(cursor, ctx.tenant_id, ticket_id)  # noqa: SLF001
        rows = cursor.execute(
            "SELECT id FROM control_effects WHERE tenant_id=%s AND ticket_id=%s "
            "AND state='pending'", (ctx.tenant_id, ticket_id)).fetchall()
    return len(rows)


def open_attention(store: ControlStore, ctx: TenantContext, *, ticket_id: str,
                   need_type: str, question: str) -> str | None:
    return ControlEngine(store).open_attention(
        ctx, ticket_id=ticket_id, need_type=need_type, question=question)


def list_open_attentions(store: ControlStore, *, tenant_id: str) -> list[dict]:
    from .contracts import normalize_ticket_state, phase_for, user_state_for

    rows = store.connection.execute(
        "SELECT a.id, a.ticket_id, a.need_type, a.question, a.blocking, t.goal, "
        "t.state AS ticket_state, l.plan_id "
        "FROM control_attentions a JOIN control_tickets t "
        "ON t.id=a.ticket_id AND t.tenant_id=a.tenant_id "
        "LEFT JOIN control_odoo_plan_links l "
        "ON l.ticket_id=a.ticket_id AND l.tenant_id=a.tenant_id "
        "WHERE a.tenant_id=%s AND a.state='open' ORDER BY a.created_at LIMIT 100",
        (tenant_id,)).fetchall()
    values = [{"attention_id": str(row["id"]), "ticket_id": str(row["ticket_id"]),
               "need": str(row["need_type"]), "question": str(row["question"]),
               "blocking": bool(row["blocking"]), "goal": str(row["goal"]),
               "state": normalize_ticket_state(row["ticket_state"]),
               "user_state": user_state_for(row["ticket_state"]),
               "phase": phase_for(row["ticket_state"]),
               "plan_id": int(row["plan_id"]) if row["plan_id"] else None} for row in rows]
    store.connection.commit()
    return values


def answer_attention_by_id(store: ControlStore, ctx: TenantContext, *,
                           attention_id: str, answer: dict | None = None) -> None:
    ControlEngine(store).answer_attention(ctx, attention_id=attention_id, answer=answer)


def cancel_ticket(store: ControlStore, ctx: TenantContext, *, ticket_id: str) -> int:
    return ControlEngine(store).cancel_ticket(ctx, ticket_id=ticket_id)


def record_plan_decision(store: ControlStore, ctx: TenantContext, *, plan_id: int,
                         decision: dict) -> None:
    """Record an approval decision against the plan's task, without writing state directly."""
    engine = ControlEngine(store)
    row = store.connection.execute(
        "SELECT attention_id FROM control_odoo_plan_links WHERE tenant_id=%s AND plan_id=%s",
        (ctx.tenant_id, int(plan_id))).fetchone()
    store.connection.commit()
    if row is None:
        raise LookupError("plan_task_missing")
    engine.answer_attention(ctx, attention_id=str(row["attention_id"]), answer=decision)


def plan_payload(store: ControlStore, *, tenant_id: str, plan_id: int) -> dict:
    row = store.connection.execute(
        "SELECT payload FROM control_events WHERE tenant_id=%s AND ticket_id=("
        "SELECT ticket_id FROM control_odoo_plan_links WHERE tenant_id=%s AND plan_id=%s) "
        "AND kind='plan_prepared' ORDER BY sequence DESC LIMIT 1",
        (tenant_id, tenant_id, int(plan_id))).fetchone()
    store.connection.commit()
    return dict(row["payload"]) if row else {}


def serialize_answer(answer: dict | None) -> Jsonb:
    return Jsonb(answer or {})


def ticket_detail(store: ControlStore, *, tenant_id: str, ticket_id: str) -> dict | None:
    """One task with its user-facing state, open attentions and recent events."""
    from .contracts import normalize_ticket_state, phase_for, user_state_for

    row = store.connection.execute(
        "SELECT id, goal, goal_kind, state, spent_tokens, spent_effects, deadline_at, "
        "created_at, updated_at, progress_at, stop_reason "
        "FROM control_tickets WHERE tenant_id=%s AND id=%s", (tenant_id, ticket_id)).fetchone()
    store.connection.commit()
    if row is None:
        return None
    attentions = [{"attention_id": str(item["id"]), "need": str(item["need_type"]),
                   "question": str(item["question"]), "blocking": bool(item["blocking"])}
                  for item in store.connection.execute(
                      "SELECT id, need_type, question, blocking FROM control_attentions "
                      "WHERE tenant_id=%s AND ticket_id=%s AND state='open' "
                      "ORDER BY created_at", (tenant_id, ticket_id)).fetchall()]
    events = [{"sequence": int(item["sequence"]), "kind": str(item["kind"]),
               "payload": item["payload"]}
              for item in store.connection.execute(
                  "SELECT sequence, kind, payload FROM control_events "
                  "WHERE tenant_id=%s AND ticket_id=%s ORDER BY sequence DESC LIMIT 50",
                  (tenant_id, ticket_id)).fetchall()]
    store.connection.commit()
    return {
        "ticket_id": str(row["id"]), "goal": str(row["goal"]),
        "goal_kind": row["goal_kind"],
        "state": normalize_ticket_state(row["state"]),
        "user_state": user_state_for(row["state"]),
        "phase": phase_for(row["state"]),
        "spent_tokens": int(row["spent_tokens"] or 0),
        "spent_effects": int(row["spent_effects"] or 0),
        "deadline_at": row["deadline_at"], "stop_reason": row["stop_reason"],
        "progress_at": row["progress_at"],
        "attentions": attentions,
        "events": events,
    }


def link_plan_to_ticket(store: ControlStore, ctx: TenantContext, *, plan_id: int,
                        ticket_id: str, question: str | None = None) -> str | None:
    """Record an approved-plan link on an *existing* ticket and ask for approval.

    Used when the plan belongs to work that is already running: the original
    matter keeps its thread, its mandate and its history instead of spawning a
    second task for the same business object.
    """
    engine = ControlEngine(store)
    attention_id = engine.open_attention(
        ctx, ticket_id=ticket_id, need_type="approval",
        question=question or "Approve the prepared recovery plan.",
        detail={"plan_id": int(plan_id)})
    if attention_id is None:
        raise LookupError("plan_ticket_not_active")
    with store.transaction() as cursor:
        thread = cursor.execute(
            "SELECT thread_id FROM control_tickets WHERE id=%s AND tenant_id=%s",
            (ticket_id, ctx.tenant_id)).fetchone()
        if thread is None:
            raise LookupError("ticket_missing")
        cursor.execute(
            "INSERT INTO control_odoo_plan_links(plan_id, tenant_id, ticket_id, "
            "thread_id, attention_id, principal_id) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, plan_id) DO UPDATE SET "
            "ticket_id=EXCLUDED.ticket_id, attention_id=EXCLUDED.attention_id, "
            "thread_id=EXCLUDED.thread_id",
            (int(plan_id), ctx.tenant_id, ticket_id, thread["thread_id"],
             attention_id, ctx.principal_id))
        written = cursor.execute(
            "SELECT count(*) AS count FROM control_odoo_plan_links "
            "WHERE tenant_id=%s AND plan_id=%s AND ticket_id=%s",
            (ctx.tenant_id, int(plan_id), ticket_id)).fetchone()["count"]
        if int(written) != 1:
            raise RuntimeError("plan_link_not_written")
    return attention_id
