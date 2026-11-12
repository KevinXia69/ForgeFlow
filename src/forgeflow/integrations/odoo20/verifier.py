"""Independent business verification over authoritative Odoo 20 records.

Verification has three layers and they are not interchangeable:

1. **plan check** -- does the plan satisfy the formal rules and the fact snapshot;
2. **readback** -- did the ERP actually change in the way the approved actions say;
3. **business acceptance** -- does the resulting business state meet the recovery
   goal.

The verifier reads Odoo itself.  It never trusts the executor's ``succeeded``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .adapter import Odoo20Adapter
from .contracts import CREATING_ACTIONS


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    code: str
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: str
    checks: tuple[VerificationCheck, ...] = field(default_factory=tuple)
    layers: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == "verified"


def _id_of(value: Any) -> int:
    """Read a numeric id out of an Odoo Many2one pair or a scalar."""
    if isinstance(value, (list, tuple)):
        return int(value[0]) if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _combine(checks: list[VerificationCheck]) -> str:
    if any(check.status == "failed" for check in checks):
        return "failed"
    if any(check.status in {"unknown", "pending"} for check in checks):
        return "unknown"
    return "verified"


class Odoo20BusinessVerifier:
    """Reads the plan, the approval, the operations and the affected records."""

    def __init__(self, adapter: Odoo20Adapter) -> None:
        self.adapter = adapter

    # -- plan ---------------------------------------------------------------
    def plan_checks(self, plan_id: int) -> tuple[list[VerificationCheck], dict[str, Any]]:
        plan = self._plan(plan_id)
        checks = [VerificationCheck(
            "plan_present", "pass" if plan else "failed",
            None if plan else "recovery plan not found")]
        if not plan:
            return checks, {}
        try:
            payload = self.adapter.plan_checks(plan_id)
        except Exception:  # noqa: BLE001 - reported as an unknown layer, not success
            checks.append(VerificationCheck("plan_checks_available", "unknown",
                                            "addon plan check unavailable"))
            return checks, {}
        for item in payload.get("checks") or []:
            checks.append(VerificationCheck(str(item.get("code")),
                                            str(item.get("status")),
                                            item.get("detail")))
        return checks, payload

    # -- readback -----------------------------------------------------------
    def readback(self, plan_id: int) -> tuple[list[VerificationCheck], dict[str, Any]]:
        plan = self._plan(plan_id)
        checks: list[VerificationCheck] = []
        if not plan:
            return [VerificationCheck("plan_present", "failed", "plan not found")], {}
        actions = self._actions(plan)
        operations = self.adapter.search_read(
            "forgeflow.execution_operation", [("plan_id", "=", int(plan_id))],
            ["id", "action_id", "state", "fingerprint", "target_model", "target_id",
             "response_json"],
            limit=200)
        by_action = {str(row.get("action_id")): row for row in operations}
        for action in actions:
            action_id = str(action.get("action_id"))
            operation = by_action.get(action_id)
            if operation is None:
                checks.append(VerificationCheck(
                    f"{action_id}:operation_present", "pending",
                    "action not executed"))
                continue
            checks.append(VerificationCheck(
                f"{action_id}:operation_state",
                "pass" if str(operation.get("state")) in {"succeeded", "verified"}
                else ("unknown" if str(operation.get("state")) in {"unknown", "dispatched"}
                      else "failed"),
                str(operation.get("state"))))
        # Independent record readback: do not rely on the executor's response.
        for action in actions:
            action_id = str(action.get("action_id"))
            action_type = str(action.get("type"))
            model = str(action.get("target_model"))
            target_id = int(action.get("target_id") or 0)
            operation = by_action.get(action_id) or {}
            if target_id <= 0 and action.get("target_ref"):
                # A target created by an earlier action has no id in the approved
                # payload; the executor recorded the resolved id on the operation.
                target_id = int(operation.get("target_id") or 0)
            fields = ["id", "state"]
            if action_type in CREATING_ACTIONS:
                created = _loads(operation.get("response_json"))
                created_id = created.get("id") if isinstance(created, dict) else None
                if not created_id:
                    checks.append(VerificationCheck(
                        f"{action_id}:created_present", "unknown",
                        "created document id not recorded"))
                    continue
                rows = self.adapter.search_read(model, [("id", "=", int(created_id))],
                                                ["id", "state"], limit=1)
                checks.append(VerificationCheck(
                    f"{action_id}:created_present", "pass" if rows else "failed",
                    None if rows else "created document missing"))
                continue
            if target_id <= 0:
                checks.append(VerificationCheck(f"{action_id}:target", "failed",
                                                "no target id"))
                continue
            rows = self.adapter.search_read(model, [("id", "=", target_id)], fields, limit=1)
            record = rows[0] if rows else None
            checks.append(VerificationCheck(
                f"{action_id}:record_present", "pass" if record else "failed",
                None if record else "target record missing"))
            if record and action_type in {"cancel_purchase_order",
                                          "cancel_manufacturing_order"}:
                checks.append(VerificationCheck(
                    f"{action_id}:cancelled",
                    "pass" if str(record.get("state")) in {"cancel", "cancelled"}
                    else "failed", str(record.get("state"))))
        receipts = self.adapter.search_read(
            "forgeflow.execution_receipt", [("state", "!=", False)],
            ["id", "operation_id", "state", "verified"], limit=200) if operations else []
        operation_ids = {int(row["id"]) for row in operations}
        relevant = [row for row in receipts
                    if _id_of(row.get("operation_id")) in operation_ids]
        checks.append(VerificationCheck(
            "receipts_present",
            "pass" if operations and len(relevant) >= len(operations) else "unknown",
            f"{len(relevant)}/{len(operations)}"))
        return checks, {"operations": operations, "receipts": relevant}

    # -- business acceptance ------------------------------------------------
    def business_acceptance(self, plan_id: int) -> tuple[list[VerificationCheck], dict[str, Any]]:
        plan = self._plan(plan_id)
        if not plan:
            return [VerificationCheck("plan_present", "failed", "plan not found")], {}
        checks: list[VerificationCheck] = []
        try:
            payload = self.adapter.verify_plan(plan_id)
        except Exception:  # noqa: BLE001
            return [VerificationCheck("business_acceptance_available", "unknown",
                                      "addon acceptance check unavailable")], {}
        for item in payload.get("checks") or []:
            checks.append(VerificationCheck(str(item.get("code")),
                                            str(item.get("status")), item.get("detail")))
        checks.append(VerificationCheck("business_status", str(payload.get("status")),
                                        None))
        return checks, payload

    # -- composed -----------------------------------------------------------
    def verify_plan(self, plan_id: int) -> VerificationResult:
        plan_layer, plan_detail = self.plan_checks(plan_id)
        readback_layer, readback_detail = self.readback(plan_id)
        business_layer, business_detail = self.business_acceptance(plan_id)
        checks = [*plan_layer, *readback_layer, *business_layer]
        plan_status = _combine(plan_layer)
        readback_status = _combine(readback_layer)
        business_status = _combine([check for check in business_layer
                                    if check.code != "business_status"])
        # The overall status is the worst layer: a plan-level pass cannot rescue
        # an ERP read-back that never happened.
        order = {"failed": 3, "unknown": 2, "pending": 2, "pass": 1, "verified": 0}
        worst = max([plan_status, readback_status, business_status],
                    key=lambda value: order.get(value, 2))
        status = "failed" if worst == "failed" else (
            "unknown" if worst in {"unknown", "pending"} else "verified")
        return VerificationResult(
            status, tuple(checks),
            {"plan": plan_status, "readback": readback_status,
             "business": business_status, "plan_detail": plan_detail,
             "readback_detail": readback_detail, "business_detail": business_detail})

    # -- legacy single-operation verification -------------------------------
    def verify_purchase_cancel(self, plan_id: int, idempotency_key: str) -> VerificationResult:
        checks: list[VerificationCheck] = []
        operations = self.adapter.search_read(
            "forgeflow.execution_operation",
            [("plan_id", "=", int(plan_id)), ("idempotency_key", "=", idempotency_key)],
            ["id", "state", "target_model", "target_id"], limit=2)
        checks.append(VerificationCheck(
            "operation_present", "pass" if len(operations) == 1 else "failed",
            None if len(operations) == 1 else "expected exactly one operation"))
        if not operations:
            return VerificationResult("unknown", tuple(checks))
        operation = operations[0]
        checks.append(VerificationCheck(
            "operation_succeeded", "pass" if operation.get("state") in {
                "succeeded", "verified"} else "failed", str(operation.get("state"))))
        receipts = self.adapter.search_read(
            "forgeflow.execution_receipt", [("operation_id", "=", operation["id"])],
            ["id", "state", "readback_json", "verified"], limit=2)
        checks.append(VerificationCheck(
            "receipt_present", "pass" if len(receipts) == 1 else "unknown",
            None if len(receipts) == 1 else "receipt unavailable"))
        if len(receipts) == 1:
            receipt = receipts[0]
            readback = _loads(receipt.get("readback_json")) or {}
            checks.append(VerificationCheck(
                "receipt_state", "pass" if receipt.get("state") in {
                    "succeeded", "verified"} else "failed", str(receipt.get("state"))))
            checks.append(VerificationCheck(
                "readback_cancel", "pass" if readback.get("state") == "cancel"
                else "failed", str(readback.get("state"))))
        target_id = operation.get("target_id")
        if not isinstance(target_id, int):
            checks.append(VerificationCheck("target_present", "unknown",
                                            "target id missing"))
        else:
            target = self.adapter.search_read("purchase.order", [("id", "=", target_id)],
                                              ["id", "state"], limit=1)
            native_state = target[0].get("state") if target else None
            checks.append(VerificationCheck(
                "native_cancelled", "pass" if native_state == "cancel" else "failed",
                str(native_state)))
        return VerificationResult(_combine(checks), tuple(checks))

    # -- helpers ------------------------------------------------------------
    def _plan(self, plan_id: int) -> dict[str, Any] | None:
        rows = self.adapter.search_read(
            "forgeflow.recovery_plan", [("id", "=", int(plan_id))],
            ["id", "name", "state", "plan_hash", "action_digest", "action_count",
             "version", "payload_json", "environment_generation", "fact_snapshot_hash"],
            limit=1)
        return rows[0] if rows else None

    @staticmethod
    def _actions(plan: dict[str, Any]) -> list[dict[str, Any]]:
        payload = _loads(plan.get("payload_json")) or {}
        actions = payload.get("actions") if isinstance(payload, dict) else None
        return [action for action in (actions or []) if isinstance(action, dict)]
