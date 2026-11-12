"""The single approved business-write gateway for the Odoo 20 slice.

The executor never accepts caller-supplied business parameters.  It names an
action that already exists in an approved plan; the addon resolves the record, the
quantity, the dates and the currency from that plan and validates the approval
binding inside the same transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adapter import Odoo20Adapter, Odoo20Error
from .contracts import ACTION_TYPES, CREATING_ACTIONS


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    operation_id: int
    state: str
    readback: dict[str, Any] | None
    receipt_id: int | None
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanExecutionResult:
    plan_id: int
    state: str
    actions: tuple[dict[str, Any], ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.state in {"executed", "verified"}

    @property
    def needs_reconcile(self) -> bool:
        return any(str(item.get("state")) in {"unknown", "reconciling"}
                   for item in self.actions)


class Odoo20BusinessExecutor:
    """Only calls the addon's approval-checked methods; no generic CRUD writes."""

    ALLOWED_ACTION_TYPES = frozenset(ACTION_TYPES)

    def __init__(self, adapter: Odoo20Adapter) -> None:
        self.adapter = adapter

    # -- single action -------------------------------------------------------
    def apply_action(self, plan_id: int, action_id: str, idempotency_key: str,
                     execution_identity: str) -> ExecutionResult:
        if not action_id:
            raise Odoo20Error("action_id_required")
        if not idempotency_key:
            raise Odoo20Error("idempotency_key_required")
        if not execution_identity:
            raise Odoo20Error("execution_identity_required")
        raw = self.adapter.apply_plan_action(plan_id, action_id, idempotency_key,
                                             execution_identity)
        return ExecutionResult(
            operation_id=int(raw.get("operation_id") or 0),
            state=str(raw.get("state")), readback=raw.get("readback"),
            receipt_id=(int(raw["receipt_id"]) if raw.get("receipt_id") else None),
            action_id=str(raw.get("action_id") or action_id))

    # -- whole plan ----------------------------------------------------------
    def apply_plan(self, plan_id: int, idempotency_prefix: str,
                   execution_identity: str) -> PlanExecutionResult:
        if not idempotency_prefix or not execution_identity:
            raise Odoo20Error("execution_identity_required")
        raw = self.adapter.apply_plan(plan_id, idempotency_prefix, execution_identity)
        return PlanExecutionResult(
            plan_id=int(raw.get("plan_id") or plan_id),
            state=str(raw.get("state")),
            actions=tuple(raw.get("actions") or ()))

    # -- legacy single-operation entry point ---------------------------------
    def apply(self, plan_id: int, idempotency_key: str, operation_type: str,
              target_model: str, target_id: int,
              payload: dict[str, Any] | None = None) -> ExecutionResult:
        """Kept for existing callers.  The addon still owns parameter resolution."""
        if str(operation_type) not in self.ALLOWED_ACTION_TYPES:
            raise Odoo20Error("operation_not_allowed")
        if payload:
            # A caller may not smuggle parameters past the approved action.
            raise Odoo20Error("parameters_not_accepted")
        raw = self.adapter.apply_approved_plan(
            plan_id, idempotency_key, operation_type, target_model, int(target_id), {})
        return ExecutionResult(
            operation_id=int(raw["operation_id"]), state=str(raw["state"]),
            readback=raw.get("readback"), receipt_id=raw.get("receipt_id"))

    def reconcile(self, plan_id: int, idempotency_key: str) -> ExecutionResult:
        raw = self.adapter.reconcile_plan(plan_id, idempotency_key)
        return ExecutionResult(
            operation_id=int(raw["operation_id"]), state=str(raw["state"]),
            readback=raw.get("readback"), receipt_id=raw.get("receipt_id"),
            action_id=(str(raw["action_id"]) if raw.get("action_id") else None))

    # -- uncertainty handling ------------------------------------------------
    def mark_result_unknown(self, plan_id: int, idempotency_key: str,
                            reason: str = "") -> dict[str, Any]:
        return self.adapter.mark_operation_unknown(plan_id, idempotency_key, reason)

    def resolve_unknown(self, plan_id: int, idempotency_key: str,
                        confirmed_applied: bool) -> dict[str, Any]:
        return self.adapter.resolve_unknown_operation(plan_id, idempotency_key,
                                                      confirmed_applied)

    # -- acceptance surfaces -------------------------------------------------
    def readback(self, plan_id: int) -> dict[str, Any]:
        return self.adapter.readback_plan(plan_id)

    def verify(self, plan_id: int) -> dict[str, Any]:
        return self.adapter.verify_plan(plan_id)

    def plan_checks(self, plan_id: int) -> dict[str, Any]:
        return self.adapter.plan_checks(plan_id)

    @staticmethod
    def action_creates_document(action_type: str) -> bool:
        return str(action_type) in CREATING_ACTIONS
