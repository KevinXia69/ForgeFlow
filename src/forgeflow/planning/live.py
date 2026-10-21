"""Read-only, deliberately bounded Odoo -> public procurement solver mapping.

Inputs are live JSON-2 records and an explicit business request. This module never
reads task files, reference plans, seeds, credentials, or verifier state, and never
calls a write method. Unsupported/ambiguous planning facts fail closed.
"""

from __future__ import annotations

import html
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

MARKER = "[ForgeFlow planning-metadata-v1]"


class LiveInputError(ValueError):
    """Business data is missing, ambiguous, or outside the supported model."""

    def __init__(
        self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        # Optional metadata is not automatically safe to expose. The CLI
        # separately allowlists and validates the complete error protocol.
        self.code = code
        self.details = details


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise LiveInputError(message)


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    _need(
        isinstance(value, (int, float)) and not isinstance(value, bool), f"{label}: number required"
    )
    _need(
        math.isfinite(value) and (value > 0 if positive else value >= 0), f"{label}: invalid value"
    )
    return float(value)


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    number = _number(value, label, positive=positive)
    _need(number.is_integer(), f"{label}: fractional quantities/days are unsupported")
    return int(number)


def _relation(value: Any) -> int | None:
    if value is False or value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    _need(isinstance(value, int) and not isinstance(value, bool), "Malformed Odoo relation")
    return value


def _plain(value: Any) -> str:
    if not value:
        return ""
    _need(isinstance(value, str), "Business note must be text")
    # Preserve JSON lines that Odoo's HTML note field wraps in paragraph/BR tags.
    return html.unescape(re.sub(r"<[^>]+>", "\n", value))


def _day(value: Any, label: str) -> date:
    _need(isinstance(value, str) and bool(value), f"{label}: date required")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise LiveInputError(f"{label}: invalid ISO date") from exc


def validate_request(request: dict) -> dict:
    """Validate the entire request before it is persisted or used for RPC.

    Errors describe the expected schema rather than echoing unknown keys/values.
    No filesystem, network, or mutation occurs here.
    """
    _need(isinstance(request, dict), "Request must be an object")
    required = {
        "target_product_code",
        "sales_order_ids",
        "anchor_date",
        "margin",
        "sales_order_budget_caps",
        "disruption",
    }
    _need(set(request) == required, f"Request fields must be exactly {sorted(required)}")
    _need(
        isinstance(request["target_product_code"], str) and bool(request["target_product_code"]),
        "target_product_code required",
    )
    ids = request["sales_order_ids"]
    _need(isinstance(ids, list) and bool(ids), "Explicit sales_order_ids required")
    _need(
        all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids)
        and len(set(ids)) == len(ids),
        "sales_order_ids must be unique positive integers",
    )
    anchor = _day(request["anchor_date"], "anchor_date")
    _need(request["anchor_date"] == anchor.isoformat(), "anchor_date must use YYYY-MM-DD")
    margin = _number(request["margin"], "margin")
    _need(margin < 1, "margin must be a fraction below 1")
    caps = request["sales_order_budget_caps"]
    _need(
        isinstance(caps, dict) and set(caps) == {str(i) for i in ids},
        "An explicit sales_order_budget_caps value is required for every SO id",
    )
    for key, value in caps.items():
        _number(value, f"budget cap for SO {key}", positive=True)
    disruption = request["disruption"]
    _need(isinstance(disruption, dict), "disruption object required")
    kind = disruption.get("kind")
    if kind == "supplier_cancellation":
        _need(
            set(disruption) in ({"kind", "vendor_ref"}, {"kind", "vendor_ref", "offer_key"}),
            "Supplier disruption requires vendor_ref and optional offer_key",
        )
        _need(
            isinstance(disruption.get("vendor_ref"), str) and bool(disruption["vendor_ref"]),
            "Supplier vendor_ref required",
        )
        if "offer_key" in disruption:
            _need(
                isinstance(disruption["offer_key"], str) and bool(disruption["offer_key"]),
                "offer_key must be nonempty text",
            )
    else:
        _need(
            kind == "workcenter_outage"
            and set(disruption) == {"kind", "workcenter_code"}
            and isinstance(disruption["workcenter_code"], str)
            and bool(disruption["workcenter_code"]),
            "Workcenter disruption requires workcenter_code",
        )
    return request


