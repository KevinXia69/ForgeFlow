"""Product planning-cost evidence from the already bound, read-only ERP facts.

Amounts are exact decimal strings: raw PO quantity × unit price plus public
per-workcenter manufacturing standards. They are neither invoices nor cash
flows. In particular, cancelling an order does not prove its liability is zero.
No extra ERP reads, financial assumptions or autonomous solver inputs are added.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from forgeflow.recovery.storage import StorageError, digest, read_json


class CostEvidenceError(ValueError):
    pass


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CostEvidenceError("product_costs_invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise CostEvidenceError("product_costs_invalid") from None
    if not result.is_finite() or result < 0:
        raise CostEvidenceError("product_costs_invalid")
    return result


def _text(value):
    return None if value is None else format(value, "f")


def _id(value):
    if isinstance(value, list) and value:
        value = value[0]
    return value if type(value) is int and value > 0 else None


def _stamp(value):
    try:
        return value if isinstance(value, str) and datetime.fromisoformat(value).tzinfo else None
    except ValueError:
        return None


def _totals(lines, side):
    result = {}
    known = Decimal(0)
    for kind, key in (("purchase_order", "purchase"), ("manufacturing_order", "manufacturing")):
        values = [line[side]["amount"] for line in lines if line["kind"] == kind and line[side] is not None]
        subtotal = sum((_number(v) for v in values if v is not None), Decimal(0))
        known += subtotal
        result[key] = _text(subtotal) if all(v is not None for v in values) else None
    result["total"] = (_text(sum((_number(result[k]) for k in ("purchase", "manufacturing")), Decimal(0)))
                       if all(result[k] is not None for k in ("purchase", "manufacturing")) else None)
    result["known_subtotal"] = _text(known)
    return result


def build_cost_report(request, snapshot, blueprint, changes):
    with localcontext() as context:
        context.prec = 80
        return project_cost_report(_build_cost_report(request, snapshot, blueprint, changes))


def _build_cost_report(request, snapshot, blueprint, changes):
    """Recompute before/after document costs; never call the original feasible.

