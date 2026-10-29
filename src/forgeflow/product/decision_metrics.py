"""Bound planning headroom; no financial actuals, daily calendar or risk probability."""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation, localcontext

from forgeflow.recovery.margin import MARGIN_BASIS, decimal_amount, margin_ratio
from forgeflow.recovery.product_constraints import canonical_constraints
from forgeflow.recovery.product_costs import project_cost_report
from forgeflow.recovery.storage import digest

from .budget_checks import order_budget_facts
from .capacity import project_capacity
from .readiness import project_order_readiness


def _d(value, *, signed=False):
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 100:
        raise ValueError
    number = Decimal(str(value))
    if (not number.is_finite() or (not signed and number < 0)
            or abs(number.adjusted()) > 100 or abs(number.as_tuple().exponent) > 100):
        raise ValueError
    return number


def _s(number):
    if number is None:
        return None
    if number == 0:
        return "0"
    value = format(number, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _name(value):
    if not isinstance(value, str) or not 0 < len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise ValueError
    return value


def readiness_counts(readiness):
    rows = readiness["orders"]
    buffers = [row["buffer_days"] for row in rows]
    return {"order_count": len(rows), "zero_buffer_orders": sum(day == 0 for day in buffers),
            "late_orders": sum(row["status"] == "late" for row in rows),
            "uncovered_orders": sum(row["covered_units"] < row["demand_units"] for row in rows),
            "unknown_ready_orders": sum(day is None for day in buffers),
            "minimum_buffer_days": None if None in buffers else min(buffers)}


def _margin(revenue, cost, original, effective):
    """Historical v1 arithmetic only; never relabel it as new-spend coverage."""
    maximum = revenue * (1 - effective / 100)
    actual = None if cost is None else (revenue - cost) / revenue * 100
    return {"sales_value": _s(revenue), "planned_cost": _s(cost),
            "original_floor_pct": _s(original), "effective_floor_pct": _s(effective),
            "planned_margin_pct": None if actual is None else _s(actual.quantize(Decimal("0.0001"))),
            "headroom_pp": None if actual is None else _s((actual - effective).quantize(Decimal("0.0001"))),
            "max_planning_cost": _s(maximum), "cost_headroom": _s(None if cost is None else maximum - cost)}


def _coverage(budgets, readiness):
    """Recompute backing from the bound per-order supply quantities and prices."""
    orders = {row["order_ref"]: row for row in readiness["orders"]}
    if len(orders) != len(readiness["orders"]) or set(orders) != {row["order_ref"] for row in budgets}:
        raise ValueError
    revenue, backed_units, stock_units = Decimal(0), 0, 0
    for budget in budgets:
        order = orders[budget["order_ref"]]
        price = decimal_amount(_d(budget["sales_value"]) / order["demand_units"], cents=True)
        quantity = order["purchased_units"] + order["manufactured_units"]
        revenue += price * quantity
        backed_units += quantity
        stock_units += order["stock_units"]
    return revenue, backed_units, stock_units


def _margin_v2(sales_value, revenue, units, stock_units, cost, original, effective):
    maximum = revenue * (1 - effective / 100)
    ratio = None if cost is None else margin_ratio(revenue, cost)
    actual = None if ratio is None else ratio * 100
    return {"sales_value": _s(sales_value), "new_spend_revenue": _s(revenue),
            "new_spend_units": units, "stock_units": stock_units, "planned_cost": _s(cost),
            "original_floor_pct": _s(original), "effective_floor_pct": _s(effective),
            "planned_margin_pct": None if actual is None else _s(actual.quantize(Decimal("0.0001"))),
            "headroom_pp": None if actual is None else _s((actual - effective).quantize(Decimal("0.0001"))),
            "max_planning_cost": _s(maximum), "cost_headroom": _s(None if cost is None else maximum - cost)}


def build_decision_metrics(request, snapshot, blueprint, constraints, costs, capacity, readiness, *, plan_id, snapshot_sha256):
    """Recompute headroom from the same bound records used by independent checks."""
    try:
        with localcontext() as context:
            context.prec = 80
            if digest(snapshot) != snapshot_sha256 or costs is None or capacity is None or readiness is None:
                raise ValueError
            readiness = project_order_readiness(readiness, plan_id=plan_id, snapshot_sha256=snapshot_sha256)
            constraints = canonical_constraints(constraints, request)
            facts = order_budget_facts(request, snapshot)
            if facts is None or any(row["compliant"] is None for row in facts):
                raise ValueError
            budgets, revenue = [], Decimal(0)
            for row in facts:
                revenue += _d(row["sales_value"])
                budgets.append({"order_ref": _name(row["order_ref"]), "sales_value": row["sales_value"],
                                "customer_cap": row["customer_cap"], "headroom": row["headroom"]})
            if revenue <= 0:
                raise ValueError
            # The readiness projector checks internal conservation; the builder
            # also ties its demand and implied unit prices to actual sale lines.
            orders = {row["id"]: row for row in snapshot["models"]["sale.order"]}
            ready_by_name = {row["order_ref"]: row for row in readiness["orders"]}
            for sid in request["sales_order_ids"]:
                lines = [row for row in snapshot["models"]["sale.order.line"]
                         if (row["order_id"][0] if isinstance(row["order_id"], list)
                             else row["order_id"]) == sid and not row["display_type"]]
                if len(lines) != 1 or _d(lines[0]["product_uom_qty"]) != ready_by_name[orders[sid]["name"]]["demand_units"]:
                    raise ValueError
                decimal_amount(lines[0]["price_unit"], cents=True)
            backed_revenue, backed_units, stock_units = _coverage(budgets, readiness)
            original = _d(request["margin"]) * 100
            effective = max(original, _d(constraints.get("minimum_margin_pct", original)))
            limits = {row["code"]: row.get("capacity_minutes") for row in blueprint["workcenters"]}
            if len(limits) != len(blueprint["workcenters"]):
                raise ValueError
            centers = []
            for row in capacity["workcenters"]:
                code = row["code"]
                limit = None if limits.get(code) is None else _d(limits[code])
                used = None if row["standard_minutes"] is None else _d(row["standard_minutes"])
                centers.append({"code": code, "used_standard_minutes": _s(used), "horizon_limit_minutes": _s(limit),
                                "headroom_minutes": _s(None if used is None or limit is None else limit - used)})
            total = costs["planned"]["total"]
            result = {"schema_version": 2, "basis": {"plan_id": plan_id, "snapshot_sha256": snapshot_sha256,
                "currency": costs["basis"]["currency"], "pricing_sha256": costs["basis"]["pricing_sha256"],
                "cost_basis": "recorded_unit_price_and_manufacturing_standard", "capacity_basis": "planning_horizon_standard_minutes",
                "margin_basis": MARGIN_BASIS},
                "margin": _margin_v2(revenue, backed_revenue, backed_units, stock_units,
                    None if total is None else _d(total), original, effective),
                "order_budgets": sorted(budgets, key=lambda row: row["order_ref"]),
                "workcenters": centers, "readiness": readiness_counts(readiness)}
            return project_decision_metrics(result, plan_id=plan_id, snapshot_sha256=snapshot_sha256,
                                            costs=costs, capacity=capacity, readiness=readiness)
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise ValueError("invalid_decision_metrics") from None


def project_decision_metrics(value, *, plan_id, snapshot_sha256, costs, capacity, readiness):
    if value is None:
        return None
    try:
        with localcontext() as context:
            context.prec = 80
            costs = project_cost_report(costs)
            capacity = project_capacity(capacity, plan_id=plan_id, snapshot_sha256=snapshot_sha256)
            readiness = project_order_readiness(readiness, plan_id=plan_id, snapshot_sha256=snapshot_sha256)
            if (costs is None or capacity is None or readiness is None
                    or type(value) is not dict or set(value) != {"schema_version", "basis", "margin", "order_budgets", "workcenters", "readiness"}
                    or type(value["schema_version"]) is not int or value["schema_version"] not in (1, 2)
                    or any(not isinstance(key, str) or re.fullmatch(r"[a-f0-9]{64}", key) is None for key in (plan_id, snapshot_sha256))
                    or costs["basis"]["snapshot_sha256"] != snapshot_sha256
                    or value["basis"] != {"plan_id": plan_id, "snapshot_sha256": snapshot_sha256, "currency": costs["basis"]["currency"],
                        "pricing_sha256": costs["basis"]["pricing_sha256"], "cost_basis": "recorded_unit_price_and_manufacturing_standard",
                        "capacity_basis": "planning_horizon_standard_minutes",
                        **({"margin_basis": MARGIN_BASIS} if value["schema_version"] == 2 else {})}):
                raise ValueError
            rows, revenue, names = value["order_budgets"], Decimal(0), set()
            if type(rows) is not list or not 0 < len(rows) <= 128:
                raise ValueError
            for row in rows:
                if type(row) is not dict or set(row) != {"order_ref", "sales_value", "customer_cap", "headroom"}:
                    raise ValueError
                name = _name(row["order_ref"])
                if (name in names or any(row[key] != _s(_d(row[key])) for key in ("sales_value", "customer_cap"))
                        or row["headroom"] != _s(_d(row["customer_cap"]) - _d(row["sales_value"]))):
                    raise ValueError
                names.add(name)
                revenue += _d(row["sales_value"])
            if revenue <= 0 or names != {row["order_ref"] for row in readiness["orders"]}:
                raise ValueError
            margin = value["margin"]
            original, effective = _d(margin["original_floor_pct"]), _d(margin["effective_floor_pct"])
            if not 0 <= original <= effective < 100:
                raise ValueError
            cost = costs["planned"]["total"]
            if value["schema_version"] == 2:
                backed_revenue, backed_units, stock_units = _coverage(rows, readiness)
                if any(type(margin.get(key)) is not int for key in ("new_spend_units", "stock_units")):
                    raise ValueError
                expected_margin = _margin_v2(revenue, backed_revenue, backed_units, stock_units,
                    None if cost is None else _d(cost), original, effective)
            else:
                expected_margin = _margin(revenue, None if cost is None else _d(cost), original, effective)
            if margin != expected_margin:
                raise ValueError
            centers = value["workcenters"]
            if type(centers) is not list or len(centers) != len(capacity["workcenters"]):
                raise ValueError
            for row, cap in zip(centers, capacity["workcenters"], strict=True):
                if type(row) is not dict or set(row) != {"code", "used_standard_minutes", "horizon_limit_minutes", "headroom_minutes"}:
                    raise ValueError
                used = None if cap["standard_minutes"] is None else _d(cap["standard_minutes"])
                limit = None if row["horizon_limit_minutes"] is None else _d(row["horizon_limit_minutes"])
                if (row["code"] != cap["code"] or row["used_standard_minutes"] != _s(used)
                        or row["horizon_limit_minutes"] != _s(limit)
                        or row["headroom_minutes"] != _s(None if used is None or limit is None else limit - used)):
                    raise ValueError
            if (value["readiness"] != readiness_counts(readiness)
                    or any(type(v) is not int and v is not None for v in value["readiness"].values())):
                raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise ValueError("invalid_decision_metrics") from None
    return copy.deepcopy(value)
