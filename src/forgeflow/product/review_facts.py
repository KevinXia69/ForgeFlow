"""Bound business fields used to inspect a recovery choice or verify a disputed deadline."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from forgeflow.recovery.storage import digest

from .artifacts import prepared_directory
from .executor import ProductExecutionError


def _relation(value):
    return value[0] if isinstance(value, list) and value else value


def _row(model, record, fields):
    return {"model": model, "id": record["id"], "fields": {
        name: _relation(record[name]) for name in fields if name in record}}


def check_confirmed_supply(snapshot, blueprint, changes, subject):
    """Require the notified purchase, its open line, and its cancellation in the bound plan."""
    tables = snapshot["models"]
    orders = [row for row in tables["purchase.order"] if row["id"] == subject["purchase_order_id"]
              and _relation(row["partner_id"]) == subject["supplier_id"]
              and row["state"] in {"purchase", "done"}]
    lines = [row for row in tables["purchase.order.line"] if row["id"] == subject["purchase_line_id"]
             and _relation(row["order_id"]) == subject["purchase_order_id"]
             and _relation(row["product_id"]) == subject["product_id"]
             and row["product_qty"] > row["qty_received"]]
    repair = blueprint["repair_context"]
    broken = repair.get("broken_supplier_offer_key")
    lane = [row["ref"] for row in repair["seeded_purchase_orders"] if row["offer_key"] == broken]
    cancelled = [op["target"] for op in changes["operations"]
                 if op["action"] == "cancel" and op["kind"] == "purchase_order"]
    if any(target["id"] != subject["purchase_order_id"] for target in cancelled):
        raise ProductExecutionError("additional_purchase_cancellation_unsupported")
    if (len(orders) != 1 or len(lines) != 1 or lane != [orders[0]["name"]]
            or repair["broken_purchase_order_refs"] != lane or len(cancelled) != 1
            or cancelled[0]["name"] != orders[0]["name"]):
        raise ProductExecutionError("review_notice_line_mismatch")


def check_live_purchase(source, snapshot, subject):
    """Use the whole notified PO: cancelling its header would also cancel any newly added line."""
    from forgeflow.investigation.evidence import related

    po_id = str(subject["purchase_order_id"])
    headers = source.query("record", {"entity": "purchase_order", "id": po_id})["records"]
    if len(headers) != 1:
        raise ProductExecutionError("review_notice_line_mismatch")
    fields = headers[0]["fields"]
    tables = snapshot["models"]
    saved = next(row for row in tables["purchase.order"] if row["id"] == subject["purchase_order_id"])
    if (fields["state"] != saved["state"] or fields["name"] != saved["name"]
            or fields["supplier_id"] != str(subject["supplier_id"])):
        raise ProductExecutionError("review_notice_line_mismatch")
    lines = [row for row in related(source, "purchase_order", po_id) if row["entity"] == "purchase_order_line"]
    saved_line = next(row for row in tables["purchase.order.line"] if row["id"] == subject["purchase_line_id"])
    if len(lines) != 1 or lines[0]["id"] != str(subject["purchase_line_id"]):
        raise ProductExecutionError("review_notice_line_mismatch")
    line = lines[0]["fields"]
    if (line["order_id"] != po_id or line["product_id"] != str(subject["product_id"])
            or line["quantity"] != saved_line["product_qty"] or line["unit_price"] != saved_line["price_unit"]
            or line["received_quantity"] != saved_line["qty_received"]):
        raise ProductExecutionError("review_notice_line_mismatch")


def review_sources(executor, case, plan):
    executor._require_binding(case, plan)
    with prepared_directory(executor, case, plan["plan_id"]) as directory:
        manifest = executor._json(directory / "manifest.json")
        snapshot = executor._json(directory / "snapshot.json")
        blueprint = executor._json(directory / "blueprint.json")
    if (digest(manifest) != plan["plan_id"] or digest(snapshot) != plan["snapshot_sha256"]
            or manifest["artifacts"]["snapshot"] != plan["snapshot_sha256"]
            or digest(blueprint) != manifest["artifacts"]["blueprint"]):
        raise ProductExecutionError("plan_not_bound")
    tables = snapshot["models"]
    orders = set(executor.request["sales_order_ids"])
    products = {r["id"] for r in tables["product.product"]}
    productions = {r["id"] for r in tables["mrp.production"] if _relation(r["product_id"]) in products}
    sources = []
    for name, fields in [
        ("sale.order", ("name", "commitment_date", "state", "currency_id")),
        ("sale.order.line", ("order_id", "product_id", "product_uom_qty", "product_uom_id", "qty_delivered", "price_unit")),
        ("purchase.order", ("name", "partner_id", "state", "currency_id")),
        ("purchase.order.line", ("order_id", "product_id", "product_qty", "product_uom_id", "price_unit", "qty_received", "date_planned")),
        ("mrp.production", ("name", "product_id", "product_qty", "product_uom_id", "qty_produced", "state", "date_start", "date_deadline")),
        ("product.product", ("default_code", "product_tmpl_id", "uom_id")),
        ("mrp.bom", ("product_id", "product_tmpl_id", "product_qty", "product_uom_id", "produce_delay")),
        ("mrp.bom.line", ("bom_id", "product_id", "product_qty", "product_uom_id")),
        ("mrp.routing.workcenter", ("name", "bom_id", "workcenter_id")),
        ("mrp.workorder", ("name", "production_id", "workcenter_id", "state", "product_id")),
        ("product.supplierinfo", ("partner_id", "product_tmpl_id", "price", "delay", "currency_id")),
    ]:
        for row in tables[name]:
            if name == "sale.order" and row["id"] not in orders:
                continue
            if name == "sale.order.line" and _relation(row["order_id"]) not in orders:
                continue
            if name in {"mrp.production", "purchase.order.line"} and _relation(row["product_id"]) not in products:
                continue
            if name == "mrp.workorder" and _relation(row["production_id"]) not in productions:
                continue
            sources.append(_row(name, row, fields))
    # The solver validated these unit standards against the selected centre's
    # planning metadata. Free-form notes and other centres are never disclosed.
    centers = {c["code"]: c["id"] for c in tables["mrp.workcenter"]}
    for product in blueprint["manufactured_products"]:
        for choice in product["workcenter_choices"]:
            code = choice["workcenter_code"]
            sources.append({"model": "mrp.workcenter", "id": centers[code], "fields": {
                "code": code, "source_field": "note", "product_code": product["product"]["code"],
                "standard_USD_per_unit": choice["unit_cost_dollars"],
                "standard_minutes_per_unit": choice["time_per_unit_minutes"],
                "lead_days": choice["lead_time_days"]}})
    return {"plan_id": plan["plan_id"], "snapshot_sha256": plan["snapshot_sha256"],
            "captured_at": snapshot["collected_at_utc"], "rows": sources}


def check_deadline(executor, case, plan, order_id):
    sources = review_sources(executor, case, plan)
    prior = next((r["fields"].get("commitment_date") for r in sources["rows"]
                  if r["model"] == "sale.order" and r["id"] == order_id), None)
    if not isinstance(prior, str) or order_id not in executor.request["sales_order_ids"]:
        raise ProductExecutionError("review_order_unavailable")
    rows = executor.review_connection().get_model("sale.order").read(
        ids=[order_id], fields=["id", "commitment_date", "write_date"])
    if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("id") != order_id
            or not isinstance(rows[0].get("commitment_date"), str)
            or not isinstance(rows[0].get("write_date"), str)):
        raise ProductExecutionError("review_order_unavailable")
    old_day = datetime.fromisoformat(prior).date()
    new_day = datetime.fromisoformat(rows[0]["commitment_date"]).date()
    return {"model": "sale.order", "id": order_id, "field": "commitment_date",
            "prepared_value": prior, "current_value": rows[0]["commitment_date"],
            "current_write_date": rows[0]["write_date"], "snapshot_sha256": sources["snapshot_sha256"],
            "changed": old_day != new_day}


def compare_candidates(plans):
    """Summarize the two actually prepared alternatives on one priced baseline."""
    by_objective = {plan.get("objective"): plan for plan in plans}
    if set(by_objective) != {"preserve_quantities", "min_planned_cost"}:
        return None
    quantity = by_objective["preserve_quantities"]["costs"]
    cheap = by_objective["min_planned_cost"]["costs"]
    if quantity["basis"] != cheap["basis"] or quantity["baseline"] != cheap["baseline"]:
        raise ValueError("review_baseline_mismatch")
    def existing_lines(costs):
        return {(row["kind"], row["document"], row["product_code"]): row
                for row in costs["lines"] if row["before"] is not None}
    left, right = existing_lines(quantity), existing_lines(cheap)
    if left.keys() != right.keys() or any(left[k]["before"] != right[k]["before"] for k in left):
        raise ValueError("review_baseline_mismatch")
    differences = []
    for key in left:
        a, b = left[key], right[key]
        if a["action"] != b["action"] or a["after"] != b["after"]:
            differences.append({"kind": a["kind"], "document": a["document"], "product_code": a["product_code"],
                "baseline": a["before"]["quantity"], "unit": a["before"]["uom_label"] or a["before"]["uom_id"],
                "preserve": {"action": a["action"], "quantity": a["after"]["quantity"] if a["after"] else "0"},
                "low_cost": {"action": b["action"], "quantity": b["after"]["quantity"] if b["after"] else "0"}})
    delta = None
    if quantity["comparable"] and cheap["comparable"]:
        delta = format(Decimal(quantity["planned"]["total"]) - Decimal(cheap["planned"]["total"]), "f")
    return {"currency": quantity["basis"]["currency"], "preserve_minus_low_cost": delta, "differences": differences}
