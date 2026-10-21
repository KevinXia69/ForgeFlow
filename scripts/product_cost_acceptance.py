"""Read-only cost audit for the authorized dedicated-sandbox acceptance tools.

Only public planning-cost facts and checks leave this function. Original ERP
rows stay private. Baselines are independently priced from captured PO lines
and assigned workcenters, not copied from the report being checked. No ERP,
approval, solver, credential or reference-solution operation is performed.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, localcontext
from pathlib import Path

from forgeflow.planning.live import _metadata
from forgeflow.recovery.product_costs import project_cost_report
from forgeflow.recovery.service import checked_id
from forgeflow.recovery.storage import digest, read_json


def need(value, code="cost_acceptance_mismatch"):
    if not value:
        raise RuntimeError(code)


def relation(value):
    return value[0] if isinstance(value, list) else value


def audit_plan_costs(store: Path, public_plan: dict) -> dict:
    with localcontext() as context:
        context.prec = 80
        return _audit_plan_costs(store, public_plan)


def _audit_plan_costs(store: Path, public_plan: dict) -> dict:
    plan_id = checked_id(public_plan["plan_id"])
    directory = store / plan_id
    need(directory.is_dir() and not directory.is_symlink(), "cost_plan_directory_invalid")
    manifest = read_json(directory / "manifest.json")
    need(digest(manifest) == plan_id and "product" in manifest, "cost_manifest_not_bound")
    artifacts = {name: read_json(directory / f"{name}.json")
                 for name in ("request", "snapshot", "blueprint", "change-set", "plan")}
    need(all(digest(value) == manifest["artifacts"].get(name) for name, value in artifacts.items()),
         "cost_source_not_bound")
    report = project_cost_report(read_json(directory / "product-costs.json"))
    need(report is not None and public_plan.get("costs") == report
         and digest(report) == manifest.get("product_costs_sha256"), "cost_public_report_not_bound")
    request, snapshot, changes = (artifacts[name] for name in ("request", "snapshot", "change-set"))
    models = {model: {row["id"]: row for row in rows} for model, rows in snapshot["models"].items()}
    need(report["basis"]["snapshot_sha256"] == public_plan["snapshot_sha256"]
         == manifest["artifacts"]["snapshot"], "cost_snapshot_not_bound")
    need(report["basis"]["as_of"] == snapshot["collected_at_utc"] and report["comparable"],
         "cost_observation_incomplete")
    need(report["basis"]["sales_order_ids"] == sorted(request["sales_order_ids"])
         and report["basis"]["target_product_code"] == request["target_product_code"]
         and report["basis"]["anchor_date"] == request["anchor_date"], "cost_scope_not_bound")
    for sid in request["sales_order_ids"]:
        sale = models["sale.order"][sid]
        need(relation(sale["company_id"]) == report["basis"]["company_id"]
             and models["res.currency"][relation(sale["currency_id"])]["name"] == "USD")
    centers = {row["code"]: row for row in models["mrp.workcenter"].values()}
    projected = {row["target"]["token"]: row for kind in ("purchase_orders", "manufacturing_orders")
                 for row in changes["projected_state"][kind]}
    totals = {side: {kind: Decimal(0) for kind in ("purchase_order", "manufacturing_order")}
              for side in ("before", "after")}
    additions = {"new_purchase_value": Decimal(0), "purchase_increase": Decimal(0),
                 "new_manufacturing_standard": Decimal(0)}
    operations = changes["operations"]
    need(len(report["lines"]) == len(operations))
    reports = {line["document"]: line for line in report["lines"]}
    need(len(reports) == len(operations))

    def price(kind, raw):
        product = models["product.product"][relation(raw["product_id"])]
        quantity = Decimal(str(raw["quantity"]))
        unit = relation(raw["product_uom_id"])
        need(unit == relation(product["uom_id"]))
        if kind == "purchase_order":
            amount = Decimal(str(raw["unit_price"]))
        else:
            _, standards = _metadata(centers[raw["workcenter_code"]])
            amount = Decimal(str(standards[product["default_code"]]["unit_cost"]))
        return {"quantity": quantity, "unit_price": amount, "amount": quantity * amount,
                "uom_id": unit, "product_code": product["default_code"]}

    for operation in operations:
        target, kind, action = operation["target"], operation["kind"], operation["action"]
        line = reports[target["name"] or target["token"]]
        need(line["kind"] == kind and line["action"] == action)
        before, after = None, None
        if target["id"] is not None:
            original = models[target["model"]][target["id"]]
            need(relation(original["company_id"]) == report["basis"]["company_id"])
            if kind == "purchase_order":
                rows = [row for row in models["purchase.order.line"].values()
                        if relation(row["order_id"]) == target["id"] and not row["display_type"]]
                need(len(rows) == 1 and models["res.currency"][relation(original["currency_id"])]["name"] == "USD")
                raw = rows[0]
                before = price(kind, {**raw, "quantity": raw["product_qty"], "unit_price": raw["price_unit"]})
            else:
                work = [row for row in models["mrp.workorder"].values()
                        if relation(row["production_id"]) == target["id"] and row["state"] not in ("cancel", "done")]
                need(len(work) == 1)
                center = models["mrp.workcenter"][relation(work[0]["workcenter_id"])]["code"]
                before = price(kind, {**original, "quantity": original["product_qty"], "workcenter_code": center})
        if action != "cancel":
            raw = projected[target["token"]]
            need(raw == {**operation["after"], "target": target})
            after = price(kind, raw)
        values = {}
        for side, observed in (("before", before), ("after", after)):
            if observed is None:
                need(line[side] is None)
                values[side] = Decimal(0)
                continue
            need(line[side] is not None and line["product_code"] == observed["product_code"]
                 and line[side]["uom_id"] == observed["uom_id"])
            for key in ("quantity", "unit_price", "amount"):
                need(Decimal(line[side][key]) == observed[key])
            totals[side][kind] += observed["amount"]
            values[side] = observed["amount"]
        delta = values["after"] - values["before"]
        need(Decimal(line["known_delta"]) == delta and line["cancellation_fee"] is None)
        if action == "create":
            key = "new_purchase_value" if kind == "purchase_order" else "new_manufacturing_standard"
            additions[key] += values["after"]
        elif action == "update" and kind == "purchase_order":
            additions["purchase_increase"] += max(delta, Decimal(0))
    for side, field in (("before", "baseline"), ("after", "planned")):
        for kind, key in (("purchase_order", "purchase"), ("manufacturing_order", "manufacturing")):
            need(Decimal(report[field][key]) == totals[side][kind])
        need(Decimal(report[field]["total"]) == sum(totals[side].values()))
    need(Decimal(report["known_delta"]) == sum(totals["after"].values()) - sum(totals["before"].values()))
    need(all(Decimal(report[key]) == value for key, value in additions.items()))
    need(Decimal(str(public_plan["new_spend"])) == Decimal(report["planned"]["total"]))
    need(abs(Decimal(str(artifacts["plan"]["plan"]["expected_new_spend"]))
             - Decimal(report["planned"]["total"])) <= Decimal("0.02"))
    need(report["full_delta"] is None and report["actual_cost"] is None
         and {"tax_treatment", "price_validity_dates", "actual_financials"} <= set(report["missing"]))
    return {"plan_id": plan_id, "costs_sha256": digest(report), "snapshot_sha256": report["basis"]["snapshot_sha256"],
            "pricing_sha256": report["basis"]["pricing_sha256"], "status": "independent_cost_audit_passed",
            "operation_counts": dict(Counter(operation["action"] for operation in operations)),
            "baseline": report["baseline"], "planned": report["planned"], "known_delta": report["known_delta"],
            **{key: report[key] for key in additions}, "missing": report["missing"],
            "actual_financials_verified": False, "approval_or_erp_write_called": False}