# Only these fields are requested; no full-record or full-schema dumps. A tuple
# lists canonical name followed by older supported field names, discovered first.
FIELDS = {
    "product.product": [
        "id",
        "name",
        "default_code",
        "product_tmpl_id",
        "list_price",
        "standard_price",
        "type",
        "is_storable",
        "route_ids",
        "uom_id",
    ],
    "sale.order": [
        "id",
        "name",
        "partner_id",
        "state",
        "commitment_date",
        "currency_id",
        "company_id",
        "client_order_ref",
    ],
    "sale.order.line": [
        "id",
        "order_id",
        "product_id",
        "product_uom_qty",
        "price_unit",
        "qty_delivered",
        "discount",
        "display_type",
        ("product_uom_id", "product_uom"),
    ],
    "res.partner": ["id", "name", "ref", "email", "comment", "supplier_rank"],
    "mrp.bom": [
        "id",
        "product_id",
        "product_tmpl_id",
        "product_qty",
        "product_uom_id",
        "type",
        "produce_delay",
        "active",
    ],
    "mrp.bom.line": ["id", "bom_id", "product_id", "product_qty", "product_uom_id"],
    "mrp.routing.workcenter": ["id", "bom_id", "name", "workcenter_id", "sequence"],
    "mrp.workcenter": [
        "id",
        "name",
        "code",
        "note",
        "alternative_workcenter_ids",
        "time_efficiency",
        "time_start",
        "time_stop",
    ],
    "product.supplierinfo": [
        "id",
        "partner_id",
        "product_id",
        "product_tmpl_id",
        "price",
        "min_qty",
        "delay",
        "product_name",
        "currency_id",
        "company_id",
    ],
    "purchase.order": ["id", "name", "partner_id", "state", "origin", "currency_id", "company_id"],
    "purchase.order.line": [
        "id",
        "order_id",
        "product_id",
        "product_qty",
        "price_unit",
        "qty_received",
        "date_planned",
        "display_type",
        ("product_uom_id", "product_uom"),
    ],
    "mrp.production": [
        "id",
        "name",
        "product_id",
        "product_qty",
        "product_uom_id",
        "state",
        "origin",
        "date_start",
        "date_deadline",
        "qty_produced",
        "bom_id",
        "company_id",
    ],
    "mrp.workorder": ["id", "name", "production_id", "workcenter_id", "state", "product_id"],
    "stock.quant": [
        "id",
        "product_id",
        "location_id",
        "quantity",
        "reserved_quantity",
        "company_id",
    ],
    "stock.location": ["id", "usage"],
    "stock.route": ["id", "name"],
    "res.currency": ["id", "name"],
}


def _records(value: Any, model: str) -> list[dict]:
    if isinstance(value, dict):
        value = [value]
    _need(
        isinstance(value, list) and all(isinstance(r, dict) for r in value),
        f"{model}: malformed read() response",
    )
    _need(
        all(isinstance(r.get("id"), int) and not isinstance(r["id"], bool) for r in value),
        f"{model}: every record must have an integer id",
    )
    _need(len({r["id"] for r in value}) == len(value), f"{model}: duplicate record IDs")
    return value


