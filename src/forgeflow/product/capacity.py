"""Planning quantities and standard load; never elapsed work or a calendar schedule."""
from __future__ import annotations

import copy
import math
import re
from decimal import Decimal


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_capacity_measurement")
    return Decimal(str(value))


def build_capacity_summary(request, blueprint, changes, *, plan_id, snapshot_sha256):
    standards = {}
    for product in blueprint["manufactured_products"]:
        for choice in product["workcenter_choices"]:
            key = (product["product"]["code"], choice["workcenter_code"])
            if key in standards:
                raise ValueError("ambiguous_capacity_standard")
            standards[key] = choice.get("time_per_unit_minutes")
    centers, missing = {}, set()
    manufactured = purchased = Decimal(0)
    for operation in changes["operations"]:
        row = operation["after"]
        if row is None:
            continue  # Canceled commitments consume no planned capacity.
        quantity = _number(row["quantity"])
        target = row["product_code"] == request["target_product_code"]
        if operation["kind"] == "manufacturing_order":
            if target:
                manufactured += quantity
            center = row["workcenter_code"]
            minutes = standards.get((row["product_code"], center))
            if minutes is None:
                centers[center] = None
                missing.add("standard_unit_minutes")
            elif centers.get(center, Decimal(0)) is not None:
                centers[center] = centers.get(center, Decimal(0)) + quantity * _number(minutes)
        elif target and row.get("supply_role") == "finished":
            purchased += quantity
    summary = {
        "schema_version": 1,
        "basis": {"plan_id": plan_id, "snapshot_sha256": snapshot_sha256,
                  "quantity_scope": "target_product", "time_basis": "standard_unit_minutes"},
        "manufactured_units": float(manufactured), "finished_purchase_units": float(purchased),
        "standard_minutes": None if missing else float(sum(centers.values(), Decimal(0))),
        "workcenters": [{"code": code, "standard_minutes": None if amount is None else float(amount)}
                        for code, amount in sorted(centers.items())],
        "missing": sorted(missing),
    }
    return project_capacity(summary, plan_id=plan_id, snapshot_sha256=snapshot_sha256)


def project_capacity(value, *, plan_id, snapshot_sha256):
    """Legacy plans omit this measurement; invalid new fields are never trusted."""
    if value is None:
        return None
    try:
        if (type(value) is not dict or set(value) != {"schema_version", "basis", "manufactured_units",
                "finished_purchase_units", "standard_minutes", "workcenters", "missing"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["basis"] != {"plan_id": plan_id, "snapshot_sha256": snapshot_sha256,
                    "quantity_scope": "target_product", "time_basis": "standard_unit_minutes"}
                or any(not isinstance(key, str) or re.fullmatch(r"[a-f0-9]{64}", key) is None
                       for key in (plan_id, snapshot_sha256))):
            raise ValueError
        _number(value["manufactured_units"])
        _number(value["finished_purchase_units"])
        rows = value["workcenters"]
        if type(rows) is not list or len(rows) > 128:
            raise ValueError
        codes, total, unknown = set(), Decimal(0), False
        for row in rows:
            if (type(row) is not dict or set(row) != {"code", "standard_minutes"}
                    or not isinstance(row["code"], str)
                    or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", row["code"]) is None
                    or row["code"] in codes):
                raise ValueError
            codes.add(row["code"])
            if row["standard_minutes"] is None:
                unknown = True
            else:
                total += _number(row["standard_minutes"])
        if unknown:
            if value["standard_minutes"] is not None or value["missing"] != ["standard_unit_minutes"]:
                raise ValueError
        elif (value["missing"] != [] or value["standard_minutes"] is None
              or abs(_number(value["standard_minutes"]) - total) > Decimal("0.000001")):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("invalid_capacity_summary") from None
    return copy.deepcopy(value)
