"""Dispatcher handlers for the plan half of a persistent recovery chain.

The chain is driven entirely by durable effects: ``prepare_plan`` turns an
investigated exception into a solved, approval-bound Odoo plan; a separate
approval command records the decision; ``apply_approved_plan`` executes exactly
that approved plan atomically; ``verify_execution`` reads the ERP independently
and closes the ticket only when every verification layer passes.

Nothing here trusts a previous step's optimistic return value.  The apply
handler re-loads the plan and requires the approval's execution identity; the
verify handler never reads the executor's ``succeeded`` flag as proof.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..integrations.odoo20.business import Odoo20BusinessService
from ..integrations.odoo20.procurement import (
    ProcurementError,
    collect_replacement_cases,
)
from ..planning.actions import ActionCompileError
from ..planning.replacement import plan_to_dict, replacement_actions
from .contracts import EffectKind, TicketState
from .engine import StateTransitionRejected
from .manufacturing import ManufacturingPlanError, prepare_manufacturing_plan
from .store import ControlStore
from .tasks import link_plan_to_ticket


def _linked_plan(store: ControlStore, tenant_id: str,
                 ticket_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT l.plan_id, a.id AS attention_id, a.state AS attention_state "
        "FROM control_odoo_plan_links l "
        "JOIN control_attentions a ON a.id=l.attention_id AND a.tenant_id=l.tenant_id "
        "WHERE l.tenant_id=%s AND l.ticket_id=%s ORDER BY l.created_at DESC LIMIT 1",
        (tenant_id, ticket_id)).fetchone()
    store.connection.commit()
    if row is None:
        return None
    return {"plan_id": int(row["plan_id"]), "attention_id": str(row["attention_id"]),
            "attention_state": str(row["attention_state"])}


def _linked_plan_id(store: ControlStore, tenant_id: str, ticket_id: str) -> int | None:
    linked = _linked_plan(store, tenant_id, ticket_id)
    return int(linked["plan_id"]) if linked else None


class ReplacementPreparationError(ValueError):
    """A bounded preparation refusal shared by the dispatcher and the workbench."""

    def __init__(self, code: str, message: str, *, need: str = "failure",
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.need = need
        self.detail = detail or {}


def prepare_replacement_plan(business: Odoo20BusinessService, *, order_id: int,
                             tenant_id: str, name: str | None = None,
                             time_limit_seconds: float = 10.0) -> dict[str, Any]:
    """Solve and prepare one replacement plan for every product on the order.

    Both the ``prepare_plan`` effect and the workbench API call this, so an
    operator's button and the dispatcher cannot produce different plans from the
    same commitment.
    """
    try:
        cases = collect_replacement_cases(business.adapter, order_id=int(order_id))
    except ProcurementError as error:
        raise ReplacementPreparationError(
            error.code, f"The commitment could not be read: {error.code}.",
            need="fact") from None
    plans: list[Any] = []
    actions: list[dict[str, Any]] = []
    for case in cases:
        try:
            plan = case.planner(time_limit_seconds=time_limit_seconds).solve()
        except ValueError as error:
            raise ReplacementPreparationError(
                "replacement_input_invalid",
                f"The replacement input was rejected: {error}"[:300],
                need="fact") from None
        if not plan.solved:
            raise ReplacementPreparationError(
                "replacement_unsolved",
                f"The replacement solver returned {plan.status.value}.")
        if plan.unmet > 0:
            raise ReplacementPreparationError(
                "replacement_shortfall",
                (f"No supplier or stock can cover {plan.unmet} of "
                 f"{plan.demand.quantity} {plan.demand.product_code} by "
                 f"{case.due_date.isoformat()}. Decide whether to accept a later "
                 "date or change the commitment."),
                detail={"plan": plan_to_dict(plan),
                        "product_id": plan.demand.product_id})
        try:
            compiled = replacement_actions(
                plan, origin_ref=f"purchase.order:{int(order_id)}", unit=case.unit,
                # A multi-product order can buy the same supplier in the same
                # period for different products; action ids must stay unique.
                identity_suffix=(f"-p{case.product['id']}" if len(cases) > 1 else ""))
        except ActionCompileError as error:
            raise ReplacementPreparationError(
                f"replacement_compile_failed:{error.code}",
                f"The solved plan cannot become an executable action set: {error.code}.",
                detail={"plan": plan_to_dict(plan)}) from None
        plans.append(plan)
        actions.extend(compiled["actions"])
    primary = cases[0]
    facts = business.recovery_snapshot(order_id=int(order_id))
    facts["affected_targets"] = sorted({
        f"{action['target_model']}:{action['target_id']}"
        for action in actions if int(action.get("target_id") or 0) > 0})
    facts["replacement"] = plan_to_dict(plans[0])
    facts["replacements"] = [plan_to_dict(plan) for plan in plans]
    facts["suppliers"] = [
        {"supplier_ref": offer.supplier_ref, "supplier_id": offer.supplier_id,
         "price": offer.price, "lead_periods": offer.lead_periods,
         "min_order_quantity": offer.min_order_quantity}
        for plan in plans for offer in plan.offers]
    exception_id = business.adapter.create("forgeflow.exception_event", {
        "name": f"Replace cancelled commitment {primary.order.get('name') or order_id}",
        "event_type": "supplier_disruption",
        "partner_id": (int(primary.order["partner_id"][0])
                       if isinstance(primary.order.get("partner_id"), (list, tuple))
                       else int(primary.order.get("partner_id") or 0)) or False,
        "occurred_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "description": "; ".join(
            f"{plan.demand.product_code} x{plan.demand.quantity}" for plan in plans),
        "company_id": primary.company_id or False,
    })
    prepared = business.prepare_recovery(
        exception_id, list(actions), facts,
        tenant_ref=f"{tenant_id}:company:{primary.company_id}",
        name=name or (f"Replacement purchase for {primary.order.get('name') or order_id}"),
        expires_in_hours=72)
    return {"prepared": prepared, "actions": actions, "plan": plans[0],
            "plans": plans, "case": primary, "cases": cases, "facts": facts}

class PlanEffects:
    """The three plan effects plus their exact preconditions."""

    def __init__(self, business: Odoo20BusinessService, *,
                 time_limit_seconds: float = 10.0) -> None:
        self.business = business
        self.time_limit_seconds = float(time_limit_seconds)

    # -- registration --------------------------------------------------------
    def register(self, dispatcher) -> None:
        dispatcher.register_handler(EffectKind.PREPARE_PLAN.value, self.prepare_plan)
        dispatcher.register_handler(EffectKind.APPLY_APPROVED_PLAN.value,
                                    self.apply_approved_plan)
        dispatcher.register_handler(EffectKind.VERIFY_EXECUTION.value,
                                    self.verify_execution)
        dispatcher.register_handler(EffectKind.RECONCILE_EXECUTION.value,
                                    self.reconcile_execution)

    # -- prepare -------------------------------------------------------------
    def prepare_plan(self, context) -> dict[str, Any]:
        store = context.engine.store
        existing = _linked_plan(store, context.tenant_id, context.ticket_id)
        if existing is not None:
            # A retried preparation must not create a second plan for one ticket.
            return {"status": "completed", "plan_id": existing["plan_id"],
                    "attention_id": existing["attention_id"], "reused": True}
        object_ref = str(context.payload.get("object_ref") or "")
        model, _, identifier = object_ref.partition(":")
        if (model == "mrp.production" and identifier
                and all(part.isdigit() for part in identifier.split(","))):
            try:
                manufacturing = prepare_manufacturing_plan(
                    self.business, mo_ids=[int(part) for part in identifier.split(",")],
                    tenant_id=context.tenant_id,
                    time_limit_seconds=self.time_limit_seconds)
            except ManufacturingPlanError as error:
                return self._blocked(context, error.code, error.message,
                                     need=error.need, detail=error.detail)
            prepared = manufacturing["prepared"]
            actions = manufacturing["actions"]
            facts = manufacturing["facts"]
            produced = facts["manufacturing"]["produced"]
            question = (f"Approve the replacement manufacturing order and component "
                        f"purchases for {sum(produced.values())} units "
                        f"({', '.join(f'{v} {k}' for k, v in produced.items())}).")
            attention_id = link_plan_to_ticket(
                store, context.ctx, plan_id=prepared.plan_id,
                ticket_id=context.ticket_id, question=question)
            context.engine.append_event(
                context.ctx, ticket_id=context.ticket_id, kind="plan_prepared",
                payload={"plan_id": prepared.plan_id, "plan_hash": prepared.plan_hash,
                         "snapshot_hash": prepared.snapshot_hash,
                         "attention_id": attention_id, "actions": list(actions),
                         "plan": facts.get("manufacturing"), "order_ref": object_ref})
            return {"status": "completed", "plan_id": prepared.plan_id,
                    "plan_hash": prepared.plan_hash, "attention_id": attention_id,
                    "action_count": len(actions), "plan": facts.get("manufacturing")}
        if model != "purchase.order" or not identifier.isdigit():
            return self._blocked(context, "replacement_object_unsupported",
                                 f"The replacement planner cannot treat {object_ref!r} "
                                 "as a purchase commitment.", need="failure")
        try:
            prepared_plan = prepare_replacement_plan(
                self.business, order_id=int(identifier),
                tenant_id=context.tenant_id,
                time_limit_seconds=self.time_limit_seconds)
        except ReplacementPreparationError as error:
            return self._blocked(context, error.code, error.message,
                                 need=error.need, detail=error.detail)
        prepared = prepared_plan["prepared"]
        plans = prepared_plan["plans"]
        actions = prepared_plan["actions"]
        quantities = ", ".join(
            f"{plan.demand.quantity} {plan.demand.product_code}" for plan in plans)
        question = (f"Approve the replacement purchase of {quantities} from "
                    f"{', '.join(sorted({name for plan in plans for name in plan.buy_by_supplier()})) or 'stock'}.")
        attention_id = link_plan_to_ticket(
            store, context.ctx, plan_id=prepared.plan_id,
            ticket_id=context.ticket_id, question=question)
        context.engine.append_event(
            context.ctx, ticket_id=context.ticket_id, kind="plan_prepared",
            payload={"plan_id": prepared.plan_id, "plan_hash": prepared.plan_hash,
                     "snapshot_hash": prepared.snapshot_hash,
                     "attention_id": attention_id, "actions": list(actions),
                     "plan": ([plan_to_dict(plan) for plan in plans]
                              if len(plans) > 1 else plan_to_dict(plans[0])),
                     "plans": [plan_to_dict(plan) for plan in plans],
                     "order_ref": object_ref})
        return {"status": "completed", "plan_id": prepared.plan_id,
                "plan_hash": prepared.plan_hash, "attention_id": attention_id,
                "action_count": len(actions),
                "plan": ([plan_to_dict(plan) for plan in plans]
                         if len(plans) > 1 else plan_to_dict(plans[0]))}

    # -- apply ---------------------------------------------------------------
    def apply_approved_plan(self, context) -> dict[str, Any]:
        plan_id = self._plan_id(context)
        if plan_id is None:
            return {"status": "blocked", "reason": "plan_missing"}
        if context.cancel_check():
            return {"status": "cancelled", "reason": "execution_cancelled"}
        prepared = self.business.load_prepared(plan_id)
        requested_identity = context.payload.get("execution_identity")
        if requested_identity and requested_identity != prepared.execution_identity:
            # A late handler from a revoked approval must never execute a later
            # approval or disturb that approval's new attention/effect.
            return {"status": "cancelled", "reason": "approval_superseded"}
        if not prepared.execution_identity:
            return self._blocked(
                context, "approval_missing",
                ("The plan has no approval decision bound to its digest; execution "
                 "is refused."), need="approval")
        try:
            self._mark(context, TicketState.EXECUTING.value, "execution_started")
        except StateTransitionRejected:
            return {"status": "cancelled", "reason": "ticket_not_active"}
        try:
            result = self.business.apply_plan(prepared)
        except Exception as error:  # noqa: BLE001 - the write may have happened
            self._mark(context, TicketState.BLOCKED.value, "execution_result_unknown")
            self._attention(
                context, "result_unknown",
                ("The ERP write raised before a confirmed receipt. The operation "
                 "result is unknown; reconcile it against the original receipt."),
                {"plan_id": plan_id, "error": type(error).__name__})
            reconcile_effect = self._request_reconcile(context, prepared)
            return {"status": "failed", "plan_id": plan_id, "state": "unknown",
                    "reconcile_effect": reconcile_effect}
        operations = [{"action_id": str(item.get("action_id")),
                       "state": str(item.get("state"))}
                      for item in result.actions]
        if not result.succeeded:
            self._mark(context, TicketState.BLOCKED.value, "execution_failed")
            self._attention(context, "failure",
                            f"The approved plan could not be executed: {result.state}.",
                            {"operations": operations})
            reconcile_effect = (self._request_reconcile(context, prepared)
                                if result.needs_reconcile else None)
            return {"status": "failed", "plan_id": plan_id, "state": result.state,
                    "operations": operations, "reconcile_effect": reconcile_effect}
        self._mark(context, TicketState.VERIFYING.value, "execution_committed")
        verification = self._request_verification(context, prepared)
        return {"status": "completed", "plan_id": plan_id, "state": result.state,
                "operations": operations, "verify_effect": verification}

    # -- verify --------------------------------------------------------------
    def verify_execution(self, context) -> dict[str, Any]:
        plan_id = self._plan_id(context)
        if plan_id is None:
            return {"status": "blocked", "reason": "plan_missing"}
        prepared = self.business.load_prepared(plan_id)
        readback = self.business.executor.readback(plan_id)
        verification = self.business.verify_plan(prepared)
        checks = [{"code": check.code, "status": check.status} for check in verification.checks]
        failed = [check.code for check in verification.checks
                  if check.status == "failed"]
        context.engine.append_event(
            context.ctx, ticket_id=context.ticket_id, kind="plan_verified",
            payload={"plan_id": plan_id, "status": verification.status,
                     "layers": verification.layers, "failed": failed,
                     "readback_targets": sorted(readback),
                     "check_count": len(checks)})
        if verification.verified:
            self._mark(context, TicketState.COMPLETED.value, "recovery_verified")
            return {"status": "completed", "plan_id": plan_id,
                    "verification": "verified", "checks": len(checks),
                    "readback_targets": sorted(readback)}
        self._mark(context, TicketState.BLOCKED.value,
                   f"verification_{verification.status}")
        self._attention(
            context, "failure",
            (f"Independent verification returned {verification.status}: "
             + (", ".join(failed[:5]) if failed else "no failed check reported")),
            {"plan_id": plan_id, "failed": failed[:20]})
        return {"status": "failed", "plan_id": plan_id,
                "verification": verification.status, "failed": failed[:20]}

    # -- reconcile -----------------------------------------------------------
    def reconcile_execution(self, context) -> dict[str, Any]:
        """Read the original operations back; never dispatch a second write."""
        plan_id = self._plan_id(context)
        if plan_id is None:
            return {"status": "blocked", "reason": "plan_missing"}
        prepared = self.business.load_prepared(plan_id)
        operations = self.business.adapter.search_read(
            "forgeflow.execution_operation", [("plan_id", "=", int(plan_id))],
            ["id", "action_id", "state", "idempotency_key"], limit=100)
        if not operations:
            return self._blocked(
                context, "operation_missing",
                "No execution operation exists for this plan; there is nothing to "
                "reconcile.", need="failure")
        outcomes: list[dict[str, Any]] = []
        unknown: list[str] = []
        for operation in operations:
            state = str(operation.get("state"))
            if state in {"unknown", "dispatched", "reconciling"}:
                key = str(operation.get("idempotency_key") or "")
                result = self.business.reconcile(prepared, idempotency_key=key)
                state = str(result.state)
            outcomes.append({"action_id": str(operation.get("action_id")),
                             "state": state})
            if state not in {"succeeded", "verified", "failed", "cancelled"}:
                unknown.append(str(operation.get("action_id")))
        if unknown:
            self._mark(context, TicketState.BLOCKED.value, "reconcile_unknown")
            self._attention(
                context, "result_unknown",
                ("Reconciliation could not confirm the ERP write. Check the original "
                 "receipt before retrying."),
                {"plan_id": plan_id, "operations": outcomes, "unknown": unknown})
            return {"status": "failed", "plan_id": plan_id, "state": "unknown",
                    "operations": outcomes}
        self._mark(context, TicketState.VERIFYING.value, "execution_reconciled")
        verification = self._request_verification(context, prepared)
        return {"status": "completed", "plan_id": plan_id, "state": "reconciled",
                "operations": outcomes, "verify_effect": verification}

    def _request_reconcile(self, context, prepared) -> str | None:
        store = context.engine.store
        row = store.connection.execute(
            "SELECT id FROM control_effects WHERE tenant_id=%s AND ticket_id=%s "
            "AND kind=%s AND state IN ('pending','running') ORDER BY created_at DESC "
            "LIMIT 1",
            (context.tenant_id, context.ticket_id,
             EffectKind.RECONCILE_EXECUTION.value)).fetchone()
        store.connection.commit()
        if row is not None:
            return str(row["id"])
        return context.engine.request_effect(
            context.ctx, ticket_id=context.ticket_id,
            kind=EffectKind.RECONCILE_EXECUTION.value,
            payload={"plan_id": prepared.plan_id},
            idempotency_key=(f"reconcile:{prepared.plan_id}:"
                             f"{str(prepared.plan_hash)[:12]}"))

    # -- helpers -------------------------------------------------------------
    def _plan_id(self, context) -> int | None:
        raw = context.payload.get("plan_id")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        return _linked_plan_id(context.engine.store, context.tenant_id,
                               context.ticket_id)

    def _mark(self, context, state: str, reason: str) -> None:
        stages = {TicketState.EXECUTING.value: "executing",
                  TicketState.VERIFYING.value: "verifying",
                  TicketState.COMPLETED.value: "completed",
                  TicketState.BLOCKED.value: "blocked"}
        context.engine.mark_execution(context.ctx, ticket_id=context.ticket_id,
                                      stage=stages[state], reason=reason)

    def _attention(self, context, need: str, question: str,
                   detail: dict[str, Any] | None = None) -> str | None:
        try:
            return context.engine.open_attention(
                context.ctx, ticket_id=context.ticket_id, need_type=need,
                question=question[:500], detail=detail)
        except StateTransitionRejected:
            return None

    def _blocked(self, context, code: str, question: str, *, need: str,
                 detail: dict[str, Any] | None = None) -> dict[str, Any]:
        attention = self._attention(context, need, question, detail)
        return {"status": "blocked", "reason": code, "attention_id": attention}

    def _request_verification(self, context, prepared) -> str | None:
        store = context.engine.store
        row = store.connection.execute(
            "SELECT id FROM control_effects WHERE tenant_id=%s AND ticket_id=%s "
            "AND kind=%s AND state IN ('pending','running') ORDER BY created_at DESC "
            "LIMIT 1",
            (context.tenant_id, context.ticket_id, EffectKind.VERIFY_EXECUTION.value)
        ).fetchone()
        store.connection.commit()
        if row is not None:
            return str(row["id"])
        return context.engine.request_effect(
            context.ctx, ticket_id=context.ticket_id,
            kind=EffectKind.VERIFY_EXECUTION.value,
            payload={"plan_id": prepared.plan_id},
            idempotency_key=(f"verify:{prepared.plan_id}:"
                             f"{str(prepared.plan_hash)[:12]}"))
