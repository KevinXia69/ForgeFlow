"""Deterministic conversion from a solved change set to approved Odoo actions.

The action list is not written by hand in an acceptance script or a service.
Each action names the change-set operation that produced it, the demand or
supply it serves (``origin_refs``) and the quantity, price and dates the solver
chose.  A plan whose projection checks failed, whose solver status is not a
solved status, or whose creating action has no origin cannot be compiled into an
executable plan; it remains analysis only.
"""
from __future__ import annotations

from typing import Any

#: Statuses that mean the solver actually produced a usable plan.
SOLVED_STATUSES = frozenset({"OPTIMAL", "FEASIBLE"})

#: Operations that only restate an existing commitment.
KEEP = "keep"


class ActionCompileError(ValueError):
    """A bounded, machine-readable refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def solver_status(plan: dict[str, Any] | None) -> str:
    """Read the solver's own status, not a label added by the caller."""
    plan = plan or {}
    calls = (plan.get("trace") or {}).get("solve_calls") or []
    for call in reversed(calls):
        status = str((call or {}).get("status") or "")
        if status:
            return status
    inner = plan.get("plan") or {}
    return str(inner.get("status") or plan.get("status") or "")


def compile_actions(changes: dict[str, Any], *, plan: dict[str, Any] | None,
                    solver_status_value: str | None = None,
                    currency: str = "USD", unit: str = "Units") -> dict[str, Any]:
    """Return ``actions`` plus the provenance that binds each one to the plan."""
    status = str(solver_status_value or solver_status(plan)).upper()
    if status not in SOLVED_STATUSES:
        # INFEASIBLE, UNKNOWN and MODEL_INVALID are different states; none of
        # them may become an approval.
        raise ActionCompileError("plan_status_not_executable")
    if str(changes.get("semantics") or "") != "target_state_to_change_set":
        raise ActionCompileError("change_set_semantics_unsupported")
    assumptions = [str(item) for item in ((plan or {}).get("assumptions") or [])]
    if assumptions:
        # An unconfirmed fact may be analysed, never approved and executed.
        raise ActionCompileError("plan_has_unconfirmed_assumptions")
    checks = ((changes.get("projected_state") or {}).get("checks") or {})
    failed = sorted(name for name, value in checks.items() if value is False)
    if failed:
        raise ActionCompileError("plan_check_failed")

    actions: list[dict[str, Any]] = []
    trace: dict[str, dict[str, Any]] = {}
    for operation in changes.get("operations") or []:
        if not isinstance(operation, dict) or operation.get("action") == KEEP:
            continue
        action = _compile_operation(operation, len(actions), currency=currency,
                                    unit=unit)
        if action["action_id"] in trace:
            raise ActionCompileError("action_id_duplicated")
        actions.append(action)
        trace[action["action_id"]] = _provenance(operation, action)
    if not actions:
        raise ActionCompileError("plan_has_no_actions")
    _check_actions(actions, trace)
    return {
        "actions": actions,
        "action_trace": trace,
        "solver": {"status": status},
        "checks": checks,
        "assumptions": assumptions,
    }


