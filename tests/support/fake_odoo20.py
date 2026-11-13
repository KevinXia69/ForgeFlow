"""An in-memory Odoo 20 stand-in that enforces the addon's real contract.

This is deliberately not a mock: it re-uses the addon's pure decision logic
(``forgeflow_general/models/recovery_logic.py``) for the approval binding, action
validation, execution ordering, idempotency fingerprints and acceptance checks, so
a chain tested against it proves the *contract* rather than the test author's
assumptions.

It is labelled a fake wherever it is used.  Real Odoo 20 acceptance needs a real
instance; see ``docs/W9_ACCEPTANCE.md``.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ADDON = (Path(__file__).resolve().parents[2] / "odoo-addons" / "forgeflow_general"
          / "models" / "recovery_logic.py")
_spec = importlib.util.spec_from_file_location("fake_recovery_logic", _ADDON)
logic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logic)


class FakeOdooError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _moment(value) -> datetime | None:
    return logic.parse_moment(value)


def _match(record: dict, domain: list) -> bool:
    for item in domain:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            # A malformed clause is not "match everything".
            return False
        field, operator, expected = item
        value = record
        for part in str(field).split("."):
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, (list, tuple)) and len(value) == 2 and part == "id":
                value = value[0]
            elif isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
                value = [entry.get(part) for entry in value]
            else:
                value = None
            if value is None:
                break
        if isinstance(value, (list, tuple)) and len(value) == 2:
            value = value[0]
        if isinstance(value, dict):
            value = value.get("id")
        if operator == "=" and value != expected:
            return False
        if operator == "!=" and value == expected:
            return False
        if operator == "in" and value not in (expected or []):
            return False
        if operator == "not in" and value in (expected or []):
            return False
        if operator == "=ilike" and str(value or "").lower() != str(expected or "").lower():
            return False
        if operator == "ilike" and str(expected or "").lower() not in str(value or "").lower():
            return False
        if operator in {">", ">=", "<", "<="}:
            try:
                if operator == ">" and not value > expected:
                    return False
                if operator == ">=" and not value >= expected:
                    return False
                if operator == "<" and not value < expected:
                    return False
                if operator == "<=" and not value <= expected:
                    return False
            except TypeError:
                return False
        if operator not in {"=", "!=", "in", "not in", "=ilike", "ilike",
                            ">", ">=", "<", "<="}:
            return False
    return True


class FakeOdoo20:
    """Minimal but contract-faithful Odoo 20 recovery surface."""

    def __init__(self, *, environment: str = "env-test", database: str = "fake20") -> None:
        self.environment = environment
        self.database = database
        self.records: dict[str, dict[int, dict]] = {
            "purchase.order": {}, "purchase.order.line": {}, "sale.order": {},
            "sale.order.line": {}, "stock.quant": {}, "stock.move": {},
            "mrp.production": {}, "mrp.bom": {}, "mrp.bom.line": {},
            "product.product": {}, "res.partner": {},
            "forgeflow.exception_event": {}, "forgeflow.business_rule": {},
            "forgeflow.recovery_plan": {}, "forgeflow.decision_record": {},
            "forgeflow.execution_operation": {}, "forgeflow.execution_receipt": {},
            "forgeflow.event_outbox": {},
        }
        self._sequences: dict[str, int] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.fail_next_apply = False
        self.lose_apply_response = False
        self.writes = 0

    # -- infrastructure -----------------------------------------------------
    def version(self):
        class _Version:
            major = 20
            raw = "20.0"

        return _Version()

    def environment_generation(self):
        class _Generation:
            def __init__(self, generation, database, version, module_count):
                self.generation = generation
                self.database = database
                self.version = version
                self.module_count = module_count

            def __repr__(self) -> str:
                return f"EnvironmentGeneration(generation={self.generation!r})"

        digest = hashlib.sha256(f"{self.database}:{self.environment}".encode()).hexdigest()
        return _Generation(digest[:32], self.database, "20.0", 12)

    def _id(self, model: str) -> int:
        self._sequences[model] = self._sequences.get(model, 0) + 1
        return self._sequences[model]

    def create(self, model: str, values: dict[str, Any]) -> int:
        if model not in self.records:
            raise FakeOdooError(f"model_not_supported:{model}")
        record_id = self._id(model)
        record = {key: value for key, value in values.items()
                  if not isinstance(value, (list, tuple)) or not value}
        record["id"] = record_id
        record.setdefault("state", "draft")
        self.records[model][record_id] = record
        self.writes += 1
        return record_id

    def write(self, model: str, ids, values: dict[str, Any]) -> bool:
        for record_id in ids:
            record = self.records.get(model, {}).get(int(record_id))
            if record is None:
                raise FakeOdooError("record_not_found")
            record.update({key: value for key, value in values.items()
                           if not isinstance(value, (list, tuple)) or not value})
        self.writes += 1
        return True

    def search_read(self, model: str, domain, fields, limit: int = 100,
                    order: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        self.calls.append((model, tuple(map(tuple, domain or []))))
        rows = [record for record in self.records.get(model, {}).values()
                if _match(record, list(domain or []))]
        if order:
            key = order.split()[0].lstrip("-")
            rows.sort(key=lambda row: (row.get(key) is None, row.get(key)),
                      reverse=order.strip().startswith("-") or order.strip().endswith(" desc"))
        else:
            rows.sort(key=lambda row: row.get("id", 0))
        page = rows[offset:offset + max(1, min(int(limit), 500))]
        return [self._project(row, fields) for row in page]

    @staticmethod
    def _project(row: dict, fields) -> dict:
        wanted = list(fields or row.keys())
        return {name: copy.deepcopy(row.get(name)) for name in wanted
                if name in row or name == "id"}

    # -- seeds ---------------------------------------------------------------
    def seed_purchase_order(self, *, order_id: int, name: str, state: str = "purchase",
                            partner_id: int = 3, date_planned: str = "2026-11-10 10:00:00",
                            lines: list[dict] | None = None) -> int:
        self._sequences["purchase.order"] = max(self._sequences.get("purchase.order", 0),
                                                order_id)
        self.records["purchase.order"][order_id] = {
            "id": order_id, "name": name, "state": state,
            "partner_id": [partner_id, f"Supplier {partner_id}"],
            "date_planned": date_planned, "company_id": [1, "Company"],
            "amount_total": 1000.0,
        }
        for line in lines or []:
            line_id = self._id("purchase.order.line")
            self.records["purchase.order.line"][line_id] = {
                "id": line_id, "order_id": [order_id, name],
                "product_id": [line["product_id"], f"Product {line['product_id']}"],
                "product_qty": line.get("product_qty", 10.0),
                "qty_received": line.get("qty_received", 0.0),
                "date_planned": line.get("date_planned", date_planned),
                "price_unit": line.get("price_unit", 100.0),
            }
        return order_id

    def seed_product(self, product_id: int, code: str) -> None:
        self.records["product.product"][product_id] = {
            "id": product_id, "name": code, "default_code": code,
            "product_tmpl_id": [product_id, code], "uom_id": [1, "Units"]}

    def seed_sale_line(self, line_id: int, *, order_id: int, product_id: int,
                       quantity: float) -> None:
        self.records["sale.order"].setdefault(order_id, {
            "id": order_id, "name": f"SO{order_id}", "partner_id": [3, "Supplier 3"],
            "state": "sale", "commitment_date": "2026-11-20 10:00:00"})
        self.records["sale.order.line"][line_id] = {
            "id": line_id, "order_id": [order_id, f"SO{order_id}"],
            "product_id": [product_id, f"Product {product_id}"],
            "product_uom_qty": quantity}

    def seed_stock_move(self, move_id: int, *, product_id: int, quantity: float) -> None:
        self.records["stock.move"][move_id] = {
            "id": move_id, "product_id": [product_id, f"Product {product_id}"],
            "product_uom_qty": quantity, "state": "confirmed", "sale_line_id": False}

    def seed_workcenter(self, workcenter_id: int, name: str) -> None:
        self.records.setdefault("mrp.workcenter", {})[workcenter_id] = {
            "id": workcenter_id, "name": name, "resource_calendar_id": [1, "Standard"]}

    # -- plan surface --------------------------------------------------------
    @property
    def _plans(self) -> dict[int, dict]:
        return self.records["forgeflow.recovery_plan"]

    def prepare_plan(self, exception_id: int, actions: list[dict], fact_snapshot: dict, *,
                     environment_generation: str, rule_version: int = 1,
                     tenant_ref: str = "", name: str = "Recovery plan",
                     expires_in_hours: int = 72) -> int:
        try:
            validated = logic.execution_order(actions)
        except logic.PlanError as error:
            raise FakeOdooError(error.code) from None
        snapshot_hash = logic.digest(fact_snapshot or {})
        plan_hash = logic.digest({
            "actions": validated, "fact_snapshot_hash": snapshot_hash,
            "environment_generation": environment_generation,
            "rule_version": int(rule_version), "tenant_ref": tenant_ref})
        plan_id = self._id("forgeflow.recovery_plan")
        self._plans[plan_id] = {
            "id": plan_id, "name": name, "exception_id": [exception_id, "exception"],
            "company_id": [1, "Company"], "version": 1, "plan_hash": plan_hash,
            "action_digest": logic.action_digest(validated),
            "action_count": len(validated), "fact_snapshot_hash": snapshot_hash,
            "fact_snapshot_json": json.dumps(fact_snapshot or {}, ensure_ascii=False, default=str),
            "environment_generation": environment_generation, "tenant_ref": tenant_ref,
            "rule_version": int(rule_version), "state": "awaiting_approval",
            "payload_json": json.dumps({"actions": validated,
                                        "snapshot": fact_snapshot or {},
                                        "environment_generation": environment_generation},
                                       ensure_ascii=False, default=str),
            "expires_at": (datetime.now(UTC) + timedelta(hours=expires_in_hours)
                           ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._plans[plan_id]["plan_digest"] = self._plan_digest(plan_id)
        return plan_id

    def _plan_actions(self, plan_id: int) -> list[dict]:
        plan = self._plans[int(plan_id)]
        payload = json.loads(plan.get("payload_json") or "{}")
        return payload.get("actions") or []

    def _plan_digest(self, plan_id: int) -> str:
        plan = self._plans[int(plan_id)]
        return logic.plan_digest(
            tenant=plan.get("tenant_ref") or "",
            environment_generation=plan.get("environment_generation") or "",
            plan_id=int(plan_id), plan_version=int(plan["version"]),
            fact_snapshot_hash=plan["fact_snapshot_hash"],
            rule_version=int(plan["rule_version"]),
            actions=self._plan_actions(plan_id))

    def _approval(self, plan_id: int, expected_digest: str) -> dict:
        decisions = [row for row in self.records["forgeflow.decision_record"].values()
                     if row.get("plan_id") == int(plan_id)
                     and row.get("decision") == "approved"]
        decisions.sort(key=lambda row: row["id"], reverse=True)
        decision = decisions[0] if decisions else None
        record = None
        if decision:
            record = {"decision": decision["decision"],
                      "plan_hash": decision.get("plan_hash"),
                      "plan_version": decision.get("plan_version"),
                      "plan_digest": decision.get("plan_digest"),
                      "action_digest": decision.get("action_digest"),
                      "environment_generation": decision.get("environment_generation"),
                      "approver_id": decision.get("approver_id"),
                      "execution_identity": decision.get("execution_identity"),
                      "expires_at": decision.get("expires_at")}
        plan = self._plans[int(plan_id)]
        try:
            return logic.validate_approval(
                plan={"plan_hash": plan["plan_hash"], "version": plan["version"],
                      "environment_generation": plan.get("environment_generation")},
                decision=record, expected_digest=expected_digest,
                expected_action_digest=plan.get("action_digest"))
        except logic.PlanError as error:
            raise FakeOdooError(error.code) from None

    def approve_plan(self, plan_id: int, approver_id: int, execution_identity: str, *,
                     expires_in_hours: int = 24, comment: str = "",
                     expected_plan_version: int | None = None,
                     expected_plan_digest: str | None = None,
                     expected_action_digest: str | None = None) -> int:
        plan = self._plans[int(plan_id)]
        if plan["state"] not in ("awaiting_approval", "draft"):
            raise FakeOdooError("plan_not_awaiting_approval")
        digest = self._plan_digest(plan_id)
        if (expected_plan_version is not None and expected_plan_version != plan["version"]
                or expected_plan_digest is not None and expected_plan_digest != digest
                or expected_action_digest is not None
                and expected_action_digest != plan["action_digest"]):
            raise FakeOdooError("approval_plan_changed")
        decision_id = self._id("forgeflow.decision_record")
        self.records["forgeflow.decision_record"][decision_id] = {
            "id": decision_id, "plan_id": int(plan_id), "decision": "approved",
            "approver_id": int(approver_id), "plan_hash": plan["plan_hash"],
            "plan_version": plan["version"], "plan_digest": digest,
            "action_digest": plan["action_digest"],
            "environment_generation": plan.get("environment_generation"),
            "execution_identity": execution_identity, "revoked_at": False,
            "expires_at": (datetime.now(UTC) + timedelta(hours=expires_in_hours)
                           ).isoformat(),
            "comment": comment,
        }
        plan.update({"state": "approved", "plan_digest": digest})
        return decision_id

    def _fingerprint(self, plan_id: int, action: dict, execution_identity: str) -> str:
        plan = self._plans[int(plan_id)]
        return logic.idempotency_fingerprint(
            company_id=1, environment_generation=plan.get("environment_generation") or "",
            plan_id=int(plan_id), plan_version=int(plan["version"]),
            action_id=action["action_id"], params=action.get("params") or {},
            execution_identity=execution_identity)

    def apply_plan_action(self, plan_id: int, action_id: str, idempotency_key: str,
                          execution_identity: str | None = None) -> dict[str, Any]:
        plan = self._plans[int(plan_id)]
        actions = {action["action_id"]: action for action in self._plan_actions(plan_id)}
        if action_id not in actions:
            raise FakeOdooError("action_not_in_approved_plan")
        action = actions[action_id]
        digest = self._plan_digest(plan_id)
        approval = self._approval(plan_id, digest)
        if execution_identity and approval["execution_identity"] != execution_identity:
            raise FakeOdooError("execution_identity_mismatch")
        fingerprint = self._fingerprint(plan_id, action, approval["execution_identity"])
        execution_key = logic.plan_execution_key(
            plan_id=int(plan_id), plan_version=int(plan["version"]), plan_digest=digest,
            execution_identity=approval["execution_identity"],
            environment_generation=plan.get("environment_generation") or "")
        plan["execution_key"] = execution_key
        existing = [row for row in self.records["forgeflow.execution_operation"].values()
                    if row.get("idempotency_key") == idempotency_key]
        if existing:
            if existing[0].get("fingerprint") != fingerprint:
                raise FakeOdooError("idempotency_key_conflict")
            return self._receipt(existing[0])
        operation_id = self._id("forgeflow.execution_operation")
        operation = {
            "id": operation_id, "plan_id": int(plan_id),
            "plan_version": plan["version"], "plan_digest": digest,
            "action_id": action["action_id"], "operation_type": action["type"],
            "target_model": action["target_model"],
            "target_id": int(action["target_id"]),
            "request_hash": logic.digest(action), "fingerprint": fingerprint,
            "environment_generation": plan.get("environment_generation"),
            "execution_identity": approval["execution_identity"],
            "execution_key": execution_key,
            "idempotency_key": idempotency_key, "state": "dispatched",
            "company_id": [1, "Company"],
            "request_json": json.dumps(action, ensure_ascii=False, default=str),
        }
        self.records["forgeflow.execution_operation"][operation_id] = operation
        if self.fail_next_apply:
            self.fail_next_apply = False
            operation.update({"state": "failed",
                              "response_json": json.dumps({"error": "injected"})})
            raise FakeOdooError("injected_apply_failure")
        try:
            readback = self._run_action(action)
        except FakeOdooError as error:
            operation.update({"state": "failed",
                              "response_json": json.dumps({"error": str(error)})})
            self._receipt(operation, state="failed")
            raise
        operation.update({"state": "succeeded",
                          "response_json": json.dumps(readback, default=str)})
        receipt = self._receipt(operation)
        outbox_id = self._id("forgeflow.event_outbox")
        self.records["forgeflow.event_outbox"][outbox_id] = {
            "id": outbox_id,
            "event_type": "execution.applied", "state": "pending",
            "aggregate_model": "forgeflow.execution_operation",
            "aggregate_id": operation_id,
            "external_event_id": f"apply:{idempotency_key}",
            "company_id": [1, "Company"],
        }
        if self.lose_apply_response:
            self.lose_apply_response = False
            raise FakeOdooError("response_lost")
        return {"operation_id": operation_id, "state": "succeeded",
                "action_id": action["action_id"], "readback": readback,
                "receipt_id": receipt["receipt_id"]}

    def apply_plan(self, plan_id: int, idempotency_prefix: str,
                   execution_identity: str) -> dict[str, Any]:
        digest = self._plan_digest(plan_id)
        self._approval(plan_id, digest)
        self._plans[int(plan_id)]["state"] = "executing"
        results = []
        for action in self._plan_actions(plan_id):
            key = f"{idempotency_prefix}:{plan_id}:{self._plans[int(plan_id)]['version']}:" \
                  f"{action['action_id']}"
            result = self.apply_plan_action(plan_id, action["action_id"], key,
                                            execution_identity)
            results.append(result)
            if result["state"] not in ("succeeded", "verified"):
                self._plans[int(plan_id)]["state"] = "partially_executed"
                return {"plan_id": int(plan_id), "state": "partially_executed",
                        "actions": results}
        self._plans[int(plan_id)]["state"] = "executed"
        return {"plan_id": int(plan_id), "state": "executed", "actions": results}

    def _receipt(self, operation: dict, *, state: str | None = None) -> dict:
        receipts = [row for row in self.records["forgeflow.execution_receipt"].values()
                    if row.get("operation_id") == operation["id"]]
        if receipts:
            receipt = receipts[-1]
            return {"operation_id": operation["id"], "state": operation["state"],
                    "readback": json.loads(receipt.get("readback_json") or "{}"),
                    "receipt_id": receipt["id"]}
        receipt_id = self._id("forgeflow.execution_receipt")
        payload = operation.get("response_json") or "{}"
        self.records["forgeflow.execution_receipt"][receipt_id] = {
            "id": receipt_id, "operation_id": operation["id"],
            "company_id": [1, "Company"],
            "state": state or operation["state"],
            "readback_json": payload, "verified": False}
        return {"operation_id": operation["id"], "state": operation["state"],
                "readback": json.loads(payload), "receipt_id": receipt_id}

    def _run_action(self, action: dict) -> dict:
        kind = action["type"]
        handler = getattr(self, f"_action_{kind}", None)
        if handler is None:
            raise FakeOdooError("operation_not_supported")
        return handler(action)

    def _target(self, model: str, record_id: int) -> dict:
        record = self.records.get(model, {}).get(int(record_id))
        if record is None:
            raise FakeOdooError("target_not_found")
        return record

    def _action_preserve_supply(self, action: dict) -> dict:
        record = self._target(action["target_model"], action["target_id"])
        logic.check_preconditions(action, record)
        if record.get("state") not in ("purchase", "sent", "confirmed", "progress", "done"):
            raise FakeOdooError("target_not_preservable")
        return {"model": action["target_model"], "id": record["id"],
                "state": record["state"], "preserved": True}

    def _action_cancel_purchase_order(self, action: dict) -> dict:
        record = self._target("purchase.order", action["target_id"])
        logic.check_preconditions(action, record)
        if record["state"] not in ("purchase", "draft", "sent", "to approve"):
            raise FakeOdooError("target_not_cancellable")
        record["state"] = "cancel"
        return {"model": "purchase.order", "id": record["id"], "state": "cancel"}

    def _action_cancel_manufacturing_order(self, action: dict) -> dict:
        record = self._target("mrp.production", action["target_id"])
        logic.check_preconditions(action, record)
        if record["state"] in ("done", "cancel"):
            raise FakeOdooError("target_not_cancellable")
        record["state"] = "cancel"
        return {"model": "mrp.production", "id": record["id"], "state": "cancel"}

    def _action_adjust_purchase_line(self, action: dict) -> dict:
        record = self._target("purchase.order.line", action["target_id"])
        logic.check_preconditions(action, record)
        record["product_qty"] = action["quantity"]
        params = action.get("params") or {}
        if params.get("date_planned"):
            record["date_planned"] = params["date_planned"]
        return {"model": "purchase.order.line", "id": record["id"],
                "product_qty": record["product_qty"],
                "date_planned": record["date_planned"]}

    def _action_create_purchase_order(self, action: dict) -> dict:
        params = action.get("params") or {}
        if not params.get("partner_id") or not params.get("lines"):
            raise FakeOdooError("purchase_lines_required")
        order_id = self._id("purchase.order")
        self.records["purchase.order"][order_id] = {
            "id": order_id, "name": f"P{order_id:05d}", "state": "purchase",
            "partner_id": [int(params["partner_id"]), "Replacement supplier"],
            "date_planned": params.get("date_planned") or "2026-11-15 10:00:00",
            "company_id": [1, "Company"], "amount_total": 0.0}
        for line in params["lines"]:
            line_id = self._id("purchase.order.line")
            self.records["purchase.order.line"][line_id] = {
                "id": line_id, "order_id": [order_id, f"P{order_id:05d}"],
                "product_id": [line["product_id"], "Product"],
                "product_qty": line["product_qty"], "qty_received": 0.0,
                "date_planned": line.get("date_planned") or "2026-11-15 10:00:00",
                "price_unit": line.get("price_unit") or 0.0}
            self.records["purchase.order"][order_id]["amount_total"] += (
                float(line.get("price_unit") or 0.0) * float(line["product_qty"]))
        return {"model": "purchase.order", "id": order_id, "state": "purchase",
                "name": f"P{order_id:05d}"}

    def _action_create_manufacturing_order(self, action: dict) -> dict:
        params = action.get("params") or {}
        if not params.get("product_id"):
            raise FakeOdooError("product_required")
        order_id = self._id("mrp.production")
        self.records["mrp.production"][order_id] = {
            "id": order_id, "name": f"MO{order_id:05d}", "state": "confirmed",
            "product_id": [int(params["product_id"]), "Product"],
            "product_qty": action["quantity"], "bom_id": params.get("bom_id"),
            "date_start": params.get("date_start") or "2026-11-12 08:00:00",
            "date_finished": params.get("date_finished") or "2026-11-14 08:00:00",
            "company_id": [1, "Company"]}
        return {"model": "mrp.production", "id": order_id, "state": "confirmed",
                "name": f"MO{order_id:05d}", "product_qty": action["quantity"]}

    def _action_adjust_manufacturing_order(self, action: dict) -> dict:
        record = self._target("mrp.production", action["target_id"])
        logic.check_preconditions(action, record)
        record["product_qty"] = action["quantity"]
        params = action.get("params") or {}
        for key in ("date_start", "date_finished"):
            if params.get(key):
                record[key] = params[key]
        return {"model": "mrp.production", "id": record["id"],
                "product_qty": record["product_qty"],
                "date_start": record.get("date_start"),
                "date_finished": record.get("date_finished")}

    def _action_link_supply_to_demand(self, action: dict) -> dict:
        params = action.get("params") or {}
        sale_line = self._target("sale.order.line", int(params.get("sale_line_id") or 0))
        record = self._target(action["target_model"], action["target_id"])
        record["sale_line_id"] = [sale_line["id"], "Sales line"]
        return {"model": action["target_model"], "id": record["id"],
                "sale_line_id": sale_line["id"],
                "sale_order": (sale_line.get("order_id") or [None, None])[0]}

    def _action_reserve_stock(self, action: dict) -> dict:
        params = action.get("params") or {}
        if params.get("move_id"):
            move = self._target("stock.move", int(params["move_id"]))
            move["state"] = "assigned"
            return {"model": "stock.move", "id": move["id"], "state": move["state"]}
        raise FakeOdooError("stock_unavailable")

    # -- readback / verify ---------------------------------------------------
    def readback_plan(self, plan_id: int) -> dict[str, Any]:
        actions = self._plan_actions(plan_id)
        result: dict[str, Any] = {}
        for target in logic.readback_targets(actions):
            if target["kind"] == "created":
                operations = [row for row in
                              self.records["forgeflow.execution_operation"].values()
                              if row.get("plan_id") == int(plan_id)
                              and row.get("action_id") == target["action_id"]
                              and row.get("state") == "succeeded"]
                if not operations:
                    continue
                response = json.loads(operations[0].get("response_json") or "{}")
                created_id = response.get("id")
                record = self.records[target["model"]].get(int(created_id)) if created_id else None
                if record is None:
                    continue
                result[f"{target['model']}:new:{target['action_id']}"] = {
                    name: record.get(name) for name in target["fields"]} | {"id": record["id"]}
                continue
            record = self.records[target["model"]].get(int(target["id"]))
            result[f"{target['model']}:{target['id']}"] = (
                {name: record.get(name) for name in target["fields"]} | {"id": record["id"]}
                if record else {"id": None, "missing": True})
        return result

    def verify_plan(self, plan_id: int) -> dict[str, Any]:
        actions = self._plan_actions(plan_id)
        readback = self.readback_plan(plan_id)
        checks = logic.business_acceptance(actions, readback)
        return {"plan_id": int(plan_id), "status": logic.acceptance_status(checks),
                "checks": checks}

    def plan_checks(self, plan_id: int) -> dict[str, Any]:
        plan = self._plans[int(plan_id)]
        payload = json.loads(plan.get("payload_json") or "{}")
        facts = payload.get("snapshot") or {}
        checks = logic.plan_checks(actions=self._plan_actions(plan_id), facts=facts)
        return {"plan_id": int(plan_id), "status": logic.acceptance_status(checks),
                "checks": checks}

    def mark_operation_unknown(self, plan_id: int, idempotency_key: str,
                               reason: str = "") -> dict[str, Any]:
        operations = [row for row in self.records["forgeflow.execution_operation"].values()
                      if row.get("idempotency_key") == idempotency_key
                      and row.get("plan_id") == int(plan_id)]
        if not operations:
            raise FakeOdooError("operation_not_found")
        operations[0].update({"state": "unknown", "reconcile_state": "unknown"})
        return {"operation_id": operations[0]["id"], "state": "unknown"}

    def resolve_unknown_operation(self, plan_id: int, idempotency_key: str,
                                  confirmed_applied: bool) -> dict[str, Any]:
        operations = [row for row in self.records["forgeflow.execution_operation"].values()
                      if row.get("idempotency_key") == idempotency_key
                      and row.get("plan_id") == int(plan_id)]
        if not operations:
            raise FakeOdooError("operation_not_found")
        operations[0].update({
            "state": "succeeded" if confirmed_applied else "failed",
            "reconcile_state": "confirmed_applied" if confirmed_applied
            else "confirmed_not_applied"})
        return {"operation_id": operations[0]["id"], "state": operations[0]["state"]}

    # -- legacy single-op surface used by the compatibility path -------------
    def apply_approved_plan(self, plan_id: int, idempotency_key: str,
                            operation_type: str, target_model: str,
                            target_id: int, payload: dict) -> dict[str, Any]:
        for action in self._plan_actions(plan_id):
            if (action["type"] == operation_type
                    and action["target_model"] == target_model
                    and int(action["target_id"]) == int(target_id)):
                return self.apply_plan_action(plan_id, action["action_id"],
                                              idempotency_key)
        raise FakeOdooError("target_not_in_approved_plan")

    def reconcile_plan(self, plan_id: int, idempotency_key: str) -> dict[str, Any]:
        operations = [row for row in self.records["forgeflow.execution_operation"].values()
                      if row.get("idempotency_key") == idempotency_key
                      and row.get("plan_id") == int(plan_id)]
        if not operations:
            raise FakeOdooError("operation_not_found")
        return self._receipt(operations[0])
