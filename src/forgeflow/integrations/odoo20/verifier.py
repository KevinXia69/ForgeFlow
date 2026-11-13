"""Independent business verification over authoritative Odoo 20 records.

Verification has three layers and they are not interchangeable:

1. **plan check** -- does the plan satisfy the formal rules and the fact snapshot;
2. **readback** -- did the ERP actually change in the way the approved actions say;
3. **business acceptance** -- does the resulting business state meet the recovery
   goal.

The verifier reads Odoo itself.  It never trusts the executor's ``succeeded``.
"""
from __future__ import annotations

import hashlib
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


class _ReadbackIncomplete(ValueError):
    pass


def _id_of(value: Any) -> int:
    """Read a numeric id out of an Odoo Many2one pair or a scalar."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else 0
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
    if any(check.status not in {"pass", "verified"} for check in checks):
        return "unknown"
    return "verified"


def _digest(value: Any) -> str:
    """The addon's canonical digest format, independently recomputed on readback."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Odoo20BusinessVerifier:
    """Reads the plan, the approval, the operations and the affected records."""

    def __init__(self, adapter: Odoo20Adapter) -> None:
        self.adapter = adapter

    PAGE_SIZE = 200
    MAX_RECORDS = 10_000

    # -- plan ---------------------------------------------------------------
    def plan_checks(self, plan_id: int) -> tuple[list[VerificationCheck], dict[str, Any]]:
        try:
            plan = self._plan(plan_id)
        except Exception:  # noqa: BLE001
            return [VerificationCheck("plan_present", "unknown", "plan query unavailable")], {}
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
        try:
            return self._readback(plan_id)
        except _ReadbackIncomplete as error:
            return [VerificationCheck(str(error), "unknown", "reconcile required")], {}
        except Exception:  # noqa: BLE001 - incomplete queries are never successful evidence
            return [VerificationCheck("readback_available", "unknown",
                                      "readback query incomplete; reconcile required")], {}

    def _readback(self, plan_id: int) -> tuple[list[VerificationCheck], dict[str, Any]]:
        plan = self._plan(plan_id)
        checks: list[VerificationCheck] = []
        if not plan:
            return [VerificationCheck("plan_present", "failed", "plan not found")], {}
        actions = self._actions(plan)
        action_ids = [str(action.get("action_id") or "") for action in actions]
        if len(actions) > self.MAX_RECORDS:
            raise _ReadbackIncomplete("readback_supported_limit_exceeded")
        if (not actions or len(actions) != _id_of(plan.get("action_count"))
                or len(set(action_ids)) != len(actions) or not all(action_ids)):
            return [VerificationCheck("action_set_complete", "unknown",
                                      "plan action set is incomplete or ambiguous")], {}
        if not plan.get("execution_key"):
            return [VerificationCheck("execution_binding", "unknown",
                                      "plan has no recorded execution attempt")], {}
        operations = self._all_records(
            "forgeflow.execution_operation",
            [("plan_id", "=", int(plan_id)),
             ("execution_key", "=", plan["execution_key"])],
            ["id", "plan_id", "company_id", "plan_version", "plan_digest",
             "execution_key", "execution_identity", "environment_generation",
             "action_id", "operation_type", "state", "fingerprint", "request_hash", "target_model",
             "target_id", "response_json"])
        by_action: dict[str, dict[str, Any]] = {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in operations:
            grouped.setdefault(str(row.get("action_id")), []).append(row)
        for action_id, rows in grouped.items():
            if action_id not in action_ids or len(rows) != 1:
                checks.append(VerificationCheck("operation_set_complete", "unknown",
                                                "unexpected or ambiguous operation"))
            else:
                by_action[action_id] = rows[0]
        for action in actions:
            action_id = str(action.get("action_id"))
            operation = by_action.get(action_id)
            if operation is None:
                checks.append(VerificationCheck(
                    f"{action_id}:operation_present", "pending",
                    "action not executed"))
                continue
            binding = self._operation_bound(plan, action, operation, by_action)
            checks.append(VerificationCheck(
                f"{action_id}:operation_binding", "pass" if binding else "unknown",
                None if binding else "operation does not match the plan execution"))
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
            fields = ["id", "state"] if model in {
                "purchase.order", "mrp.production", "stock.move"} else ["id"]
            if action_type in CREATING_ACTIONS or action_type == "reserve_stock":
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
        # Query only this attempt's operation IDs. A plan-level verification
        # receipt (checks + readback) is an audit record, not execution evidence
        # for whichever single operation the addon attaches it to.
        receipts = []
        operation_ids = [int(row["id"]) for row in operations]
        for start in range(0, len(operation_ids), self.PAGE_SIZE):
            receipts.extend(self._all_records(
                "forgeflow.execution_receipt",
                [("operation_id", "in", operation_ids[start:start + self.PAGE_SIZE])],
                ["id", "operation_id", "company_id", "state", "verified", "readback_json"]))
            if len(receipts) > self.MAX_RECORDS:
                raise _ReadbackIncomplete("readback_supported_limit_exceeded")
        receipt_checks = self._receipt_checks(operations, receipts)
        checks.extend(receipt_checks)
        complete = bool(operations) and len(by_action) == len(actions) and all(
            check.status == "pass" for check in checks
            if check.code.endswith((":receipt", ":operation_binding", ":operation_state"))
            or check.code in {"receipt_scope", "operation_set_complete"})
        checks.append(VerificationCheck(
            "receipts_present", "pass" if complete else "unknown",
            f"{sum(check.status == 'pass' for check in receipt_checks)}/{len(actions)} operations"))
        latest = self._plan(plan_id)
        stable_fields = ("version", "plan_hash", "plan_digest", "action_digest",
                         "payload_json", "execution_key", "company_id", "action_count",
                         "environment_generation", "fact_snapshot_hash")
        stable = latest is not None and all(
            latest.get(name) == plan.get(name) for name in stable_fields)
        checks.append(VerificationCheck("plan_readback_stable", "pass" if stable else "unknown",
                                        None if stable else "plan changed during readback"))
        return checks, {"operations": operations, "receipts": receipts}

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
        plan_status = _combine(plan_layer)
        readback_status = _combine(readback_layer)
        # forgeflow_verify writes the plan/exception terminal status. Missing
        # execution evidence must not be followed by that success transition.
        if plan_status == readback_status == "verified":
            business_layer, business_detail = self.business_acceptance(plan_id)
        else:
            business_layer = [VerificationCheck(
                "business_acceptance_deferred", "unknown", "reconcile readback evidence first")]
            business_detail = {}
        checks = [*plan_layer, *readback_layer, *business_layer]
        business_status = _combine(business_layer)
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
        try:
            return self._verify_purchase_cancel(plan_id, idempotency_key)
        except Exception:  # noqa: BLE001
            return VerificationResult("unknown", (VerificationCheck(
                "readback_available", "unknown", "readback query incomplete; reconcile required"),))

    def _verify_purchase_cancel(self, plan_id: int, idempotency_key: str) -> VerificationResult:
        checks, _ = self.readback(plan_id)
        if _combine(checks) != "verified":
            return VerificationResult(_combine(checks), tuple(checks))
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
        receipts = self._all_records(
            "forgeflow.execution_receipt", [("operation_id", "=", operation["id"])],
            ["id", "state", "readback_json", "verified"])
        receipts = [row for row in receipts if not (
            isinstance(_loads(row.get("readback_json")), dict)
            and "checks" in _loads(row.get("readback_json"))
            and "readback" in _loads(row.get("readback_json")))]
        # Execution history is append-only; a later verification receipt is not
        # a duplicate execution, and a valid retry may have several receipts.
        receipts = sorted(receipts, key=lambda row: _id_of(row.get("id")), reverse=True)[:1]
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
    def _all_records(self, model: str, domain: list, fields: list[str]) -> list[dict[str, Any]]:
        """Keyset pagination with an explicit, fail-closed supported bound.

        Query until an empty page, not merely a short page: an intermediary may
        apply a smaller page size. IDs must advance or the query is incomplete.
        """
        rows: list[dict[str, Any]] = []
        last_id = 0
        while True:
            page = self.adapter.search_read(
                model, [*domain, ("id", ">", last_id)], fields,
                limit=self.PAGE_SIZE, order="id asc")
            if not page:
                return rows
            ids = [_id_of(row.get("id")) for row in page]
            if len(rows) + len(page) > self.MAX_RECORDS:
                raise _ReadbackIncomplete("readback_supported_limit_exceeded")
            if ids != sorted(set(ids)) or ids[0] <= last_id:
                raise _ReadbackIncomplete("readback_query_incomplete")
            rows.extend(page)
            last_id = ids[-1]

    @staticmethod
    def _operation_bound(plan: dict, action: dict, operation: dict,
                         by_action: dict[str, dict]) -> bool:
        """Bind a receipt's parent to the exact plan, action and execution identity."""
        company = _id_of(plan.get("company_id"))
        identity = operation.get("execution_identity")
        key_payload = {
            "plan_id": _id_of(plan.get("id")), "plan_version": _id_of(plan.get("version")),
            "plan_digest": plan.get("plan_digest"), "execution_identity": identity,
            "environment_generation": plan.get("environment_generation") or "",
        }
        fingerprint = _digest({
            "company_id": company,
            "environment_generation": plan.get("environment_generation") or "",
            "plan_id": _id_of(plan.get("id")), "plan_version": _id_of(plan.get("version")),
            "action_id": action.get("action_id"), "params": action.get("params") or {},
            "execution_identity": identity,
        })
        target = _id_of(action.get("target_id"))
        if action.get("target_ref"):
            reference = action["target_ref"]
            producer = by_action.get(str(reference.get("action_id"))) or {}
            response = _loads(producer.get("response_json"))
            if not isinstance(response, dict):
                return False
            target = _id_of(response.get(str(reference.get("field"))))
            if target <= 0:
                return False
        return bool(company and identity and plan.get("plan_digest")
                    and _id_of(operation.get("plan_id")) == _id_of(plan.get("id"))
                    and _id_of(operation.get("company_id")) == company
                    and _id_of(operation.get("plan_version")) == _id_of(plan.get("version"))
                    and operation.get("plan_digest") == plan.get("plan_digest")
                    and operation.get("environment_generation") == plan.get("environment_generation")
                    and operation.get("execution_key") == plan.get("execution_key") == _digest(key_payload)
                    and operation.get("fingerprint") == fingerprint
                    and operation.get("request_hash") == _digest(action)
                    and operation.get("operation_type") == action.get("type")
                    and operation.get("target_model") == action.get("target_model")
                    and _id_of(operation.get("target_id")) == target)

    @staticmethod
    def _receipt_checks(operations: list[dict], receipts: list[dict]) -> list[VerificationCheck]:
        """Latest execution receipt per operation; keep all audit history intact.

        `succeeded` is execution evidence and need not already be verified.
        `verified` requires its verified flag. Aggregate verification records are
        not execution receipts. Earlier failures may be superseded by a later
        successful execution receipt, while a latest failed/unknown stays unknown.
        """
        grouped: dict[int, list[dict]] = {}
        expected_ids = {_id_of(operation.get("id")) for operation in operations}
        checks = []
        for receipt in receipts:
            operation_id = _id_of(receipt.get("operation_id"))
            if operation_id not in expected_ids:
                checks.append(VerificationCheck("receipt_scope", "unknown",
                                                "query returned an unrelated receipt"))
                continue
            payload = _loads(receipt.get("readback_json"))
            if isinstance(payload, dict) and "checks" in payload and "readback" in payload:
                continue
            grouped.setdefault(operation_id, []).append(receipt)
        for operation in operations:
            candidates = grouped.get(_id_of(operation.get("id"))) or []
            receipt = max(candidates, key=lambda row: _id_of(row.get("id"))) if candidates else {}
            payload = _loads(receipt.get("readback_json"))
            response = _loads(operation.get("response_json"))
            expected_id = (_id_of(response.get("id")) if isinstance(response, dict) else 0)
            if not expected_id:
                expected_id = _id_of(operation.get("target_id"))
            valid = (bool(receipt) and expected_id > 0 and isinstance(payload, dict)
                     and _id_of(receipt.get("company_id")) == _id_of(operation.get("company_id"))
                     and (receipt.get("state") == "succeeded" or (
                         receipt.get("state") == "verified" and receipt.get("verified") is True))
                     and _id_of(payload.get("id")) == expected_id
                     and payload.get("model") == operation.get("target_model"))
            checks.append(VerificationCheck(
                f"{operation.get('action_id')}:receipt", "pass" if valid else "unknown",
                None if valid else "execution receipt missing, unresolved or mismatched"))
        return checks

    def _plan(self, plan_id: int) -> dict[str, Any] | None:
        rows = self.adapter.search_read(
            "forgeflow.recovery_plan", [("id", "=", int(plan_id))],
            ["id", "name", "state", "plan_hash", "plan_digest", "action_digest", "action_count",
             "version", "payload_json", "environment_generation", "fact_snapshot_hash",
             "company_id", "execution_key"],
            limit=1)
        return rows[0] if rows else None

    @staticmethod
    def _actions(plan: dict[str, Any]) -> list[dict[str, Any]]:
        payload = _loads(plan.get("payload_json")) or {}
        actions = payload.get("actions") if isinstance(payload, dict) else None
        return [action for action in (actions or []) if isinstance(action, dict)]
