"""Per-order planned supply readiness, never a shipping or actual delivery promise."""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from datetime import date, timedelta


def _units(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid_order_readiness")
    return value


def _day(value):
    if type(value) is not int or not 0 <= value <= 36500:
        raise ValueError("invalid_order_readiness")
    return value


def _date(anchor, offset):
    return (date.fromisoformat(anchor) + timedelta(days=offset)).isoformat()


def _status(row):
    if row["covered_units"] != row["demand_units"]:
        return "incomplete"
    if row["planned_ready_day"] is None:
        return "unknown"
    return "on_time" if row["buffer_days"] >= 0 else "late"


def build_order_readiness(request, blueprint, plan, changes, *, plan_id, snapshot_sha256):
    """Recompute from bound customers and independently checked source flows.

    A complete field is optional for legacy artifacts; callers decide whether
    their bound source package contains the required facts. Never backfill a
    saved historical case using new live data.
    """
    try:
        anchor = request["anchor_date"]
        if date.fromisoformat(anchor).isoformat() != anchor:
            raise ValueError
        target = request["target_product_code"]
        customers = blueprint["customers"]
        allocations = plan["allocations"]
        by_ref = {row["ref"]: row for row in allocations}
        if len(by_ref) != len(allocations) or set(by_ref) != {row["ref"] for row in customers}:
            raise ValueError
        sources = {}
        for operation in changes["operations"]:
            row = operation.get("after")
            if row is None or row.get("product_code") != target:
                continue
            if operation["kind"] == "manufacturing_order":
                kind, available = "manufactured_units", row["due_day"]
            elif operation["kind"] == "purchase_order" and row.get("supply_role") == "finished":
                kind, available = "purchased_units", row["arrival_day"]
            else:
                continue
            token = operation["target"]["token"]
            if token in sources:
                raise ValueError
            sources[token] = (kind, _day(available), _units(row["quantity"]))
        totals = defaultdict(int)
        by_order = defaultdict(list)
        names = {row["task_order_ref"] for row in customers}
        if len(names) != len(customers):
            raise ValueError
        for flow in changes["projected_state"]["sales_order_flows"]:
            name = flow["sales_order"]["name"]
            quantity, available = _units(flow["quantity"]), _day(flow["available_day"])
            if name not in names or quantity == 0 or flow["product_code"] != target:
                raise ValueError
            token = flow["source_token"]
            if token == f"stock:{target}":
                kind = "stock_units"
                if available != 0:
                    raise ValueError
            else:
                kind, source_day, source_quantity = sources[token]
                totals[token] += quantity
                if available != source_day or totals[token] > source_quantity:
                    raise ValueError
            by_order[name].append((kind, quantity, available))
        rows = []
        for customer in customers:
            name, allocation = customer["task_order_ref"], by_ref[customer["ref"]]
            demand, deadline = _units(customer["demand"]), _day(customer["deadline"])
            if (demand == 0 or allocation["task_order_ref"] != name
                    or allocation["demand"] != demand or allocation["deadline"] != deadline):
                raise ValueError
            flows = by_order[name]
            ready = max((row[2] for row in flows), default=None)
            row = {"order_ref": name, "demand_units": demand,
                   "covered_units": sum(item[1] for item in flows),
                   "customer_due_day": deadline, "customer_due_date": _date(anchor, deadline),
                   "planned_ready_day": ready,
                   "planned_ready_date": None if ready is None else _date(anchor, ready),
                   "buffer_days": None if ready is None else deadline - ready,
                   **{kind: sum(item[1] for item in flows if item[0] == kind)
                      for kind in ("stock_units", "manufactured_units", "purchased_units")}}
            if any(row[field] != allocation[allocation_field] for field, allocation_field in (
                    ("stock_units", "stock_allocated"), ("manufactured_units", "assembly_units"),
                    ("purchased_units", "purchase_units"), ("covered_units", "fulfilled_demand"))):
                raise ValueError
            row["status"] = _status(row)
            rows.append(row)
        result = {"schema_version": 1, "basis": {"plan_id": plan_id,
            "snapshot_sha256": snapshot_sha256, "anchor_date": anchor,
            "time_basis": "planned_supply_availability_days", "quantity_scope": "target_product"},
            "orders": sorted(rows, key=lambda row: (row["customer_due_day"], row["order_ref"]))}
        return project_order_readiness(result, plan_id=plan_id, snapshot_sha256=snapshot_sha256)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("invalid_order_readiness") from None


def project_order_readiness(value, *, plan_id, snapshot_sha256):
    if value is None:
        return None
    try:
        if (type(value) is not dict or set(value) != {"schema_version", "basis", "orders"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or any(not isinstance(key, str) or re.fullmatch(r"[a-f0-9]{64}", key) is None
                       for key in (plan_id, snapshot_sha256))):
            raise ValueError
        anchor = value["basis"]["anchor_date"]
        if date.fromisoformat(anchor).isoformat() != anchor or value["basis"] != {
                "plan_id": plan_id, "snapshot_sha256": snapshot_sha256, "anchor_date": anchor,
                "time_basis": "planned_supply_availability_days", "quantity_scope": "target_product"}:
            raise ValueError
        rows = value["orders"]
        if type(rows) is not list or not 0 < len(rows) <= 128:
            raise ValueError
        seen = set()
        for row in rows:
            if type(row) is not dict or set(row) != {
                    "order_ref", "demand_units", "covered_units", "customer_due_day", "customer_due_date",
                    "planned_ready_day", "planned_ready_date", "buffer_days", "stock_units",
                    "manufactured_units", "purchased_units", "status"}:
                raise ValueError
            name = row["order_ref"]
            if (not isinstance(name, str) or not 0 < len(name) <= 128 or name in seen
                    or any(ord(char) < 32 for char in name)):
                raise ValueError
            seen.add(name)
            demand, covered = _units(row["demand_units"]), _units(row["covered_units"])
            if not demand or covered > demand or covered != sum(_units(row[key]) for key in (
                    "stock_units", "manufactured_units", "purchased_units")):
                raise ValueError
            deadline = _day(row["customer_due_day"])
            if row["customer_due_date"] != _date(anchor, deadline):
                raise ValueError
            ready = row["planned_ready_day"]
            if ready is None:
                if covered or row["planned_ready_date"] is not None or row["buffer_days"] is not None:
                    raise ValueError
            elif (not covered or row["planned_ready_date"] != _date(anchor, _day(ready))
                  or type(row["buffer_days"]) is not int or row["buffer_days"] != deadline - ready):
                raise ValueError
            if row["status"] != _status(row):
                raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("invalid_order_readiness") from None
    return copy.deepcopy(value)