The blueprint keeps public standards for disrupted workcenters as well as
alternatives. A missing standard/price stays unknown, including in subtotals.
Tax treatment and price validity dates were not collected: only the captured
raw-unit-price planning scope is comparable, never a tax-adjusted total.
"""
    models = snapshot["models"]
    currencies = {row["id"]: row["name"] for row in models["res.currency"]}
    selected = [row for row in models["sale.order"] if row["id"] in request["sales_order_ids"]]
    currency_ids = {_id(row["currency_id"]) for row in selected}
    companies = {_id(row["company_id"]) for row in selected}
    if (len(selected) != len(request["sales_order_ids"]) or len(currency_ids) != 1
            or len(companies) != 1 or None in companies or None in currency_ids):
        raise CostEvidenceError("product_costs_scope_mismatch")
    currency_id, company_id = next(iter(currency_ids)), next(iter(companies))
    if currencies.get(currency_id) != "USD":
        raise CostEvidenceError("product_costs_currency_unsupported")
    standards = {
        (item["product"]["code"], choice["workcenter_code"]): choice.get("unit_cost_dollars")
        for item in blueprint["manufactured_products"] for choice in item["workcenter_choices"]
    }
    products = {row["id"]: row for row in models["product.product"]}
    # A pricing version depends on the captured rates, never candidate counts,
    # allocations or quantities, so same-snapshot candidates share this basis.
    pricing = {
        "suppliers": sorted(models["product.supplierinfo"], key=lambda row: row["id"]),
        "purchase_lines": sorted(models["purchase.order.line"], key=lambda row: row["id"]),
        "standards": sorted((product, center, price) for (product, center), price in standards.items()),
        "units": sorted((pid, _id(row["uom_id"])) for pid, row in products.items()),
    }
    lines = []

    def valuation(raw, kind):
        if raw is None:
            return None
        unit = _id(raw.get("product_uom_id"))
        product = products.get(raw.get("product_id"), {})
        if (unit is None or unit != _id(product.get("uom_id"))
                or raw.get("company_id") != company_id
                or raw.get("product_code") != product.get("default_code")):
            raise CostEvidenceError("product_costs_scope_mismatch")
        quantity = _number(raw.get("quantity"))
        if quantity is None or quantity <= 0:
            raise CostEvidenceError("product_costs_invalid")
        if kind == "purchase_order":
            if raw.get("currency_id") != currency_id:
                raise CostEvidenceError("product_costs_scope_mismatch")
            price, source, reference = raw.get("unit_price"), "purchase_unit_price", raw.get("offer_key")
        else:
            price = standards.get((raw["product_code"], raw.get("workcenter_code")))
            source, reference = "public_workcenter_standard", raw.get("workcenter_code")
        price = _number(price)
        return {"quantity": _text(quantity), "unit_price": _text(price),
                "amount": _text(None if price is None else quantity * price),
                "uom_id": unit, "uom_label": (product["uom_id"][1] if isinstance(product["uom_id"], list)
                                              and len(product["uom_id"]) > 1 else None),
                "price_source": source, "price_reference": reference,
                "missing": "unit_price_missing" if price is None else None}

    seen = set()
    for operation in changes["operations"]:
        kind, action, target = operation["kind"], operation["action"], operation["target"]
        if kind not in {"purchase_order", "manufacturing_order"} or action not in {"keep", "update", "create", "cancel"}:
            raise CostEvidenceError("product_costs_invalid")
        if target["token"] in seen:
            raise CostEvidenceError("product_costs_invalid")
        seen.add(target["token"])
        before, after = valuation(operation["before"], kind), valuation(operation["after"], kind)
        if (before is None) != (action == "create") or (after is None) != (action == "cancel"):
            raise CostEvidenceError("product_costs_invalid")
        raw = operation["after"] or operation["before"]
        amounts = [None if side is not None and side["amount"] is None else
                   Decimal(0) if side is None else _number(side["amount"]) for side in (before, after)]
        lines.append({"document": target.get("name") or target["token"], "kind": kind,
                      "action": action, "product_code": raw["product_code"],
                      "before": before, "after": after,
                      "known_delta": _text(amounts[1] - amounts[0]) if None not in amounts else None,
                      "cancellation_fee": None})
    before, after = _totals(lines, "before"), _totals(lines, "after")
    as_of = _stamp(snapshot.get("collected_at_utc"))
    comparable = as_of is not None and before["total"] is not None and after["total"] is not None
    additions = {"new_purchase_value": Decimal(0), "purchase_increase": Decimal(0),
                 "new_manufacturing_standard": Decimal(0)}
    for line in lines:
        key = ("new_purchase_value" if line["kind"] == "purchase_order" else "new_manufacturing_standard")
        if line["action"] == "create":
            value = _number(line["after"]["amount"])
        elif line["action"] == "update" and line["kind"] == "purchase_order":
            key = "purchase_increase"
            value = None if line["known_delta"] is None else max(Decimal(0), Decimal(line["known_delta"]))
        else:
            continue
        additions[key] = None if additions[key] is None or value is None else additions[key] + value
    return {"schema_version": 1,
            "basis": {"snapshot_sha256": digest(snapshot),
                      "pricing_sha256": digest(pricing),
                      "as_of": as_of, "anchor_date": request["anchor_date"], "currency": "USD",
                      "company_id": company_id, "sales_order_ids": sorted(request["sales_order_ids"]),
                      "target_product_code": request["target_product_code"],
                      "tax_basis": "recorded_unit_price_no_tax_adjustment",
                      "manufacturing_basis": "planning-metadata-v1",
                      "unit_basis": "product_uom_no_conversion"},
            "lines": lines, "baseline": before, "planned": after, "comparable": comparable,
            "known_delta": _text(_number(after["total"]) - _number(before["total"])) if comparable else None,
            **{key: _text(value) for key, value in additions.items()},
            "full_delta": None, "actual_cost": None,
            "missing": ["tax_treatment", "price_validity_dates", "actual_financials"]
                       + (["cancellation_fees"] if any(line["action"] == "cancel" for line in lines) else [])
                       + (["snapshot_time"] if as_of is None else [])}


def project_cost_report(value):
    with localcontext() as context:
        context.prec = 80
        return _project_cost_report(value)


def _project_cost_report(value):
    """Strict public shape: no raw ERP rows, private paths or arbitrary metadata.