# --------------------------------------------------------------------- mapping
def _amount(value: Any, code: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ActionCompileError(code)
    return float(value)


def _operation_id(operation: dict[str, Any]) -> str:
    raw = str(operation.get("operation_id") or "").strip()
    if not raw:
        raise ActionCompileError("operation_id_required")
    return raw


def _action_id(operation: dict[str, Any]) -> str:
    return _operation_id(operation).replace(":", "-").replace("/", "-")


def _compile_operation(operation: dict[str, Any], sequence: int, *,
                       currency: str, unit: str) -> dict[str, Any]:
    kind = str(operation.get("kind") or "")
    action = str(operation.get("action") or "")
    target = operation.get("target") if isinstance(operation.get("target"), dict) else {}
    before = operation.get("before") if isinstance(operation.get("before"), dict) else {}
    after = operation.get("after") if isinstance(operation.get("after"), dict) else {}
    identifier = _action_id(operation)
    if kind == "purchase_order":
        return _purchase_action(identifier, action, target, before, after,
                                currency=currency, unit=unit)
    if kind == "manufacturing_order":
        return _manufacturing_action(identifier, action, target, before, after,
                                     currency=currency, unit=unit)
    raise ActionCompileError("operation_kind_unsupported")


def _purchase_action(identifier: str, action: str, target: dict, before: dict,
                     after: dict, *, currency: str, unit: str) -> dict[str, Any]:
    if action == "cancel":
        target_id = int(target.get("id") or 0)
        if target_id <= 0:
            raise ActionCompileError("operation_target_required")
        return {
            "action_id": identifier, "type": "cancel_purchase_order",
            "target_model": "purchase.order", "target_id": target_id,
            "params": {},
            "preconditions": [{"field": "state",
                               "in": ["draft", "purchase", "sent", "to approve"]}],
            "summary": f"Cancel {target.get('name') or target_id}",
        }
    if action == "update":
        values = after or before
        line_id = int((after or before).get("line_id") or 0)
        if line_id <= 0:
            raise ActionCompileError("operation_target_required")
        quantity = _amount(values.get("quantity"), "operation_quantity_required")
        params = {}
        if values.get("date_planned"):
            params["date_planned"] = values["date_planned"]
        preconditions = []
        previous = before.get("quantity")
        if isinstance(previous, (int, float)) and not isinstance(previous, bool):
            preconditions.append({"field": "product_qty", "eq": previous})
        return {
            "action_id": identifier, "type": "adjust_purchase_line",
            "target_model": "purchase.order.line", "target_id": line_id,
            "quantity": quantity, "unit": unit, "currency": currency,
            "params": params, "preconditions": preconditions,
            "summary": f"Adjust purchase line {line_id}",
        }
    if action == "create":
        quantity = _amount(after.get("quantity"), "operation_quantity_required")
        product_id = int(after.get("product_id") or 0)
        partner_id = int(after.get("partner_id") or 0)
        if product_id <= 0 or partner_id <= 0:
            raise ActionCompileError("operation_binding_missing")
        line = {"product_id": product_id, "product_qty": quantity,
                "price_unit": float(after.get("unit_price") or 0.0)}
        if after.get("date_planned"):
            line["date_planned"] = after["date_planned"]
        params = {"partner_id": partner_id, "lines": [line]}
        if after.get("date_planned"):
            params["date_planned"] = after["date_planned"]
        return {
            "action_id": identifier, "type": "create_purchase_order",
            "target_model": "purchase.order", "target_id": 0,
            "quantity": quantity, "unit": unit, "currency": currency,
            "params": params, "preconditions": [],
            "summary": f"Buy {quantity:g} of {after.get('product_code') or product_id}",
        }
    raise ActionCompileError("operation_action_unsupported")


def _manufacturing_action(identifier: str, action: str, target: dict, before: dict,
                          after: dict, *, currency: str, unit: str) -> dict[str, Any]:
    if action == "cancel":
        target_id = int(target.get("id") or 0)
        if target_id <= 0:
            raise ActionCompileError("operation_target_required")
        return {
            "action_id": identifier, "type": "cancel_manufacturing_order",
            "target_model": "mrp.production", "target_id": target_id,
            "params": {}, "preconditions": [{"field": "state",
                                             "in": ["draft", "confirmed", "progress",
                                                    "to_close", "planned"]}],
            "summary": f"Cancel {target.get('name') or target_id}",
        }
    if action == "update":
        values = after or before
        target_id = int(target.get("id") or 0)
        if target_id <= 0:
            raise ActionCompileError("operation_target_required")
        quantity = _amount(values.get("quantity"), "operation_quantity_required")
        params = {}
        if values.get("date_start"):
            params["date_start"] = values["date_start"]
        if values.get("date_deadline"):
            params["date_finished"] = values["date_deadline"]
        preconditions = []
        previous = before.get("quantity")
        if isinstance(previous, (int, float)) and not isinstance(previous, bool):
            preconditions.append({"field": "product_qty", "eq": previous})
        return {
            "action_id": identifier, "type": "adjust_manufacturing_order",
            "target_model": "mrp.production", "target_id": target_id,
            "quantity": quantity, "unit": unit, "currency": currency,
            "params": params, "preconditions": preconditions,
            "summary": f"Adjust manufacturing order {target_id}",
        }
    if action == "create":
        quantity = _amount(after.get("quantity"), "operation_quantity_required")
        product_id = int(after.get("product_id") or 0)
        if product_id <= 0:
            raise ActionCompileError("operation_binding_missing")
        params = {"product_id": product_id}
        if after.get("bom_id"):
            params["bom_id"] = int(after["bom_id"])
        if after.get("workcenter_id"):
            params["workcenter_id"] = int(after["workcenter_id"])
        if after.get("date_start"):
            params["date_start"] = after["date_start"]
        if after.get("date_deadline"):
            params["date_finished"] = after["date_deadline"]
        return {
            "action_id": identifier, "type": "create_manufacturing_order",
            "target_model": "mrp.production", "target_id": 0,
            "quantity": quantity, "unit": unit, "currency": currency,
            "params": params, "preconditions": [],
            "summary": (f"Make {quantity:g} of "
                        f"{after.get('product_code') or product_id}"),
        }
    raise ActionCompileError("operation_action_unsupported")


def _provenance(operation: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    before = operation.get("before") if isinstance(operation.get("before"), dict) else {}
    after = operation.get("after") if isinstance(operation.get("after"), dict) else {}
    origins = after.get("origin_refs") or before.get("origin_refs") or []
    return {
        "operation_id": _operation_id(operation),
        "kind": str(operation.get("kind") or ""),
        "action": str(operation.get("action") or ""),
        "origin_refs": [dict(item) for item in origins if isinstance(item, dict)],
        "before": before,
        "after": after,
        "action_id": action["action_id"],
    }


def _check_actions(actions: list[dict[str, Any]], trace: dict[str, dict[str, Any]]) -> None:
    seen: set[str] = set()
    for action in actions:
        identifier = str(action["action_id"])
        if identifier in seen:
            raise ActionCompileError("action_id_duplicated")
        seen.add(identifier)
        if action["type"] in {"create_purchase_order", "create_manufacturing_order"}:
            origins = trace[identifier]["origin_refs"]
            if not origins:
                # A created document with no demand or supply origin is a fixed
                # action dressed up as a plan.
                raise ActionCompileError("action_without_origin")
        if "quantity" in action:
            _amount(action["quantity"], "action_quantity_required")