def collect_snapshot(client: Any, request: dict) -> dict:
    """Read a bounded multi-level BoM closure through odoolib's JSON-2 client.

    All model interactions are fields_get, search, or read. The client is supplied
    by the caller; this function does not create clients or read any credentials.
    Active MO/WO inventory is global so shared-center interference is detectable.
    """
    request = validate_request(request)
    models: dict[str, dict[int, dict]] = defaultdict(dict)
    schemas: dict[str, dict[str, str]] = {}

    def fetch(model: str, *, domain=None, ids=None) -> list[dict]:
        proxy = client.get_model(model)
        if model not in schemas:
            fields = FIELDS[model]
            candidates = [
                n for item in fields for n in (item if isinstance(item, tuple) else (item,))
            ]
            schema = proxy.fields_get(allfields=candidates, attributes=["type", "relation"])
            _need(isinstance(schema, dict), f"{model}: malformed fields_get")
            selected = {}
            for item in fields:
                choices = item if isinstance(item, tuple) else (item,)
                found = next((n for n in choices if n in schema), None)
                _need(found is not None, f"{model}: required field {choices[0]} is unavailable")
                selected[choices[0]] = found
            schemas[model] = selected
        if ids is None:
            ids = proxy.search(domain=domain)
        _need(
            isinstance(ids, list) and all(isinstance(i, int) for i in ids),
            f"{model}: malformed search() IDs",
        )
        if not ids:
            models[model]  # Keep an explicitly collected empty table.
            return []
        selected = schemas[model]
        rows = _records(proxy.read(ids=ids, fields=list(selected.values())), model)
        _need({r["id"] for r in rows} == set(ids), f"{model}: incomplete read() result")
        normalized = []
        for row in rows:
            _need(all(actual in row for actual in selected.values()), f"{model}: incomplete record")
            normalized.append({canonical: row[actual] for canonical, actual in selected.items()})
        models[model].update({r["id"]: r for r in normalized})
        return normalized

    fetch("sale.order", ids=request["sales_order_ids"])
    target = fetch(
        "product.product", domain=[("default_code", "=", request["target_product_code"])]
    )
    _need(len(target) == 1, "Target SKU must identify exactly one live product")
    seen: set[int] = set()
    pending = {target[0]["id"]}
    while pending:
        _need(len(seen) + len(pending) <= 500, "BoM closure exceeds supported 500-product bound")
        products = fetch("product.product", ids=sorted(pending))
        seen.update(pending)
        template_ids = sorted({_relation(p["product_tmpl_id"]) for p in products})
        boms = fetch(
            "mrp.bom", domain=[("product_tmpl_id", "in", template_ids), ("active", "=", True)]
        )
        boms = [
            b
            for b in boms
            if not _relation(b["product_id"]) or _relation(b["product_id"]) in pending
        ]
        lines = fetch("mrp.bom.line", domain=[("bom_id", "in", [b["id"] for b in boms])])
        fetch("mrp.routing.workcenter", domain=[("bom_id", "in", [b["id"] for b in boms])])
        pending = {_relation(line["product_id"]) for line in lines} - seen
        _need(None not in pending, "BoM line missing component product")
    template_ids = sorted(
        {_relation(models["product.product"][pid]["product_tmpl_id"]) for pid in seen}
    )
    fetch("product.supplierinfo", domain=[("product_tmpl_id", "in", template_ids)])
    # Include any competing sales demand for closure products, not just selected SOs.
    lines = fetch("sale.order.line", domain=[("product_id", "in", sorted(seen))])
    order_ids = sorted({_relation(r["order_id"]) for r in lines})
    fetch("sale.order", ids=order_ids)
    fetch("sale.order.line", domain=[("order_id", "in", request["sales_order_ids"])])
    po_lines = fetch("purchase.order.line", domain=[("product_id", "in", sorted(seen))])
    po_ids = sorted({_relation(r["order_id"]) for r in po_lines})
    fetch("purchase.order", ids=po_ids)
    fetch("purchase.order.line", domain=[("order_id", "in", po_ids)])
    fetch("mrp.production", domain=[("state", "not in", ["done", "cancel"])])
    fetch("mrp.workorder", domain=[("state", "not in", ["done", "cancel"])])
    fetch("mrp.workcenter", domain=[])
    quants = fetch("stock.quant", domain=[("product_id", "in", sorted(seen))])
    fetch("stock.location", ids=sorted({_relation(q["location_id"]) for q in quants}))
    partner_ids = {
        _relation(r["partner_id"])
        for model in ("sale.order", "purchase.order", "product.supplierinfo")
        for r in models[model].values()
    }
    fetch("res.partner", ids=sorted(partner_ids - {None}))
    route_ids = {i for p in models["product.product"].values() for i in p["route_ids"]}
    fetch("stock.route", ids=sorted(route_ids))
    currency_ids = {
        _relation(r["currency_id"])
        for model in ("sale.order", "purchase.order", "product.supplierinfo")
        for r in models[model].values()
    }
    fetch("res.currency", ids=sorted(currency_ids - {None}))
    return {
        "schema_version": 1,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "models": {name: list(rows.values()) for name, rows in models.items()},
        "scope": {
            "target_product_code": request["target_product_code"],
            "sales_order_ids": request["sales_order_ids"],
            "active_manufacturing_global": True,
        },
    }


def _metadata(center: dict) -> tuple[int, dict[str, dict]]:
    note = _plain(center["note"])
    capacity = re.findall(
        r"Benchmark capacity:\s*([\d.]+) total processing minutes across the scenario horizon\.",
        note,
    )
    _need(len(capacity) == 1, f"WC {center['code']}: exact horizon-wide capacity note required")
    cap = _integer(float(capacity[0]), f"WC {center['code']} capacity")
    _need(
        note.count(MARKER) == 1,
        f"WC {center['code']}: planning-metadata-v1 required; do not infer unit time/cost",
    )
    rows = {}
    for line in note.split(MARKER, 1)[1].splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LiveInputError(f"WC {center['code']}: malformed planning metadata JSON") from exc
        _need(
            isinstance(row, dict)
            and set(row) == {"product_code", "time_per_unit_minutes", "unit_cost", "lead_days"},
            f"WC {center['code']}: metadata fields missing/unknown",
        )
        code = row["product_code"]
        _need(isinstance(code, str) and bool(code), "Metadata product_code required")
        _need(code not in rows, f"WC {center['code']}: duplicate product metadata {code}")
        _number(row["time_per_unit_minutes"], f"{code} unit time", positive=True)
        _number(row["unit_cost"], f"{code} unit manufacturing cost")
        _integer(row["lead_days"], f"{code} lead_days")
        rows[code] = row
    _need(bool(rows), f"WC {center['code']}: no product metadata")
    return cap, rows