Recheck all arithmetic at the product boundary. Decimal strings retain the
unrounded calculation even when the page displays two decimal places.
"""
    if value is None:
        return None
    try:
        def shape(raw, keys):
            if not isinstance(raw, dict) or set(raw) != set(keys.split()):
                raise ValueError

        def amount(raw, signed=False):
            if raw is None:
                return None
            if not isinstance(raw, str) or not re.fullmatch(r"-?\d{1,24}(?:\.\d{1,24})?", raw):
                raise ValueError
            result = Decimal(raw)
            if not signed and result < 0:
                raise ValueError
            return result

        def label(raw):
            if not isinstance(raw, str) or not raw or len(raw) > 500 or any(ord(c) < 32 for c in raw):
                raise ValueError

        shape(value, "schema_version basis lines baseline planned comparable known_delta new_purchase_value "
              "purchase_increase new_manufacturing_standard full_delta actual_cost missing")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        basis = value["basis"]
        shape(basis, "snapshot_sha256 pricing_sha256 as_of anchor_date currency company_id "
              "sales_order_ids target_product_code tax_basis manufacturing_basis unit_basis")
        for key in ("snapshot_sha256", "pricing_sha256"):
            if not isinstance(basis[key], str) or not re.fullmatch(r"[a-f0-9]{64}", basis[key]):
                raise ValueError
        if (basis["currency"] != "USD" or _id(basis["company_id"]) != basis["company_id"]
                or basis["company_id"] is None or basis["as_of"] != _stamp(basis["as_of"])
                or basis["tax_basis"] != "recorded_unit_price_no_tax_adjustment"
                or basis["manufacturing_basis"] != "planning-metadata-v1"
                or basis["unit_basis"] != "product_uom_no_conversion"):
            raise ValueError
        label(basis["target_product_code"])
        if not isinstance(basis["anchor_date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", basis["anchor_date"]):
            raise ValueError
        date.fromisoformat(basis["anchor_date"])
        if (not isinstance(basis["sales_order_ids"], list) or not basis["sales_order_ids"]
                or any(type(i) is not int or i <= 0 for i in basis["sales_order_ids"])
                or sorted(set(basis["sales_order_ids"])) != basis["sales_order_ids"]):
            raise ValueError
        if not isinstance(value["lines"], list) or len(value["lines"]) > 2048:
            raise ValueError
        additions = {"new_purchase_value": Decimal(0), "purchase_increase": Decimal(0),
                     "new_manufacturing_standard": Decimal(0)}
        for line in value["lines"]:
            shape(line, "document kind action product_code before after known_delta cancellation_fee")
            label(line["document"])
            label(line["product_code"])
            if (line["kind"] not in {"purchase_order", "manufacturing_order"}
                    or line["action"] not in {"keep", "update", "cancel", "create"}
                    or line["cancellation_fee"] is not None):
                raise ValueError
            for side in ("before", "after"):
                raw = line[side]
                if raw is None:
                    continue
                shape(raw, "quantity unit_price amount uom_id uom_label price_source price_reference missing")
                if raw["uom_label"] is not None:
                    label(raw["uom_label"])
                qty, price = amount(raw["quantity"]), amount(raw["unit_price"])
                if qty is None or qty <= 0 or type(raw["uom_id"]) is not int or raw["uom_id"] <= 0:
                    raise ValueError
                if amount(raw["amount"]) != (None if price is None else qty * price):
                    raise ValueError
                if (raw["price_source"] != ("purchase_unit_price" if line["kind"] == "purchase_order"
                                            else "public_workcenter_standard")
                        or raw["missing"] != ("unit_price_missing" if price is None else None)):
                    raise ValueError
                if raw["price_reference"] is not None:
                    label(raw["price_reference"])
            if ((line["before"] is None) != (line["action"] == "create")
                    or (line["after"] is None) != (line["action"] == "cancel")):
                raise ValueError
            amounts = [Decimal(0) if line[side] is None else amount(line[side]["amount"])
                       for side in ("before", "after")]
            delta = None if None in amounts else amounts[1] - amounts[0]
            if amount(line["known_delta"], True) != delta:
                raise ValueError
            key, addition = None, None
            if line["action"] == "create":
                key = "new_purchase_value" if line["kind"] == "purchase_order" else "new_manufacturing_standard"
                addition = amounts[1]
            elif line["action"] == "update" and line["kind"] == "purchase_order":
                key, addition = "purchase_increase", None if delta is None else max(Decimal(0), delta)
            if key:
                additions[key] = None if additions[key] is None or addition is None else additions[key] + addition
        for field, side in (("baseline", "before"), ("planned", "after")):
            if value[field] != _totals(value["lines"], side):
                raise ValueError
        before, after = amount(value["baseline"]["total"]), amount(value["planned"]["total"])
        comparable = before is not None and after is not None and basis["as_of"] is not None
        if (value["comparable"] is not comparable
                or amount(value["known_delta"], True) != (after - before if comparable else None)
                or value["full_delta"] is not None or value["actual_cost"] is not None
                or any(amount(value[key]) != expected for key, expected in additions.items())):
            raise ValueError
        missing = ["tax_treatment", "price_validity_dates", "actual_financials"]
        if any(line["action"] == "cancel" for line in value["lines"]):
            missing.append("cancellation_fees")
        if basis["as_of"] is None:
            missing.append("snapshot_time")
        if value["missing"] != missing:
            raise ValueError
        # Every nested key has been checked; copy through JSON to detach owners.
        return copy.deepcopy(value)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise CostEvidenceError("product_costs_invalid") from None


def load_cost_report(directory: Path, manifest: dict, *, inputs=None):
    """Old plans stay readable without invented evidence; new evidence is bound.

With inputs, independently recompute before execution. An unbound stray file
is an error, not a way to retrofit old approvals with different cost claims.
"""
    path = directory / "product-costs.json"
    try:
        if "product_costs_sha256" not in manifest:
            if path.exists() or path.is_symlink():
                raise ValueError
            return None
        report = project_cost_report(read_json(path))
        if "product" not in manifest or digest(report) != manifest["product_costs_sha256"]:
            raise ValueError
        if report["basis"]["snapshot_sha256"] != manifest["artifacts"]["snapshot"]:
            raise ValueError
        if inputs is not None and report != build_cost_report(*inputs):
            raise ValueError
        return report
    except (OSError, ValueError, KeyError, TypeError, StorageError):
        raise CostEvidenceError("product_costs_binding_changed") from None
