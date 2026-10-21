"""Fail-closed, whole-scope guard for single-level manufacturing recovery.

The executor must persist capture_execution_baseline() BEFORE its first write,
along with ScopeGuard.binding, in its trusted journal directory. Resume reads
that same artifact; it must never recapture a replacement baseline. This is a
read-only consistency guard, not a database transaction or a concurrency lock.
The caller must exclude concurrent writers for the duration of execution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from forgeflow.planning.live import collect_snapshot, validate_request
from forgeflow.recovery.compiler import compile_steps
from forgeflow.recovery.journal import plan_digest


class ScopeError(ValueError):
    pass


def need(condition, code):
    if not condition:
        raise ScopeError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def relation(value):
    # A many2one value includes its display name; x2many lists contain only IDs.
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], str):
        return value[0]
    return value


def normalized(value):
    if isinstance(value, dict):
        return {key: normalized(relation(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def same(left, right):
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isfinite(left) and math.isfinite(right) and abs(left - right) <= 1e-6
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same(x, y) for x, y in zip(left, right))
    return type(left) is type(right) and left == right


def subset(actual, expected):
    for key, value in expected.items():
        if key not in actual:
            return False
        observed = actual[key]
        if key == "origin" and isinstance(value, str) and isinstance(observed, str):
            if sorted(x.strip() for x in value.split(",") if x.strip()) != sorted(
                x.strip() for x in observed.split(",") if x.strip()
            ):
                return False
        elif not same(observed, value):
            return False
    return True


def rows_by_id(rows):
    need(isinstance(rows, list) and len(rows) <= 10000, "unbounded_or_invalid_records")
    need(
        all(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0 for row in rows),
        "invalid_record_identity",
    )
    result = {row["id"]: normalized(row) for row in rows}
    need(len(result) == len(rows), "duplicate_record_identity")
    return result


EXTRA_FIELDS = {
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
        "qty_production",
        "qty_produced",
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
        "picking_ids",
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
        "move_ids",
    ],
    "stock.move": [
        "id",
        "product_id",
        "product_uom",
        "product_uom_qty",
        "quantity",
        "picked",
        "state",
        "date",
        "date_deadline",
        "picking_id",
        "company_id",
        "raw_material_production_id",
        "production_id",
        "purchase_line_id",
        "location_id",
        "location_dest_id",
    ],
    "stock.picking": ["id", "state", "origin", "scheduled_date", "date_done", "move_ids"],
}
MUTABLE_TABLES = set(EXTRA_FIELDS) | {"stock.quant"}
WO_SCHEDULE = {"workcenter_id", "date_start", "date_finished", "duration_expected"}


def _read(connection, model, ids):
    need(
        isinstance(ids, list)
        and len(ids) == len(set(ids))
        and len(ids) <= 10000
        and all(type(i) is int and i > 0 for i in ids),
        "invalid_read_ids",
    )
    if not ids:
        return {}
    value = connection.get_model(model).read(ids=sorted(ids), fields=EXTRA_FIELDS[model])
    rows = rows_by_id([value] if isinstance(value, dict) else value)
    need(rows.keys() == set(ids), "missing_original_or_generated_record")
    need(
        all(set(EXTRA_FIELDS[model]) <= row.keys() for row in rows.values()),
        "incomplete_extended_read",
    )
    return rows


def _search(connection, model, domain):
    result = connection.get_model(model).search(domain=domain)
    need(
        isinstance(result, list)
        and len(result) <= 10000
        and all(type(i) is int and i > 0 for i in result)
        and len(result) == len(set(result)),
        "invalid_search_ids",
    )
    return sorted(result)


def _tables(snapshot):
    need(
        snapshot.get("schema_version") == 1 and isinstance(snapshot.get("models"), dict),
        "unsupported_snapshot",
    )
    return {model: rows_by_id(rows) for model, rows in snapshot["models"].items()}


def _company(snapshot, request):
    models = _tables(snapshot)
    ids = set(request["sales_order_ids"])
    sales = models["sale.order"]
    need(ids <= sales.keys(), "missing_requested_sales_order")
    companies = {sales[i]["company_id"] for i in ids}
    need(
        len(companies) == 1 and type(next(iter(companies))) is int, "unsupported_multiple_companies"
    )
    return next(iter(companies))


def _read_scope(connection, snapshot, request):
    fresh = collect_snapshot(connection, request)
    models = _tables(fresh)
    company = _company(snapshot, request)
    # Include done/cancelled and out-of-closure company documents. Active-only
    # planner collection must not hide cancellation, deletion, or extra orders.
    for model in ("mrp.production", "purchase.order"):
        inventory = _search(connection, model, [("company_id", "=", company)])
        models[model] = _read(connection, model, sorted(set(inventory) | models[model].keys()))
    wo_ids = sorted({i for row in models["mrp.production"].values() for i in row["workorder_ids"]})
    all_live_wo = _search(connection, "mrp.workorder", [("state", "not in", ["done", "cancel"])])
    models["mrp.workorder"] = _read(connection, "mrp.workorder", sorted(set(wo_ids + all_live_wo)))
    line_ids = sorted({i for row in models["purchase.order"].values() for i in row["order_line"]})
    models["purchase.order.line"] = _read(connection, "purchase.order.line", line_ids)
    move_ids = {
        i
        for row in models["mrp.production"].values()
        for field in ("move_raw_ids", "move_finished_ids")
        for i in row[field]
    }
    move_ids.update(i for row in models["purchase.order.line"].values() for i in row["move_ids"])
    # Search back-links too: extra moves cannot be concealed by a parent list.
    for field, owner_ids in (
        ("raw_material_production_id", list(models["mrp.production"])),
        ("production_id", list(models["mrp.production"])),
        ("purchase_line_id", line_ids),
    ):
        move_ids.update(_search(connection, "stock.move", [(field, "in", owner_ids)]))
    models["stock.move"] = _read(connection, "stock.move", sorted(move_ids))
    picking_ids = sorted(
        {i for row in models["purchase.order"].values() for i in row["picking_ids"]}
    )
    models["stock.picking"] = _read(connection, "stock.picking", picking_ids)
    # Original IDs MUST remain readable even if search domains stopped matching.
    for model in ("mrp.production", "mrp.workorder", "purchase.order", "purchase.order.line"):
        original = set(_tables(snapshot)[model])
        missing = sorted(original - models[model].keys())
        if missing:
            models[model].update(_read(connection, model, missing))
    return models


def _stock_totals(rows):
    totals = defaultdict(float)
    for row in rows.values():
        number = row["quantity"]
        need(type(number) in (int, float) and math.isfinite(number), "invalid_stock_quantity")
        # Reservation changes are legitimate planning effects, physical transfers
        # between locations/companies are not. Quant split/merge is immaterial.
        totals[(row["product_id"], row["location_id"], row["company_id"])] += number
    return {key: value for key, value in totals.items() if abs(value) > 1e-6}


def _initial_matches(snapshot, models):
    original = _tables(snapshot)
    for model, before in original.items():
        need(model in models and before.keys() == models[model].keys(), "baseline_scope_drift")
        for identity, row in before.items():
            need(subset(models[model][identity], row), "baseline_fact_drift")


def capture_execution_baseline(connection, snapshot, request):
    """Read only; return an envelope for atomic persistence by the executor."""
    validate_request(request)
    models = _read_scope(connection, snapshot, request)
    _initial_matches(snapshot, models)
    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_sha256": digest(snapshot),
        "request_sha256": digest(request),
        "models": {
            model: list(sorted(rows.values(), key=lambda r: r["id"]))
            for model, rows in models.items()
        },
        "workorders": list(sorted(models["mrp.workorder"].values(), key=lambda r: r["id"])),
    }
    return {"payload": payload, "sha256": digest(payload)}


class ScopeGuard:
    def __init__(self, snapshot, request, change_set, workorders, execution_baseline):
        validate_request(request)
        self.snapshot, self.request, self.change_set = copy.deepcopy(
            (snapshot, request, change_set)
        )
        self.baseline = copy.deepcopy(execution_baseline)
        payload = self.baseline["payload"]
        need(
            set(self.baseline) == {"payload", "sha256"}
            and payload["schema_version"] == 1
            and digest(payload) == self.baseline["sha256"],
            "baseline_integrity_mismatch",
        )
        need(
            payload["snapshot_sha256"] == digest(snapshot)
            and payload["request_sha256"] == digest(request),
            "baseline_source_mismatch",
        )
        need(
            change_set["source_sha256"]["snapshot"] == digest(snapshot)
            and change_set["source_sha256"]["request"] == digest(request),
            "change_set_source_mismatch",
        )
        need(
            rows_by_id(workorders) == rows_by_id(payload["workorders"]),
            "workorder_baseline_mismatch",
        )
        self.before = {model: rows_by_id(rows) for model, rows in payload["models"].items()}
        self._before_sha256 = digest(self.before)
        _initial_matches(snapshot, self.before)
        self._supported()
        compiler_path = Path(__file__).with_name("compiler.py")
        compiler_source = compiler_path.read_bytes()
        self.compiled = compile_steps(change_set, snapshot, workorders)
        self.steps = tuple(self.compiled["steps"])
        self.binding = {
            "snapshot_sha256": digest(snapshot),
            "request_sha256": digest(request),
            "change_set_sha256": digest(change_set),
            "execution_baseline_sha256": self.baseline["sha256"],
            "steps_sha256": digest([s.record() for s in self.steps]),
            "journal_plan_sha256": plan_digest(self.steps),
            "compiler_source_sha256": hashlib.sha256(compiler_source).hexdigest(),
        }
        self._construction_hash = digest(self.binding)

    def _supported(self):
        models = self.before
        code = self.request["target_product_code"]
        targets = [r for r in models["product.product"].values() if r["default_code"] == code]
        need(len(targets) == 1, "ambiguous_target_product")
        self.product = targets[0]
        boms = list(models["mrp.bom"].values())
        need(
            len(boms) == 1
            and boms[0]["type"] == "normal"
            and boms[0]["product_tmpl_id"] == self.product["product_tmpl_id"]
            and boms[0]["product_id"] in (False, self.product["id"]),
            "unsupported_non_single_level_bom",
        )
        self.bom = boms[0]
        need(
            self.bom["product_qty"] > 0 and self.bom["product_uom_id"] == self.product["uom_id"],
            "unsupported_bom_uom",
        )
        lines = list(models["mrp.bom.line"].values())
        need(
            lines and all(r["bom_id"] == self.bom["id"] and r["product_qty"] > 0 for r in lines),
            "unsupported_bom_components",
        )
        need(
            len({r["product_id"] for r in lines}) == len(lines)
            and self.product["id"] not in {r["product_id"] for r in lines},
            "unsupported_bom_shape",
        )
        self.components = {r["product_id"]: r for r in lines}
        need(
            all(
                r["product_uom_id"] == models["product.product"][r["product_id"]]["uom_id"]
                for r in lines
            ),
            "unsupported_component_uom",
        )
        operations = list(models["mrp.routing.workcenter"].values())
        need(
            len(operations) == 1 and operations[0]["bom_id"] == self.bom["id"],
            "unsupported_multiple_operations",
        )
        self.operation = operations[0]
        company = _company(self.snapshot, self.request)
        need(
            self.change_set["scope"]["company_id"] == company
            and self.change_set["scope"]["target_product_id"] == self.product["id"]
            and sorted(self.change_set["scope"]["sales_order_ids"])
            == sorted(self.request["sales_order_ids"]),
            "change_set_scope_mismatch",
        )
        for row in models["mrp.production"].values():
            need(
                row["company_id"] == company
                and row["product_id"] == self.product["id"]
                and row["bom_id"] == self.bom["id"]
                and row["state"] == "confirmed"
                and len(row["workorder_ids"]) == 1,
                "unsupported_existing_manufacturing",
            )
        for row in models["purchase.order"].values():
            need(
                row["company_id"] == company
                and row["state"] in ("purchase", "cancel")
                and len(row["order_line"]) == 1,
                "unsupported_existing_purchase",
            )
        for model, field in (
            ("sale.order.line", "qty_delivered"),
            ("purchase.order.line", "qty_received"),
            ("mrp.production", "qty_produced"),
            ("mrp.workorder", "qty_produced"),
        ):
            need(
                all(row[field] == 0 for row in models[model].values()),
                "unsupported_preexisting_physical_execution",
            )
        need(
            all(
                row["state"] not in ("done", "progress") for row in models["mrp.workorder"].values()
            ),
            "unsupported_executed_workorder",
        )
        # Existing scheduled jobs may keep their center, but crossing calendars
        # needs an explicitly reviewed unplan/calendar procedure, not this API.
        for operation in self.change_set["operations"]:
            source = operation["preconditions"]
            need(
                source["scope_snapshot_sha256"] == digest(self.snapshot),
                "operation_source_mismatch",
            )
            identity = operation["target"]
            if operation["action"] != "create":
                original = _tables(self.snapshot)[identity["model"]].get(identity["id"])
                need(
                    original is not None
                    and original["name"] == identity["name"]
                    and same(normalized(source["source_records"]["parent"]), original),
                    "operation_parent_source_mismatch",
                )
                child_model = (
                    "mrp.workorder"
                    if identity["model"] == "mrp.production"
                    else "purchase.order.line"
                )
                parent_field = "production_id" if child_model == "mrp.workorder" else "order_id"
                originals = {
                    i: row
                    for i, row in _tables(self.snapshot)[child_model].items()
                    if row[parent_field] == identity["id"]
                }
                need(
                    same(rows_by_id(source["source_records"]["children"]), originals),
                    "operation_children_source_mismatch",
                )
            if operation["kind"] == "manufacturing_order" and operation["action"] != "cancel":
                planned = operation["after"]["workorder_plan"]
                existing_id = planned["existing_id"]
                if existing_id is not None:
                    wo = models["mrp.workorder"][existing_id]
                    need(
                        wo["workcenter_id"] == operation["after"]["workcenter_id"],
                        "unsupported_existing_workcenter_change",
                    )

    def __call__(self, adapter, steps, completed):
        # Bind copied inputs, exact compiler output and the contiguous journal
        # prefix on EVERY invocation; no model-supplied operation is accepted.
        need(
            digest(self.binding) == self._construction_hash
            and digest(self.snapshot) == self.binding["snapshot_sha256"]
            and digest(self.request) == self.binding["request_sha256"]
            and digest(self.change_set) == self.binding["change_set_sha256"]
            and digest(self.before) == self._before_sha256
            and digest(self.baseline["payload"]) == self.binding["execution_baseline_sha256"],
            "guard_inputs_changed",
        )
        need(
            digest([s.record() for s in steps]) == self.binding["steps_sha256"],
            "untrusted_or_changed_steps",
        )
        keys = [step.key for step in steps]
        n = len(completed)
        need(n <= len(keys) and set(completed) == set(keys[:n]), "noncontiguous_completed_prefix")
        current = _read_scope(adapter.connection, self.snapshot, self.request)
        # A single lost-response operation may already be fully applied. Only
        # the immediate next step can be reconciled, never an arbitrary mixture.
        candidates = [n]
        if n < len(steps) and adapter.observe(steps[n]) == "after":
            candidates.append(n + 1)
        failures = []
        for count in candidates:
            try:
                self._check_projection(adapter, steps, count, current)
                return
            except ScopeError as error:
                failures.append(str(error))
        raise ScopeError(failures[-1] if failures else "scope_projection_conflict")

    def _check_projection(self, adapter, steps, count, current):
        expected = copy.deepcopy(self.before)
        scheduled = {}
        bound = {}
        changed_mo = set()
        created = {"mrp.production": set(), "purchase.order": set()}
        for step in steps[:count]:
            identity = step.payload["identity"]
            model = identity["model"]
            row = adapter.resolve(identity)
            need(row is not None, "missing_completed_document")
            record_id = row["id"]
            after = normalized(step.payload["after"])
            if step.kind == "bind_workorder":
                binding = adapter._binding(step.key, identity)
                need(
                    binding is not None and binding["id"] == record_id, "missing_workorder_binding"
                )
                adapter.binding_digest(step)
                bound[record_id] = normalized(binding)
                continue
            if model == "mrp.workorder":
                scheduled[record_id] = after
                continue
            if step.kind.startswith("create_"):
                need(
                    identity["id"] is None and identity["name"].startswith("FF/"),
                    "untrusted_created_name",
                )
                need(record_id not in self.before[model], "created_identity_reused")
                expected[model][record_id] = {}
                created[model].add(record_id)
            need(record_id in expected[model], "unbound_document_identity")
            target = expected[model][record_id]
            target.update({k: v for k, v in after.items() if k != "line"})
            target["id"] = record_id
            if model == "purchase.order":
                line_ids = current[model][record_id]["order_line"]
                need(len(line_ids) == 1, "ambiguous_purchase_lines")
                line_id = line_ids[0]
                if record_id in created[model]:
                    target["order_line"] = line_ids
                    expected["purchase.order.line"].setdefault(
                        line_id, {"id": line_id, "order_id": record_id}
                    )
                if "line" in after:
                    expected["purchase.order.line"][line_id].update(after["line"])
            elif step.kind == "update_manufacturing" and step.payload["arguments"]["mode"] in (
                "quantity",
                "plan",
            ):
                changed_mo.add(record_id)

        for model, rows in self.before.items():
            if model not in MUTABLE_TABLES:
                need(
                    model in current and same(rows, current[model]),
                    "immutable_scope_drift:" + model,
                )
        need(
            same(_stock_totals(self.before["stock.quant"]), _stock_totals(current["stock.quant"])),
            "physical_stock_drift",
        )
        for model in ("mrp.production", "purchase.order", "purchase.order.line"):
            need(
                expected[model].keys() == current[model].keys(),
                "extra_or_missing_document:" + model,
            )
        for model, field in (
            ("sale.order.line", "qty_delivered"),
            ("purchase.order.line", "qty_received"),
            ("mrp.production", "qty_produced"),
            ("mrp.workorder", "qty_produced"),
        ):
            need(
                all(row[field] == 0 for row in current[model].values()),
                "physical_execution:" + model,
            )

        accounted_wo, accounted_moves, accounted_pickings = set(), set(), set()
        for mid, wanted in expected["mrp.production"].items():
            mo = current["mrp.production"][mid]
            old = self.before["mrp.production"].get(mid)
            # Only the workorder scheduling inverse may change MO.date_finished.
            skip = {"date_finished", "workorder_ids", "move_raw_ids", "move_finished_ids"}
            need(
                subset(mo, {k: v for k, v in wanted.items() if k not in skip}),
                "manufacturing_projection_drift",
            )
            wo_ids = mo["workorder_ids"]
            need(len(wo_ids) == 1, "manufacturing_workorder_count")
            wid = wo_ids[0]
            need(wid in current["mrp.workorder"], "missing_original_workorder")
            wo = current["mrp.workorder"][wid]
            accounted_wo.add(wid)
            if old:
                need(
                    wo_ids == old["workorder_ids"]
                    and mo["move_raw_ids"] == old["move_raw_ids"]
                    and mo["move_finished_ids"] == old["move_finished_ids"],
                    "original_child_identity_drift",
                )
            need(
                wo["production_id"] == mid
                and wo["product_id"] == mo["product_id"]
                and wo["operation_id"] == self.operation["id"],
                "workorder_identity_drift",
            )
            need(
                wo["state"] not in ("progress", "done")
                and same(wo["qty_production"], mo["product_qty"]),
                "workorder_execution_or_quantity_drift",
            )
            if old:
                original_wo = self.before["mrp.workorder"][wid]
                need(
                    subset(
                        wo,
                        {
                            k: v
                            for k, v in original_wo.items()
                            if k not in WO_SCHEDULE | {"state", "qty_production"}
                        },
                    ),
                    "original_workorder_fact_drift",
                )
            if mo["state"] != "cancel":
                need(
                    wo["state"] in ("pending", "waiting", "ready", "blocked"),
                    "unexpected_inactive_workorder",
                )
            if mo["state"] == "cancel":
                need(wo["state"] == "cancel", "cancelled_workorder_not_cancelled")
                need(
                    mo["date_finished"] == old["date_finished"],
                    "cancelled_manufacturing_finish_drift",
                )
                need(
                    old is not None
                    and subset(
                        wo,
                        {
                            k: v
                            for k, v in self.before["mrp.workorder"][wid].items()
                            if k != "state"
                        },
                    ),
                    "cancelled_workorder_drift",
                )
            elif wid in scheduled:
                need(subset(wo, scheduled[wid]), "workorder_schedule_drift")
                need(
                    mo["date_start"] == wo["date_start"]
                    and mo["date_finished"] == wo["date_finished"],
                    "manufacturing_workorder_dates_not_synchronized",
                )
            elif wid in bound:
                need(subset(wo, bound[wid]), "bound_workorder_drift")
            elif old and mid not in changed_mo:
                need(subset(wo, self.before["mrp.workorder"][wid]), "untouched_workorder_drift")
                need(
                    mo["date_finished"] == old["date_finished"],
                    "untouched_manufacturing_finish_drift",
                )
            elif old:
                raise ScopeError("unbound_automatic_workorder_recalculation")
            else:
                need(
                    wo["workcenter_id"] == self.operation["workcenter_id"]
                    and wo["date_start"] is False
                    and wo["date_finished"] is False
                    and type(wo["duration_expected"]) in (int, float)
                    and math.isfinite(wo["duration_expected"])
                    and wo["duration_expected"] > 0,
                    "unbound_new_workorder_drift",
                )
            self._manufacturing_moves(mo, current, accounted_moves)

        for pid, wanted in expected["purchase.order"].items():
            po = current["purchase.order"][pid]
            old = self.before["purchase.order"].get(pid)
            need(
                subset(po, {k: v for k, v in wanted.items() if k != "picking_ids"}),
                "purchase_projection_drift",
            )
            line_id = po["order_line"][0]
            line = current["purchase.order.line"][line_id]
            wanted_line = expected["purchase.order.line"][line_id]
            need(
                subset(line, {k: v for k, v in wanted_line.items() if k != "move_ids"}),
                "purchase_line_drift",
            )
            need(not line["display_type"] and line["order_id"] == pid, "purchase_line_parent_drift")
            old_line = self.before["purchase.order.line"].get(line_id)
            if old:
                need(
                    po["order_line"] == old["order_line"]
                    and po["picking_ids"] == old["picking_ids"]
                    and line["move_ids"] == old_line["move_ids"],
                    "purchase_child_identity_drift",
                )
            self._purchase_moves(po, line, old_line, current, accounted_moves, accounted_pickings)
        need(accounted_wo == current["mrp.workorder"].keys(), "extra_workorder")
        need(accounted_moves == current["stock.move"].keys(), "extra_or_unlinked_stock_move")
        need(accounted_pickings == current["stock.picking"].keys(), "extra_or_unlinked_receipt")
        if count == len(steps):
            for item in self.compiled["final_expectations"].values():
                row = adapter.resolve(item["identity"])
                wanted = normalized(item["fields"])
                line = wanted.pop("line", None)
                need(row is not None and subset(row, wanted), "final_target_projection_mismatch")
                if line is not None:
                    need(subset(row["line"], line), "final_purchase_line_projection_mismatch")

    def _manufacturing_moves(self, mo, current, accounted):
        components = self.components
        raw = [current["stock.move"][i] for i in mo["move_raw_ids"]]
        finished = [current["stock.move"][i] for i in mo["move_finished_ids"]]
        need(
            len(raw) == len(components)
            and {m["product_id"] for m in raw} == components.keys()
            and len(finished) == 1
            and finished[0]["product_id"] == mo["product_id"],
            "unsupported_manufacturing_move_shape",
        )
        for move in raw + finished:
            need(move["id"] not in accounted, "shared_stock_move")
            accounted.add(move["id"])
            is_raw = move in raw
            wanted = (
                components[move["product_id"]]["product_qty"] / self.bom["product_qty"]
                if is_raw
                else 1
            ) * mo["product_qty"]
            need(same(move["product_uom_qty"], wanted), "manufacturing_moves_not_scaled_to_bom")
            need(
                move["product_uom"] == current["product.product"][move["product_id"]]["uom_id"]
                and move["company_id"] == mo["company_id"]
                and not move["purchase_line_id"]
                and not move["picking_id"]
                and move["raw_material_production_id"] == (mo["id"] if is_raw else False)
                and move["production_id"] == (False if is_raw else mo["id"]),
                "manufacturing_move_binding_drift",
            )
            self._move_unexecuted(move, mo["state"])
            old = self.before["stock.move"].get(move["id"])
            if old:
                need(
                    all(move[k] == old[k] for k in ("location_id", "location_dest_id")),
                    "manufacturing_move_location_drift",
                )
            old_mo = self.before["mrp.production"].get(mo["id"])
            reference_date = "date_start" if is_raw else "date_finished"
            reference_deadline = "date_start" if is_raw else "date_deadline"
            for field, reference in (
                ("date", reference_date),
                ("date_deadline", reference_deadline),
            ):
                expected_date = (
                    old[field] if old and old_mo[reference] == mo[reference] else mo[reference]
                )
                need(move[field] == expected_date, "manufacturing_move_date_drift")

    def _move_unexecuted(self, move, parent_state):
        need(move["state"] != "done" and move["picked"] is False, "physical_stock_move_execution")
        if parent_state == "cancel":
            need(move["state"] == "cancel", "stock_move_not_cancelled")
        elif parent_state == "draft":
            need(move["state"] == "draft", "premature_stock_move_confirmation")
        else:
            need(
                move["state"] in ("confirmed", "waiting", "assigned", "partially_available"),
                "invalid_planned_stock_move_state",
            )

    def _purchase_moves(self, po, line, old_line, current, moves, pickings):
        receipt_moves = [current["stock.move"][i] for i in line["move_ids"]]
        if po["state"] == "draft":
            need(not receipt_moves and not po["picking_ids"], "draft_purchase_has_receipt")
            return
        need(
            len(receipt_moves) == 1 and len(po["picking_ids"]) == 1,
            "unsupported_split_purchase_receipts",
        )
        move = receipt_moves[0]
        need(move["id"] not in moves, "shared_stock_move")
        moves.add(move["id"])
        need(
            move["product_id"] == line["product_id"]
            and move["product_uom"] == line["product_uom_id"]
            and same(move["product_uom_qty"], line["product_qty"])
            and move["purchase_line_id"] == line["id"]
            and move["company_id"] == po["company_id"]
            and not move["raw_material_production_id"]
            and not move["production_id"]
            and move["picking_id"] == po["picking_ids"][0],
            "purchase_move_binding_or_quantity_drift",
        )
        self._move_unexecuted(move, po["state"])
        old_move = self.before["stock.move"].get(move["id"])
        arrival_changed = old_line is None or line["date_planned"] != old_line["date_planned"]
        if arrival_changed:
            # A successful PO line/header write does NOT update stock.move.date
            # in pinned Odoo 19. Both fields must actually reflect the new plan.
            need(
                move["date"] == line["date_planned"]
                and move["date_deadline"] == line["date_planned"],
                "purchase_arrival_not_synchronized",
            )
        elif old_move:
            need(
                move["date"] == old_move["date"]
                and move["date_deadline"] == old_move["date_deadline"],
                "unchanged_purchase_move_date_drift",
            )
        if old_move:
            need(
                all(move[k] == old_move[k] for k in ("location_id", "location_dest_id")),
                "purchase_move_location_drift",
            )
        picking = current["stock.picking"][move["picking_id"]]
        need(picking["id"] not in pickings, "shared_purchase_receipt")
        pickings.add(picking["id"])
        need(
            picking["move_ids"] == [move["id"]]
            and not picking["date_done"]
            and picking["state"] != "done",
            "physical_receipt_execution",
        )
        if po["state"] == "cancel":
            need(picking["state"] == "cancel", "receipt_not_cancelled")
        else:
            need(picking["state"] in ("confirmed", "waiting", "assigned"), "invalid_receipt_state")
        if arrival_changed:
            need(
                picking["scheduled_date"] == line["date_planned"],
                "receipt_schedule_not_synchronized",
            )
        else:
            old = self.before["stock.picking"][picking["id"]]
            need(
                picking["scheduled_date"] == old["scheduled_date"]
                and picking["origin"] == old["origin"],
                "unchanged_receipt_plan_drift",
            )
