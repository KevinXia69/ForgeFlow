"""Per-order customer caps from immutable inputs, never purchasing authority."""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation, localcontext

from forgeflow.recovery.storage import digest

BUDGET_BASIS_KIND = "sales_order_value_per_customer_cap_v1"


def _amount(value):
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 100:
        raise ValueError
    number = Decimal(str(value))
    if (not number.is_finite() or number < 0 or abs(number.adjusted()) > 100
            or abs(number.as_tuple().exponent) > 100):
        raise ValueError
    return number


def _text(number):
    if number == 0:
        return "0"
    value = format(number, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def order_budget_facts(request, snapshot):
    """Return request-ordered facts; None scope/amounts remain unknown.

    A usable order has exactly one recorded order and one non-display sales
    line, matching the independent contract. Missing data cannot imply zero.
    Names are optional here because the budget predicate depends on IDs; the
    decision-metrics projection additionally requires safe, unique names.
    """
    ids = request.get("sales_order_ids") if isinstance(request, dict) else None
    if (type(ids) is not list or not 0 < len(ids) <= 128
            or any(type(sid) is not int or sid <= 0 for sid in ids) or len(set(ids)) != len(ids)):
        return None
    models = snapshot.get("models") if isinstance(snapshot, dict) else None
    models = models if isinstance(models, dict) else {}
    orders, lines = models.get("sale.order"), models.get("sale.order.line")
    caps = request.get("sales_order_budget_caps")
    result = []
    with localcontext() as context:
        context.prec = 420
        for sid in ids:
            fact = {"order_ref": None, "sales_value": None, "customer_cap": None,
                    "headroom": None, "compliant": None}
            try:
                if type(orders) is not list or type(lines) is not list or type(caps) is not dict:
                    raise ValueError
                selected = [row for row in orders if type(row) is dict and type(row.get("id")) is int and row["id"] == sid]
                if len(selected) != 1:
                    raise ValueError
                name = selected[0].get("name")
                if isinstance(name, str) and 0 < len(name) <= 128 and not any(ord(c) < 32 for c in name):
                    fact["order_ref"] = name
                relevant = []
                for row in lines:
                    if type(row) is not dict:
                        raise ValueError
                    order_id = row.get("order_id")
                    order_id = order_id[0] if isinstance(order_id, list) and order_id else order_id
                    if type(order_id) is int and order_id == sid:
                        # An absent display marker cannot establish a product line.
                        if "display_type" not in row:
                            raise ValueError
                        if not row["display_type"]:
                            relevant.append(row)
                if len(relevant) != 1:
                    raise ValueError
                value = _amount(relevant[0]["product_uom_qty"]) * _amount(relevant[0]["price_unit"])
                cap = _amount(caps[str(sid)])
                fact.update(sales_value=_text(value), customer_cap=_text(cap),
                            headroom=_text(cap - value), compliant=value <= cap)
            except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
                pass
            result.append(fact)
    return result


def project_budget_check(check, *, plan_id, snapshot_sha256):
    """Validate explicit new semantics; an absent marker stays legacy unchanged."""
    if not isinstance(check, dict):
        raise ValueError("invalid_budget_check")
    if "basis" not in check:
        return copy.deepcopy(check)
    basis = {"kind": BUDGET_BASIS_KIND, "plan_id": plan_id, "snapshot_sha256": snapshot_sha256}
    actual, threshold, status = check.get("actual"), check.get("threshold"), check.get("status")
    valid_count = type(threshold) is int and 0 < threshold <= 128
    if (check.get("code") != "budget" or check["basis"] != basis
            or any(type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None
                   for value in (plan_id, snapshot_sha256))
            or not (
                (status == "pass" and valid_count and type(actual) is int and actual == threshold)
                or (status == "block" and valid_count and type(actual) is int and 0 <= actual < threshold)
                or (status == "unknown" and actual is None and (threshold is None or valid_count))
            )):
        raise ValueError("invalid_budget_check")
    return copy.deepcopy(check)


def build_budget_check(request, snapshot, *, plan_id, snapshot_sha256):
    """Count each selected order against its own cap using bound source records."""
    if digest(snapshot) != snapshot_sha256:
        raise ValueError("invalid_budget_check")
    facts = order_budget_facts(request, snapshot)
    statuses = [] if facts is None else [row["compliant"] for row in facts]
    status = "block" if False in statuses else "unknown" if not statuses or None in statuses else "pass"
    result = {"code": "budget", "label": "Sales-order values within individual customer caps",
              "status": status, "actual": None if status == "unknown" else sum(value is True for value in statuses),
              "threshold": None if facts is None else len(facts),
              "source": "bound sales-order quantity times unit price compared with each customer cap; not purchase authorization",
              "basis": {"kind": BUDGET_BASIS_KIND, "plan_id": plan_id, "snapshot_sha256": snapshot_sha256}}
    return project_budget_check(result, plan_id=plan_id, snapshot_sha256=snapshot_sha256)