def build_blueprint(snapshot: dict, request: dict) -> dict:
    """Map explicit Odoo records into the pinned ScenarioBlueprint schema.

    Supported: one target SKU, integer quantities, multi-level acyclic BoMs with
    one operation per product, unexecuted confirmed commitments, USD, one company,
    canonical product units, and no unrelated demand/shared-center work. It is a
    planning input, not authorization to execute the resulting plan.
    """
    request = validate_request(request)
    _need(
        isinstance(snapshot, dict) and snapshot.get("schema_version") == 1,
        "Snapshot schema_version=1 required",
    )
    _need(isinstance(snapshot.get("models"), dict), "Snapshot models required")
    scope = snapshot.get("scope", {})
    _need(
        scope.get("active_manufacturing_global") is True,
        "Global active manufacturing snapshot required for shared capacity",
    )
    _need(
        scope.get("target_product_code") == request["target_product_code"]
        and set(scope.get("sales_order_ids", [])) == set(request["sales_order_ids"]),
        "Snapshot/request scope mismatch",
    )
    tables = {}
    for name in FIELDS:
        _need(name in snapshot["models"], f"Snapshot missing model {name}")
        rows = _records(snapshot["models"][name], name)
        tables[name] = {r["id"]: r for r in rows}

    def row(model, rid):
        _need(rid in tables[model], f"{model}: missing referenced record {rid}")
        return tables[model][rid]

    def relation_record(model, value):
        return row(model, _relation(value))

    def rows(model):
        return list(tables[model].values())

    products = tables["product.product"]
    targets = [p for p in products.values() if p["default_code"] == request["target_product_code"]]
    _need(len(targets) == 1, "Target SKU is missing/ambiguous")
    target = targets[0]
    anchor = _day(request["anchor_date"], "anchor_date")
    closure: set[int] = set()
    boms_by_product = {}
    components_by_product = {}
    visiting: set[int] = set()

    def visit(pid):
        _need(pid not in visiting, f"BoM cycle at product {pid}")
        if pid in closure:
            return
        visiting.add(pid)
        product = row("product.product", pid)
        candidates = [
            b
            for b in rows("mrp.bom")
            if b["active"]
            and _relation(b["product_tmpl_id"]) == _relation(product["product_tmpl_id"])
            and _relation(b["product_id"]) in (None, pid)
        ]
        _need(len(candidates) <= 1, f"{product['default_code']}: multiple BoMs unsupported")
        children = []
        if candidates:
            bom = candidates[0]
            _need(bom["type"] == "normal", "Phantom/subcontracting BoMs unsupported")
            _need(
                _relation(bom["product_uom_id"]) == _relation(product["uom_id"]),
                "BoM UoM conversion unsupported",
            )
            denominator = _number(bom["product_qty"], "BoM output quantity", positive=True)
            boms_by_product[pid] = bom
            for line in rows("mrp.bom.line"):
                if _relation(line["bom_id"]) != bom["id"]:
                    continue
                cid = _relation(line["product_id"])
                child = row("product.product", cid)
                _need(
                    _relation(line["product_uom_id"]) == _relation(child["uom_id"]),
                    "Component UoM conversion unsupported",
                )
                quantity = _integer(
                    _number(line["product_qty"], "BoM component quantity", positive=True)
                    / denominator,
                    "BoM units per finished unit",
                    positive=True,
                )
                children.append((cid, quantity))
                visit(cid)
            _need(bool(children), "Manufacturing BoM must contain components")
            _need(
                len({cid for cid, _ in children}) == len(children),
                "Duplicate BoM component lines unsupported",
            )
        components_by_product[pid] = children
        visiting.remove(pid)
        closure.add(pid)

    visit(target["id"])
    codes = [products[pid]["default_code"] for pid in closure]
    _need(
        all(isinstance(c, str) and c for c in codes) and len(set(codes)) == len(codes),
        "Closure SKUs missing/duplicate",
    )

    route_names = {
        "Buy": "buy",
        "Manufacture": "manufacture",
        "Replenish on Order (MTO)": "mto",
        "Make To Order": "mto",
        "Dropship": "dropship",
    }

    def product_spec(pid):
        p = products[pid]
        mapped_routes = []
        for rid in p["route_ids"]:
            name = row("stock.route", rid)["name"]
            _need(name in route_names, f"Unsupported stock route: {name}")
            mapped_routes.append(route_names[name])
        _need(
            p["type"] in ("consu", "product") and p["is_storable"] is True,
            "Only storable products supported",
        )
        _need("dropship" not in mapped_routes, "Dropship fulfillment unsupported")
        return {
            "name": p["name"],
            "code": p["default_code"],
            "list_price_dollars": _number(p["list_price"], "List price"),
            "standard_price_dollars": _number(p["standard_price"], "Standard price"),
            "type": p["type"],
            "is_storable": True,
            "routes": mapped_routes,
        }

    specs = {pid: product_spec(pid) for pid in closure}

    selected = {i: row("sale.order", i) for i in request["sales_order_ids"]}
    company_ids = {_relation(s["company_id"]) for s in selected.values()}
    _need(
        len(company_ids) == 1 and None not in company_ids, "Exactly one explicit company required"
    )
    company = next(iter(company_ids))
    currencies = {_relation(s["currency_id"]) for s in selected.values()}
    _need(
        len(currencies) == 1 and None not in currencies, "Mixed/missing sale currencies unsupported"
    )
    currency = next(iter(currencies))
    _need(
        row("res.currency", currency)["name"] == "USD",
        "Pinned solver requires USD; no implicit currency conversion",
    )
    customers = []
    customer_by_so_name = {}
    for sid, so in selected.items():
        _need(so["state"] == "sale", f"SO {sid}: confirmed, undelivered sale required")
        lines = [
            line
            for line in rows("sale.order.line")
            if _relation(line["order_id"]) == sid and not line["display_type"]
        ]
        _need(
            len(lines) == 1 and _relation(lines[0]["product_id"]) == target["id"],
            "Each selected SO must contain one target SKU line",
        )
        line = lines[0]
        _need(
            _relation(line["product_uom_id"]) == _relation(target["uom_id"]),
            "Sale UoM conversion unsupported",
        )
        _need(
            line["qty_delivered"] == 0 and line["discount"] == 0,
            "Partial delivery or sale discount unsupported",
        )
        _need(
            abs(
                _number(line["price_unit"], "Sale unit price")
                - specs[target["id"]]["list_price_dollars"]
            )
            < 0.005,
            "Sale price differs from target list price",
        )
        partner = relation_record("res.partner", so["partner_id"])
        _need(isinstance(partner["ref"], str) and partner["ref"], "Customer partner ref required")
        deadline = (_day(so["commitment_date"], f"SO {sid} commitment") - anchor).days
        _need(deadline >= 0, "Overdue SO unsupported; do not silently shift deadline")
        customers.append(
            {
                "name": partner["name"],
                "ref": partner["ref"],
                "email": partner["email"] or "",
                "demand": _integer(line["product_uom_qty"], "Sale quantity", positive=True),
                "deadline": deadline,
                "budget_dollars": request["sales_order_budget_caps"][str(sid)],
                "task_order_source": "seeded",
                "task_order_ref": so["name"],
                "seeded_order_state": "sale",
            }
        )
        customer_by_so_name[so["name"]] = partner["ref"]
    _need(
        len({c["ref"] for c in customers}) == len(customers),
        "Multiple selected SOs for one customer unsupported",
    )
    missing_sales_order_ids: set[int] = set()
    for line in rows("sale.order.line"):
        if _relation(line["product_id"]) not in closure or _relation(line["order_id"]) in selected:
            continue
        so = relation_record("sale.order", line["order_id"])
        if (
            so["state"] not in ("sale", "done")
            or line["qty_delivered"] >= line["product_uom_qty"]
        ):
            continue
        _need(
            type(so["id"]) is int and 0 < so["id"] <= 2**53 - 1,
            "Unselected active demand has an invalid sales order ID",
        )
        missing_sales_order_ids.add(so["id"])
    if missing_sales_order_ids:
        raise LiveInputError(
            "Unselected active demand shares closure products",
            code="unselected_active_demand",
            details={"missing_sales_order_ids": sorted(missing_sales_order_ids)},
        )

    centers = tables["mrp.workcenter"]
    relevant_center_ids: set[int] = set()
    manufactured = []
    wc_specs = {}
    for pid, bom in boms_by_product.items():
        # Empty product.route_ids means no automatic replenishment route is
        # configured, not that the task's explicitly allowed manual MO is
        # forbidden. A live BoM, operation and validated per-SKU metadata provide
        # the manufacturing capability. Preserve the actual route list above.
        operations = [
            o for o in rows("mrp.routing.workcenter") if _relation(o["bom_id"]) == bom["id"]
        ]
        _need(
            len(operations) == 1,
            f"{products[pid]['default_code']}: exactly one BoM operation required",
        )
        operation = operations[0]
        primary = relation_record("mrp.workcenter", operation["workcenter_id"])
        allowed = [primary["id"], *primary["alternative_workcenter_ids"]]
        _need(len(set(allowed)) == len(allowed), "Duplicate workcenter alternatives")
        choices = []
        for cid in allowed:
            center = row("mrp.workcenter", cid)
            capacity, meta = _metadata(center)
            code = products[pid]["default_code"]
            _need(code in meta, f"WC {center['code']}: missing unit time/cost for {code}")
            fact = meta[code]
            lead = _integer(bom["produce_delay"], "BoM produce_delay")
            _need(lead == fact["lead_days"], f"{code}: note lead differs from BoM produce_delay")
            _need(
                isinstance(center["code"], str) and bool(center["code"]), "Workcenter code required"
            )
            choices.append(
                {
                    "workcenter_code": center["code"],
                    "unit_cost_dollars": fact["unit_cost"],
                    "lead_time_days": lead,
                    "time_per_unit_minutes": fact["time_per_unit_minutes"],
                    "operation_name": operation["name"],
                }
            )
            wc_specs[center["code"]] = {
                "code": center["code"],
                "name": center["name"],
                "capacity_minutes": capacity,
                "time_efficiency": center["time_efficiency"],
                "time_start": center["time_start"],
                "time_stop": center["time_stop"],
                "note": _plain(center["note"]),
            }
            relevant_center_ids.add(cid)
        manufactured.append(
            {
                "product": specs[pid],
                "components": [
                    {"product": specs[cid], "per_unit": qty}
                    for cid, qty in components_by_product[pid]
                ],
                "workcenter_choices": choices,
                "primary_workcenter_code": primary["code"],
                "operation_name": operation["name"],
            }
        )
    _need(
        len({centers[cid]["code"] for cid in relevant_center_ids}) == len(relevant_center_ids),
        "Duplicate workcenter codes",
    )
    active_mos = {
        m["id"]: m for m in rows("mrp.production") if m["state"] not in ("done", "cancel")
    }
    for mo in active_mos.values():
        _need(
            _relation(mo["company_id"]) != company or _relation(mo["product_id"]) in closure,
            f"Unrelated active MO {mo['id']} is outside the BoM closure; "
            "its component reservations are not modeled",
        )
    relevant_mos = {
        mid: m for mid, m in active_mos.items() if _relation(m["product_id"]) in closure
    }
    for work in rows("mrp.workorder"):
        if work["state"] in ("done", "cancel"):
            continue
        if _relation(work["workcenter_id"]) in relevant_center_ids:
            _need(
                _relation(work["production_id"]) in relevant_mos,
                f"Unrelated active WO {work['id']} shares planning workcenter; cannot ignore its load",
            )

    def capacity_limit(partner, sku):
        pattern = rf"Maximum order quantity for {re.escape(sku)}:\s*([\d.]+) units"
        note = _plain(partner["comment"])
        matches = re.findall(pattern, note)
        _need(
            f"Maximum order quantity for {sku}:" not in note or bool(matches),
            f"{sku}: malformed vendor capacity note; cannot assume unlimited supply",
        )
        _need(len(matches) <= 1, f"{sku}: conflicting/duplicate vendor capacity notes")
        return _integer(float(matches[0]), "Vendor maximum") if matches else None

    vendors = []
    offer_by_key = {}
    offers_by_product = defaultdict(list)
    component_vendor_keys = set()
    for si in rows("product.supplierinfo"):
        applicable = [
            pid
            for pid in closure
            if _relation(products[pid]["product_tmpl_id"]) == _relation(si["product_tmpl_id"])
            and _relation(si["product_id"]) in (None, pid)
        ]
        if not applicable:
            continue
        _need(_relation(si["currency_id"]) == currency, "Supplier currency mismatch")
        _need(_relation(si["company_id"]) in (None, company), "Supplier company mismatch")
        partner = relation_record("res.partner", si["partner_id"])
        _need(isinstance(partner["ref"], str) and bool(partner["ref"]), "Vendor ref required")
        for pid in applicable:
            code = products[pid]["default_code"]
            offer = {
                "name": partner["name"],
                "ref": partner["ref"],
                "supplier_rank": partner["supplier_rank"],
                "product_code": code,
                "price_dollars": _number(si["price"], "Offer price"),
                "lead_time": _integer(si["delay"], "Offer delay"),
                "min_qty": _integer(si["min_qty"], "Offer minimum"),
                "max_qty": capacity_limit(partner, code),
                "description": si["product_name"] or None,
                "supply_role": "finished" if pid == target["id"] else "component",
            }
            key = f"{offer['ref']}|{code}|{offer['min_qty']}|{round(offer['price_dollars'], 2)}|{offer['lead_time']}"
            _need(
                key not in offer_by_key, "Duplicate indistinguishable supplier offers unsupported"
            )
            if pid != target["id"]:
                pair = (code, offer["ref"])
                _need(
                    pair not in component_vendor_keys,
                    f"{code}: multiple offers from component vendor {offer['ref']} "
                    "are unsupported by the pinned solver",
                )
                component_vendor_keys.add(pair)
            offer_by_key[key] = offer
            offers_by_product[pid].append((key, offer))
            vendors.append(offer)
    for pid in closure:
        if pid != target["id"] and pid in boms_by_product:
            _need(
                not offers_by_product[pid],
                f"{products[pid]['default_code']}: buying manufactured intermediate products "
                "is unsupported by the pinned solver",
            )
        if pid not in boms_by_product:
            _need(
                bool(offers_by_product[pid]),
                f"{products[pid]['default_code']}: missing live supplier offers",
            )

    mo_by_name = {m["name"]: m for m in relevant_mos.values()}
    _need(len(mo_by_name) == len(relevant_mos), "Duplicate MO names")

    def origins(value):
        tokens = [token.strip() for token in str(value or "").split(",") if token.strip()]
        _need(bool(tokens), "Commitment lacks traceable origin")
        _need(
            all(token in customer_by_so_name or token in mo_by_name for token in tokens),
            "Commitment origin refers outside selected orders/BoM closure",
        )
        return (
            [customer_by_so_name[token] for token in tokens if token in customer_by_so_name],
            [token for token in tokens if token in mo_by_name],
        )

    # "seeded_*" is the upstream schema's name for existing commitments. These
    # values come exclusively from explicit live Odoo records, never seed files.
    seeded_mos = []
    baseline_mfg = defaultdict(int)
    for mid, mo in relevant_mos.items():
        pid = _relation(mo["product_id"])
        _need(
            mo["state"] == "confirmed" and mo["qty_produced"] == 0,
            f"MO {mid}: partially executed/unconfirmed work unsupported",
        )
        _need(_relation(mo["company_id"]) == company, "Manufacturing company mismatch")
        _need(
            pid in boms_by_product and _relation(mo["bom_id"]) == boms_by_product[pid]["id"],
            "MO BoM mismatch",
        )
        _need(
            _relation(mo["product_uom_id"]) == _relation(products[pid]["uom_id"]),
            "MO UoM conversion unsupported",
        )
        work = [
            w
            for w in rows("mrp.workorder")
            if _relation(w["production_id"]) == mid and w["state"] not in ("done", "cancel")
        ]
        _need(
            len(work) == 1 and _relation(work[0]["workcenter_id"]) in relevant_center_ids,
            f"MO {mid}: one active assigned workorder required",
        )
        center = relation_record("mrp.workcenter", work[0]["workcenter_id"])
        choices = next(
            p["workcenter_choices"]
            for p in manufactured
            if p["product"]["code"] == products[pid]["default_code"]
        )
        _need(
            center["code"] in {c["workcenter_code"] for c in choices},
            "MO assigned to unsupported product workcenter",
        )
        quantity = _integer(mo["product_qty"], "MO quantity", positive=True)
        customer_refs, plan_refs = origins(mo["origin"])
        seeded_mos.append(
            {
                "ref": mo["name"],
                "plan_ref": mo["name"],
                "product_code": products[pid]["default_code"],
                "quantity": quantity,
                "workcenter_code": center["code"],
                "start_days": (_day(mo["date_start"], "MO start") - anchor).days,
                "deadline_days": (_day(mo["date_deadline"], "MO deadline") - anchor).days,
                "origin_customer_refs": customer_refs,
                "origin_plan_refs": plan_refs,
                "state": "confirmed",
            }
        )
        baseline_mfg[(products[pid]["default_code"], center["code"])] += quantity

    seeded_pos = []
    baseline_offers = defaultdict(int)
    for po in rows("purchase.order"):
        if po["state"] == "cancel":
            continue
        _need(
            po["state"] == "purchase",
            f"PO {po['id']}: only unreceived confirmed commitments supported",
        )
        _need(
            _relation(po["company_id"]) == company and _relation(po["currency_id"]) == currency,
            "Purchase company/currency mismatch",
        )
        lines = [
            line
            for line in rows("purchase.order.line")
            if _relation(line["order_id"]) == po["id"] and not line["display_type"]
        ]
        _need(len(lines) == 1, "Only one supplier-offer line per existing PO supported")
        line = lines[0]
        pid = _relation(line["product_id"])
        _need(pid in closure, "Existing PO mixes unrelated products")
        _need(line["qty_received"] == 0, "Partially received PO unsupported")
        _need(
            _relation(line["product_uom_id"]) == _relation(products[pid]["uom_id"]),
            "Purchase UoM conversion unsupported",
        )
        partner = relation_record("res.partner", po["partner_id"])
        matches = [
            (key, offer)
            for key, offer in offers_by_product[pid]
            if offer["ref"] == partner["ref"]
            and abs(offer["price_dollars"] - line["price_unit"]) < 0.005
        ]
        _need(
            len(matches) == 1,
            f"PO {po['id']}: supplier offer cannot be uniquely resolved from live price/vendor/SKU",
        )
        key, _ = matches[0]
        quantity = _integer(line["product_qty"], "PO quantity", positive=True)
        customer_refs, plan_refs = origins(po["origin"])
        seeded_pos.append(
            {
                "ref": po["name"],
                "plan_ref": po["name"],
                "vendor_ref": partner["ref"],
                "product_code": products[pid]["default_code"],
                "quantity": quantity,
                "price_unit": line["price_unit"],
                "planned_arrival_days": (_day(line["date_planned"], "PO delivery") - anchor).days,
                "origin_customer_refs": customer_refs,
                "origin_plan_refs": plan_refs,
                "supply_role": "finished" if pid == target["id"] else "component",
                "offer_key": key,
                "state": "purchase",
            }
        )
        baseline_offers[key] += quantity

    disruption = request["disruption"]
    repair = {
        "kind": disruption["kind"],
        "seeded_purchase_orders": seeded_pos,
        "seeded_manufacturing_orders": seeded_mos,
        "baseline_offer_quantities": [
            {"offer_key": key, "quantity": qty} for key, qty in sorted(baseline_offers.items())
        ],
        "baseline_manufacturing_quantities": [
            {"product_code": code, "workcenter_code": wc, "quantity": qty}
            for (code, wc), qty in sorted(baseline_mfg.items())
        ],
    }
    # Deliberately omit baseline_plan: only quantities/IDs observed in Odoo are
    # carried forward. The solver must recompute a plan from current facts.
    if disruption["kind"] == "supplier_cancellation":
        # A disrupted vendor may supply the finished target or a component.
        # Resolve across every offer in the closure, then use active seeded
        # POs to disambiguate multiple offers from the same vendor. Never pick
        # the first candidate and never assume the broken lane is finished.
        candidates = [
            key
            for key, offer in offer_by_key.items()
            if offer["ref"] == disruption["vendor_ref"]
            and ("offer_key" not in disruption or key == disruption["offer_key"])
        ]
        if len(candidates) > 1 and "offer_key" not in disruption:
            with_po = [
                key for key in candidates
                if any(po["offer_key"] == key for po in seeded_pos)
            ]
            if len(with_po) == 1:
                candidates = with_po
        if len(candidates) != 1:
            raise LiveInputError(
                "Broken supplier offer ambiguous/missing; provide exact offer_key",
                code=(
                    "broken_offer_ambiguous" if candidates else "broken_offer_missing"
                ),
                details={
                    "candidates": [
                        {
                            "offer_key": key,
                            "product_code": offer_by_key[key]["product_code"],
                            "supply_role": offer_by_key[key]["supply_role"],
                            "min_qty": offer_by_key[key]["min_qty"],
                            "price_dollars": offer_by_key[key]["price_dollars"],
                            "lead_time": offer_by_key[key]["lead_time"],
                        }
                        for key in candidates[:3]
                    ]
                },
            )
        broken = candidates[0]
        repair.update(
            broken_supplier_offer_key=broken,
            broken_supplier_vendor_ref=disruption["vendor_ref"],
            broken_purchase_order_refs=[p["ref"] for p in seeded_pos if p["offer_key"] == broken],
            broken_manufacturing_order_refs=[],
        )
        _need(
            bool(repair["broken_purchase_order_refs"]),
            "No active PO matches disrupted supplier offer",
        )
    else:
        broken = disruption["workcenter_code"]
        _need(broken in wc_specs, "Disrupted workcenter is not in product closure")
        repair.update(
            broken_workcenter_code=broken,
            broken_purchase_order_refs=[],
            broken_manufacturing_order_refs=[
                m["ref"] for m in seeded_mos if m["workcenter_code"] == broken
            ],
        )
        _need(
            bool(repair["broken_manufacturing_order_refs"]),
            "No active MO matches disrupted workcenter",
        )
        # Explicit task outage is a hard business constraint even if note is stale.
        wc_specs[broken]["capacity_minutes"] = 0

    stock = {products[pid]["default_code"]: 0 for pid in closure}
    for quant in rows("stock.quant"):
        pid = _relation(quant["product_id"])
        if pid not in closure:
            continue
        if relation_record("stock.location", quant["location_id"])["usage"] != "internal":
            continue
        _need(_relation(quant["company_id"]) == company, "Inventory company mismatch")
        stock[products[pid]["default_code"]] += _integer(quant["quantity"], "On-hand quantity")
    return {
        "scenario_number": 0,
        "name": "Live Odoo recovery request",
        "instruction": "Repair the explicitly selected existing confirmed sales orders.",
        "background_and_policy": "Inputs were mapped from live Odoo records and explicit business request. "
        "Horizon-wide capacity uses public planning-metadata-v1 notes, not calendar workcenter_load. "
        "For this buy-or-manufacture repair workflow, live BoMs/operations and supplier offers define "
        "manual fulfillment options; empty automatic replenishment route_ids are preserved, not treated as a ban. "
        "Shared-center work outside the selected BoM closure is rejected. Existing stock is sunk cost. "
        "This is a plan proposal; validate traceability, schedules and post-write state before claiming execution.",
        "objective_kind": "repair_plan",
        "margin": request["margin"],
        "customers": customers,
        "vendors": vendors,
        "product": specs[target["id"]],
        "manufactured_products": manufactured,
        "workcenters": list(wc_specs.values()),
        "product_stock_units": stock,
        "product_stock_cost_dollars": {code: 0.0 for code in stock},
        "repair_context": repair,
    }
