"""Journal adapter for fixed compiled planning-document steps.

No credentials or implicit connection creation. Whole-scope validation is an
injected REQUIRED guard; this module cannot establish that a plan is feasible.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from forgeflow.recovery.odoo import PlanDocuments


class ReadbackError(ValueError):
    pass


FIELDS = {
    "mrp.production": [
        "id",
        "name",
        "state",
        "company_id",
        "product_id",
        "product_uom_id",
        "bom_id",
        "product_qty",
        "qty_produced",
        "origin",
        "date_start",
        "date_deadline",
        "date_finished",
        "workorder_ids",
        "move_raw_ids",
        "move_finished_ids",
    ],
    "mrp.workorder": [
        "id",
        "name",
        "state",
        "production_id",
        "workcenter_id",
        "product_id",
        "operation_id",
        "date_start",
        "date_finished",
        "duration_expected",
    ],
    "purchase.order": [
        "id",
        "name",
        "state",
        "company_id",
        "currency_id",
        "partner_id",
        "origin",
        "order_line",
    ],
    "purchase.order.line": [
        "id",
        "order_id",
        "product_id",
        "product_uom_id",
        "product_qty",
        "qty_received",
        "price_unit",
        "date_planned",
        "display_type",
    ],
}
RELATIONS = {
    "company_id",
    "product_id",
    "product_uom_id",
    "bom_id",
    "production_id",
    "workcenter_id",
    "currency_id",
    "partner_id",
    "order_id",
    "operation_id",
}


def complete_workorder(row):
    if not isinstance(row, dict) or not set(FIELDS["mrp.workorder"]) <= row.keys():
        return False
    if not all(
        type(row[key]) is int and row[key] > 0
        for key in ("id", "production_id", "product_id", "operation_id", "workcenter_id")
    ):
        return False
    if row["state"] not in ("pending", "waiting", "ready") or not isinstance(row["name"], str):
        return False
    duration = row["duration_expected"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        return False
    for field in ("date_start", "date_finished"):
        if row[field] is False:
            continue
        try:
            parsed = datetime.strptime(row[field], "%Y-%m-%d %H:%M:%S")
            if parsed.strftime("%Y-%m-%d %H:%M:%S") != row[field]:
                return False
        except (ValueError, TypeError):
            return False
    return True


def matches(actual, expected):
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    for field, value in expected.items():
        if field not in actual:
            return False
        observed = actual[field]
        if isinstance(value, dict):
            if not matches(observed, value):
                return False
        elif field == "origin":
            if not isinstance(observed, str) or sorted(
                x.strip() for x in observed.split(",") if x.strip()
            ) != sorted(x.strip() for x in value.split(",") if x.strip()):
                return False
        elif type(value) in (int, float) and type(observed) in (int, float):
            if not math.isfinite(observed) or abs(value - observed) > 0.000001:
                return False
        elif observed != value:
            return False
    return True


class ReadbackAdapter:
    def __init__(self, connection, scope_guard, binding_directory: Path, *, plan_sha256: str):
        if not callable(scope_guard):
            raise ReadbackError("whole_scope_guard_required")
        self.connection = connection
        self.documents = PlanDocuments(connection)
        self.scope_guard = scope_guard
        # The caller supplies a fixed executor-owned directory, never a path
        # taken from the model. Binding values do not alter immutable step data.
        if not binding_directory.is_dir() or binding_directory.is_symlink():
            raise ReadbackError("invalid_binding_directory")
        if not isinstance(plan_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
            raise ReadbackError("invalid_execution_plan_digest")
        self.plan_sha256 = plan_sha256
        self.binding_directory = binding_directory / plan_sha256
        self.binding_directory.mkdir(mode=0o700, exist_ok=True)
        if self.binding_directory.is_symlink() or not self.binding_directory.is_dir():
            raise ReadbackError("invalid_binding_directory")
        parent = os.open(binding_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)

    def _binding_path(self, key):
        return self.binding_directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _binding(self, key, identity):
        path = self._binding_path(key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ReadbackError("invalid_binding_artifact")
        data = json.loads(path.read_text())
        if (
            set(data)
            != {"schema_version", "key", "identity", "record", "step_sha256", "plan_sha256"}
            or data["schema_version"] != 1
            or data["key"] != key
            or data["identity"] != identity
            or data["plan_sha256"] != self.plan_sha256
        ):
            raise ReadbackError("binding_identity_mismatch")
        if not complete_workorder(data["record"]):
            raise ReadbackError("incomplete_bound_workorder")
        return data["record"]

    def _save_binding(self, step, current):
        expected = step.payload["arguments"]
        if not complete_workorder(current) or not matches(current, expected):
            raise ReadbackError("workorder_binding_precondition")
        path = self._binding_path(step.key)
        data = {
            "schema_version": 1,
            "key": step.key,
            "identity": step.payload["identity"],
            "record": current,
            "plan_sha256": self.plan_sha256,
            "step_sha256": hashlib.sha256(
                json.dumps(step.record(), sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        raw = (json.dumps(data, sort_keys=True, allow_nan=False) + "\n").encode()
        temporary = self.binding_directory / (".binding-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            position = 0
            while position < len(raw):
                count = os.write(fd, raw[position:])
                if count <= 0:
                    raise ReadbackError("binding_write_incomplete")
                position += count
            os.fsync(fd)
        finally:
            os.close(fd)
        # link publishes atomically without overwriting an existing binding.
        # A crash may leave an unreferenced temp file, never a half-bound state.
        try:
            os.link(temporary, path)
        finally:
            temporary.unlink()
        directory = os.open(self.binding_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def binding_digest(self, step):
        record = self._binding(step.key, step.payload["identity"])
        if record is None:
            raise ReadbackError("missing_durable_binding")
        raw = self._binding_path(step.key).read_bytes()
        data = json.loads(raw)
        expected = hashlib.sha256(
            json.dumps(step.record(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if data["step_sha256"] != expected:
            raise ReadbackError("binding_step_changed")
        return hashlib.sha256(raw).hexdigest()

    def _read(self, model, ids):
        if model not in FIELDS:
            raise ReadbackError("unsupported_read_model")
        if not ids:
            return []
        result = self.connection.get_model(model).read(ids=ids, fields=FIELDS[model])
        if isinstance(result, dict):
            result = [result]
        if (
            not isinstance(result, list)
            or len(result) != len(ids)
            or not all(
                isinstance(row, dict)
                and set(FIELDS[model]) <= row.keys()
                and type(row["id"]) is int
                for row in result
            )
            or {r["id"] for r in result} != set(ids)
        ):
            raise ReadbackError("incomplete_read")
        return [
            {
                key: value[0] if key in RELATIONS and isinstance(value, list) else value
                for key, value in row.items()
                if key in FIELDS[model]
            }
            for row in result
        ]

    def resolve(self, identity):
        model = identity["model"]
        if model not in FIELDS or model == "purchase.order.line":
            raise ReadbackError("unsupported_target_model")
        if model == "mrp.workorder":
            production = self.resolve(identity["production"])
            if production is None:
                return None
            ids = production["workorder_ids"]
            rows = self._read(model, ids)
            live = [row for row in rows if row["state"] != "cancel"]
            if len(live) != 1:
                raise ReadbackError("expected_one_unexecuted_workorder")
            row = live[0]
            if row["production_id"] != production["id"] or row["state"] in ("done", "progress"):
                raise ReadbackError("workorder_execution_or_parent_conflict")
            if identity["id"] is not None and row["id"] != identity["id"]:
                raise ReadbackError("workorder_identity_changed")
            return row
        if identity["id"] is not None:
            ids = [identity["id"]]
        else:
            ids = self.connection.get_model(model).search(domain=[("name", "=", identity["name"])])
        if not ids:
            return None
        if len(ids) != 1:
            raise ReadbackError("document_identity_not_unique")
        row = self._read(model, ids)[0]
        if row["name"] != identity["name"]:
            raise ReadbackError("document_name_changed")
        if model == "purchase.order":
            lines = [
                line
                for line in self._read("purchase.order.line", row["order_line"])
                if not line["display_type"]
            ]
            if len(lines) != 1 or lines[0]["order_id"] != row["id"]:
                raise ReadbackError("expected_one_purchase_line")
            row["line"] = lines[0]
        return row

    def preflight(self, steps, completed):
        # The guard must compare the current whole scope with the expected
        # cumulative prefix, immutable facts, physical quantities and projection.
        # In particular it must cover untouched documents and latest SO demand.
        from forgeflow.recovery.journal import plan_digest

        if plan_digest(steps) != self.plan_sha256:
            raise ReadbackError("execution_plan_digest_changed")
        self.scope_guard(self, steps, completed)

    def observe(self, step):
        payload = step.payload
        current = self.resolve(payload["identity"])
        if step.kind == "bind_workorder":
            binding = self._binding(step.key, payload["identity"])
            if binding is not None:
                return (
                    "after"
                    if (
                        current is not None
                        and current["id"] == binding["id"]
                        and matches(current, payload["arguments"])
                    )
                    else "conflict"
                )
            return (
                "before"
                if current is not None and matches(current, payload["arguments"])
                else "conflict"
            )
        if current is None:
            return "before" if step.kind.startswith("create_") else "conflict"
        before = payload["before"]
        if before is not None and set(before) == {"binding_key"}:
            before = self._binding(before["binding_key"], payload["identity"])
            if before is None or current["id"] != before["id"]:
                return "conflict"
        if matches(current, payload["after"]):
            if (step.kind == "update_purchase" and payload["arguments"]["mode"] == "line"
                    and before["line"]["date_planned"] != payload["after"]["line"]["date_planned"]
                    and not self.documents.purchase_arrival_matches(
                        payload["arguments"]["line_id"], payload["arguments"]["arrival"]
                    )):
                return "conflict"
            return "after"
        if before is not None and matches(current, before):
            return "before"
        return "conflict"

    def apply(self, step):
        args = copy.deepcopy(step.payload["arguments"])
        kind = step.kind
        if kind == "create_manufacturing":
            self.documents.create_manufacturing(**args)
            return
        if kind == "create_purchase":
            self.documents.create_purchase(**args)
            return
        current = self.resolve(step.payload["identity"])
        if current is None:
            raise ReadbackError("missing_dispatch_target")
        record_id = current["id"]
        if kind == "bind_workorder":
            self._save_binding(step, current)
        elif kind == "confirm_manufacturing":
            self.documents.confirm_manufacturing(record_id)
        elif kind == "cancel_manufacturing":
            self.documents.cancel_manufacturing(record_id)
        elif kind == "confirm_purchase":
            self.documents.confirm_purchase(record_id)
        elif kind == "cancel_purchase":
            self.documents.cancel_purchase(record_id)
        elif kind == "schedule_workorder":
            self.documents.set_workorder_plan(record_id, **args)
        elif kind == "update_manufacturing":
            mode = args.pop("mode")
            if mode == "quantity":
                self.documents.set_manufacturing_quantity(record_id, **args)
            elif mode == "plan":
                self.documents.set_manufacturing_plan(record_id, **args)
            elif mode == "origin":
                self.documents.set_manufacturing_origin(record_id, **args)
            else:
                raise ReadbackError("unsupported_manufacturing_update")
        elif kind == "update_purchase":
            mode = args.pop("mode")
            if mode == "line":
                line_id = args.pop("line_id")
                if current["line"]["id"] != line_id:
                    raise ReadbackError("purchase_line_identity_changed")
                self.documents.set_purchase_line(line_id, **args)
            elif mode == "origin":
                self.documents.set_purchase_origin(record_id, **args)
            else:
                raise ReadbackError("unsupported_purchase_update")
        else:
            raise ReadbackError("unsupported_dispatch_operation")
