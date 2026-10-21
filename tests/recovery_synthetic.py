"""Pure synthetic ERP state for recovery tests; no saved runs or task assets."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

from test_recovery_change_set import bind
from test_recovery_change_set import fixture as business_fixture

from forgeflow.planning.live import FIELDS
from forgeflow.recovery import scope_guard as module
from forgeflow.recovery.change_set import build_change_set
from forgeflow.recovery.journal import plan_digest
from forgeflow.recovery.readback import ReadbackAdapter
from forgeflow.recovery.scope_guard import ScopeGuard, capture_execution_baseline, normalized


def inputs(case="mixed"):
    request, snapshot, blueprint, plan = business_fixture()
    if case == "new_purchase":
        snapshot["models"]["purchase.order"] = []
        snapshot["models"]["purchase.order.line"] = []
        request, snapshot, blueprint, plan = bind(request, snapshot)
    elif case == "origin_only":
        models = snapshot["models"]
        customer = copy.deepcopy(models["res.partner"][0])
        customer.update(id=3, name="Second customer", ref="C2")
        models["res.partner"].append(customer)
        sale = copy.deepcopy(models["sale.order"][0])
        sale.update(id=2, name="SO2", partner_id=[3, "Second customer"])
        models["sale.order"].append(sale)
        line = copy.deepcopy(models["sale.order.line"][0])
        line.update(id=2, order_id=[2, "SO2"], product_uom_qty=6)
        models["sale.order.line"].append(line)
        models["sale.order.line"][0]["product_uom_qty"] = 4
        request["sales_order_ids"] = [1, 2]
        request["sales_order_budget_caps"]["2"] = 2000
        snapshot["scope"]["sales_order_ids"] = [1, 2]
        request, snapshot, blueprint, plan = bind(request, snapshot)
        first = plan["plan"]["allocations"][0]
        first.update(demand=4, fulfilled_demand=4, assembly_units=4)
        second = copy.deepcopy(first)
        second.update(
            ref="C2", task_order_ref="SO2", demand=6, fulfilled_demand=6, assembly_units=6
        )
        plan["plan"]["allocations"].append(second)
        plan["plan"]["manufacturing_orders"][0]["origin_customer_refs"] = ["C1", "C2"]
    elif case == "quantity":
        return business_fixture(quantity=4)
    elif case != "mixed":
        raise ValueError("Unknown synthetic scenario")
    return request, snapshot, blueprint, plan


def compilation_inputs(case="mixed"):
    values = inputs(case)
    snapshot = values[1]
    changes = build_change_set(*values)
    workorders = [
        {**row, "date_start": False, "date_finished": False, "duration_expected": 1.0}
        for row in snapshot["models"]["mrp.workorder"]
    ]
    return changes, snapshot, workorders


class Model:
    def __init__(self, database, model):
        self.database, self.model = database, model

    def read(self, *, ids, fields):
        return [
            {key: copy.deepcopy(row[key]) for key in fields}
            for identity in ids
            if (row := self.database[self.model].get(identity))
        ]

    def search(self, *, domain):
        def match(row):
            for field, op, value in domain:
                actual = row[field]
                if op == "=" and actual != value:
                    return False
                if op == "in" and actual not in value:
                    return False
                if op == "not in" and actual in value:
                    return False
            return True

        return [identity for identity, row in self.database[self.model].items() if match(row)]


class Fake:
    def __init__(self, snapshot):
        self.db = {
            model: {row["id"]: normalized(row) for row in rows}
            for model, rows in snapshot["models"].items()
        }
        self.columns = {
            model: [field[0] if isinstance(field, tuple) else field for field in FIELDS[model]]
            for model in snapshot["models"]
        }
        self.db["stock.move"] = {}
        self.db["stock.picking"] = {}
        for mo in self.db["mrp.production"].values():
            wo = next(
                row for row in self.db["mrp.workorder"].values() if row["production_id"] == mo["id"]
            )
            mo.update(
                workorder_ids=[wo["id"]],
                move_raw_ids=[],
                move_finished_ids=[],
                date_finished=mo["date_deadline"],
            )
            wo.update(
                operation_id=next(iter(self.db["mrp.routing.workcenter"])),
                date_start=False,
                date_finished=False,
                duration_expected=1.0,
                qty_production=mo["product_qty"],
                qty_produced=0,
            )
            self.mo_moves(mo)
        for po in self.db["purchase.order"].values():
            lines = [
                row
                for row in self.db["purchase.order.line"].values()
                if row["order_id"] == po["id"]
            ]
            po.update(order_line=[row["id"] for row in lines], picking_ids=[])
            for line in lines:
                line["move_ids"] = []
                self.po_receipt(po, line)

    def get_model(self, name):
        return Model(self.db, name)

    def next_id(self, model):
        return max(self.db[model], default=0) + 1

    def move(self, product, quantity, company, date, deadline, **links):
        identity = self.next_id("stock.move")
        row = {
            "id": identity,
            "product_id": product,
            "product_uom": self.db["product.product"][product]["uom_id"],
            "product_uom_qty": quantity,
            "quantity": 0.0,
            "picked": False,
            "state": "confirmed",
            "date": date,
            "date_deadline": deadline,
            "picking_id": False,
            "company_id": company,
            "raw_material_production_id": False,
            "production_id": False,
            "purchase_line_id": False,
            "location_id": 1,
            "location_dest_id": 2,
            **links,
        }
        self.db["stock.move"][identity] = row
        return identity

    def mo_moves(self, mo):
        bom = self.db["mrp.bom"][mo["bom_id"]]
        for line in self.db["mrp.bom.line"].values():
            identity = self.move(
                line["product_id"],
                mo["product_qty"] * line["product_qty"] / bom["product_qty"],
                mo["company_id"],
                mo["date_start"],
                mo["date_start"],
                raw_material_production_id=mo["id"],
                state=mo["state"],
            )
            mo["move_raw_ids"].append(identity)
        mo["move_finished_ids"] = [
            self.move(
                mo["product_id"],
                mo["product_qty"],
                mo["company_id"],
                mo["date_finished"],
                mo["date_deadline"],
                production_id=mo["id"],
                state=mo["state"],
            )
        ]

    def po_receipt(self, po, line):
        picking = self.next_id("stock.picking")
        identity = self.move(
            line["product_id"],
            line["product_qty"],
            po["company_id"],
            line["date_planned"],
            line["date_planned"],
            purchase_line_id=line["id"],
            picking_id=picking,
            state="cancel" if po["state"] == "cancel" else "assigned",
        )
        line["move_ids"] = [identity]
        po["picking_ids"] = [picking]
        self.db["stock.picking"][picking] = {
            "id": picking,
            "state": "cancel" if po["state"] == "cancel" else "assigned",
            "origin": po["origin"],
            "scheduled_date": line["date_planned"],
            "date_done": False,
            "move_ids": [identity],
        }

    def collect(self, request):
        # Mirror the real collector's active-only MO/WO semantics, including
        # disappearance of cancelled originals. ScopeGuard must still read IDs.
        models = {}
        for model, columns in self.columns.items():
            rows = list(self.db[model].values())
            if model in ("mrp.production", "mrp.workorder"):
                rows = [row for row in rows if row["state"] not in ("cancel", "done")]
            models[model] = [{key: copy.deepcopy(row[key]) for key in columns} for row in rows]
        return {"schema_version": 1, "models": models}

    def apply(self, adapter, step):
        model, after = step.payload["identity"]["model"], copy.deepcopy(step.payload["after"])
        if step.kind == "bind_workorder":
            adapter.apply(step)
            return
        if step.kind.startswith("create_"):
            identity = self.next_id(model)
            row = {key: value for key, value in after.items() if key != "line"}
            row["id"] = identity
            self.db[model][identity] = row
            if model == "mrp.production":
                wo_id = self.next_id("mrp.workorder")
                row.update(
                    workorder_ids=[wo_id],
                    move_raw_ids=[],
                    move_finished_ids=[],
                    date_finished=(
                        datetime.fromisoformat(row["date_start"]) + timedelta(days=5)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                )
                operation = next(iter(self.db["mrp.routing.workcenter"].values()))
                self.db["mrp.workorder"][wo_id] = {
                    "id": wo_id,
                    "name": "Generated WO",
                    "state": "ready",
                    "production_id": identity,
                    "workcenter_id": operation["workcenter_id"],
                    "product_id": row["product_id"],
                    "operation_id": operation["id"],
                    "date_start": False,
                    "date_finished": False,
                    "duration_expected": 17.0,
                    "qty_production": row["product_qty"],
                    "qty_produced": 0,
                }
                self.mo_moves(row)
            else:
                line_id = self.next_id("purchase.order.line")
                row.update(order_line=[line_id], picking_ids=[])
                self.db["purchase.order.line"][line_id] = {
                    **after["line"],
                    "id": line_id,
                    "order_id": identity,
                    "display_type": False,
                    "move_ids": [],
                }
            return
        resolved = adapter.resolve(step.payload["identity"])
        row = self.db[model][resolved["id"]]
        row.update({key: value for key, value in after.items() if key != "line"})
        if model == "purchase.order":
            line = self.db["purchase.order.line"][row["order_line"][0]]
            if "line" in after:
                line.update(after["line"])
            if step.kind == "confirm_purchase":
                self.po_receipt(row, line)
            if step.kind == "cancel_purchase":
                for identity in row["picking_ids"]:
                    self.db["stock.picking"][identity]["state"] = "cancel"
                for identity in line["move_ids"]:
                    self.db["stock.move"][identity]["state"] = "cancel"
            if step.kind == "update_purchase" and step.payload["arguments"]["mode"] == "line":
                for identity in line["move_ids"]:
                    move = self.db["stock.move"][identity]
                    move.update(
                        product_uom_qty=line["product_qty"], date_deadline=line["date_planned"]
                    )
        elif model == "mrp.production":
            wo = self.db["mrp.workorder"][row["workorder_ids"][0]]
            if step.kind == "cancel_manufacturing":
                wo["state"] = "cancel"
            for identity in row["move_raw_ids"] + row["move_finished_ids"]:
                self.db["stock.move"][identity]["state"] = row["state"]
            if (
                step.kind == "update_manufacturing"
                and step.payload["arguments"]["mode"] == "quantity"
            ):
                ratio = row["product_qty"] / wo["qty_production"]
                wo["qty_production"] = row["product_qty"]
                wo["duration_expected"] *= ratio
                for identity in row["move_raw_ids"] + row["move_finished_ids"]:
                    self.db["stock.move"][identity]["product_uom_qty"] *= ratio
        elif step.kind == "schedule_workorder":
            mo = self.db["mrp.production"][row["production_id"]]
            mo["date_start"], mo["date_finished"] = row["date_start"], row["date_finished"]
            for identity in mo["move_raw_ids"]:
                self.db["stock.move"][identity].update(
                    date=mo["date_start"], date_deadline=mo["date_start"]
                )
            for identity in mo["move_finished_ids"]:
                self.db["stock.move"][identity].update(
                    date=mo["date_finished"], date_deadline=mo["date_deadline"]
                )


def fixture(monkeypatch, tmp_path, case="mixed", transform=None):
    request, snapshot, blueprint, plan = inputs(case)
    changes = build_change_set(request, snapshot, blueprint, plan)
    if transform:
        transform(changes)
    connection = Fake(snapshot)
    monkeypatch.setattr(module, "collect_snapshot", lambda client, request: client.collect(request))
    baseline = capture_execution_baseline(connection, snapshot, request)
    workorders = baseline["payload"]["workorders"]
    guard = ScopeGuard(snapshot, request, changes, workorders, baseline)
    adapter = ReadbackAdapter(connection, guard, tmp_path, plan_sha256=plan_digest(guard.steps))
    return connection, guard, adapter
