"""Business import: orders, BOM, warehouse, UoM, workcenter and relations."""
from __future__ import annotations

from forgeflow.onboarding_import import authorize, build_preview, execute, readback


class Adapter:
    def __init__(self):
        self.records = {
            "res.partner": {1: {"id": 1, "name": "Vendor A", "ref": "V-1"}},
            "product.product": {
                2: {"id": 2, "name": "Component", "default_code": "SKU-1",
                    "product_tmpl_id": [11, "Component"]},
                3: {"id": 3, "name": "Finished", "default_code": "FG-1",
                    "product_tmpl_id": [12, "Finished"]},
            },
            "uom.uom": {}, "stock.warehouse": {}, "mrp.workcenter": {},
            "purchase.order": {}, "purchase.order.line": {},
            "sale.order": {}, "sale.order.line": {},
            "mrp.bom": {}, "mrp.bom.line": {},
        }
        self.next_id = 100
        self.creates = 0

    def search_read(self, model, domain, fields, limit=100):
        rows = list(self.records[model].values())
        for field, operator, expected in domain:
            if operator == "=":
                rows = [row for row in rows if row.get(field) == expected]
        return [{name: row.get(name) for name in fields} | {"id": row["id"]}
                for row in rows[:limit]]

    def create(self, model, values):
        self.creates += 1
        identifier = self.next_id
        self.next_id += 1
        self.records[model][identifier] = {"id": identifier, **values}
        return identifier

    def write(self, model, ids, values):
        for identifier in ids:
            self.records[model][identifier].update(values)
        return True


def _mapping(source, source_field, model, target):
    return {"id": f"{source}:{source_field}", "source_id": source,
            "source_field": source_field, "target_model": model,
            "target_field": target, "status": "confirmed"}


SOURCES = {
    "uom": (["name"], [["Units"]]),
    "wh": (["code", "name"], [["WH1", "Main Warehouse"]]),
    "wc": (["code", "name"], [["WC1", "Assembly"]]),
    "po": (["ref", "vendor", "date"], [["PO-EXT-1", "Vendor A",
                                        "2026-11-10 09:00:00"]]),
    "pol": (["order_ref", "product", "qty", "price", "due"],
            [["PO-EXT-1", "SKU-1", "5", "2.5", "2026-11-10 09:00:00"]]),
    "bom": (["code", "product", "qty"], [["BOM-1", "FG-1", "1"]]),
    "boml": (["bom_code", "component", "qty"], [["BOM-1", "SKU-1", "2"]]),
}

MAPPINGS = [
    _mapping("uom", "name", "uom.uom", "name"),
    _mapping("wh", "code", "stock.warehouse", "code"),
    _mapping("wh", "name", "stock.warehouse", "name"),
    _mapping("wc", "code", "mrp.workcenter", "code"),
    _mapping("wc", "name", "mrp.workcenter", "name"),
    _mapping("po", "ref", "purchase.order", "partner_ref"),
    _mapping("po", "vendor", "purchase.order", "partner_id"),
    _mapping("po", "date", "purchase.order", "date_order"),
    _mapping("pol", "order_ref", "purchase.order.line", "order_id"),
    _mapping("pol", "product", "purchase.order.line", "product_id"),
    _mapping("pol", "qty", "purchase.order.line", "product_qty"),
    _mapping("pol", "price", "purchase.order.line", "price_unit"),
    _mapping("pol", "due", "purchase.order.line", "date_planned"),
    _mapping("bom", "code", "mrp.bom", "code"),
    _mapping("bom", "product", "mrp.bom", "product_tmpl_id"),
    _mapping("bom", "qty", "mrp.bom", "product_qty"),
    _mapping("boml", "bom_code", "mrp.bom.line", "bom_id"),
    _mapping("boml", "component", "mrp.bom.line", "product_id"),
    _mapping("boml", "qty", "mrp.bom.line", "product_qty"),
]


def test_orders_bom_warehouse_uom_and_workcenter_import_idempotently() -> None:
    adapter = Adapter()
    workspace = {"id": "a" * 32,
                 "mapping_adoption": {"mapping_sha256": "b" * 64},
                 "mappings": MAPPINGS}
    records = {name: {"headers": headers, "rows": rows}
               for name, (headers, rows) in SOURCES.items()}
    preview = build_preview(workspace, records, adapter)
    assert preview["importable"], preview["rows"]
    assert preview["counts"]["create"] == 7
    authorized = authorize(preview, uid="operator", now="2026-11-08T00:00:00Z",
                           preview_sha256=preview["preview_sha256"])
    execution = execute(preview, authorized=authorized, adapter=adapter)
    assert execution["status"] == "imported", execution
    report = readback(preview, execution, adapter)
    assert report["passed"], report
    assert len(adapter.records["purchase.order"]) == 1
    order = next(iter(adapter.records["purchase.order"].values()))
    assert order["partner_id"] == 1
    line = next(iter(adapter.records["purchase.order.line"].values()))
    assert line["order_id"] == order["id"] and line["product_id"] == 2
    bom = next(iter(adapter.records["mrp.bom"].values()))
    assert bom["product_tmpl_id"] == 12
    bom_line = next(iter(adapter.records["mrp.bom.line"].values()))
    assert bom_line["bom_id"] == bom["id"] and bom_line["product_id"] == 2

    # The same source rows preview as unchanged and execute without new records.
    again = build_preview(workspace, records, adapter)
    assert again["counts"]["unchanged"] == 7
    assert again["importable"]
    second = execute(again, authorized=authorize(
        again, uid="operator", now="2026-11-08T00:01:00Z",
        preview_sha256=again["preview_sha256"]), adapter=adapter)
    assert second["counts"]["succeeded"] == 0
    assert adapter.creates == 7


def _order_workspace(sources):
    mappings = []
    for source, model, field in sources:
        mappings.append(_mapping(source, field, model, field))
    return {"id": "c" * 32, "mapping_adoption": {"mapping_sha256": "d" * 64},
            "mappings": mappings}


def test_child_source_sorted_first_is_still_imported_after_its_parent() -> None:
    adapter = Adapter()
    mappings = [
        _mapping("a_child", "order_ref", "purchase.order.line", "order_id"),
        _mapping("a_child", "product", "purchase.order.line", "product_id"),
        _mapping("a_child", "qty", "purchase.order.line", "product_qty"),
        _mapping("a_child", "name", "purchase.order.line", "name"),
        _mapping("z_parent", "ref", "purchase.order", "partner_ref"),
        _mapping("z_parent", "vendor", "purchase.order", "partner_id"),
        _mapping("z_parent", "date", "purchase.order", "date_order"),
    ]
    workspace = {"id": "c" * 32,
                 "mapping_adoption": {"mapping_sha256": "d" * 64},
                 "mappings": mappings}
    records = {
        # "a_child" sorts before "z_parent" in the mapping order.
        "a_child": {"headers": ["order_ref", "product", "qty", "name"],
                    "rows": [["PO-LATE-1", "SKU-1", "3", "Line"]]},
        "z_parent": {"headers": ["ref", "vendor", "date"],
                     "rows": [["PO-LATE-1", "Vendor A", "2026-11-10 09:00:00"]]},
    }
    preview = build_preview(workspace, records, adapter)
    assert preview["importable"], preview["rows"]
    assert [row["key"] for row in preview["rows"]][0] == "z_parent:1"
    execution = execute(preview, authorized=authorize(
        preview, uid="operator", now="2026-11-08T00:00:00Z",
        preview_sha256=preview["preview_sha256"]), adapter=adapter)
    assert execution["status"] == "imported", execution
    assert readback(preview, execution, adapter)["passed"]


def test_two_new_orders_keep_same_product_lines_distinct() -> None:
    adapter = Adapter()
    mappings = []
    for name in ("po_a_header", "po_a_line", "po_b_header", "po_b_line"):
        if name.endswith("header"):
            mappings.extend([
                _mapping(name, "ref", "purchase.order", "partner_ref"),
                _mapping(name, "vendor", "purchase.order", "partner_id"),
                _mapping(name, "date", "purchase.order", "date_order"),
            ])
        else:
            mappings.extend([
                _mapping(name, "order_ref", "purchase.order.line", "order_id"),
                _mapping(name, "product", "purchase.order.line", "product_id"),
                _mapping(name, "qty", "purchase.order.line", "product_qty"),
                _mapping(name, "name", "purchase.order.line", "name"),
            ])
    workspace = {"id": "e" * 32,
                 "mapping_adoption": {"mapping_sha256": "f" * 64},
                 "mappings": mappings}
    records = {}
    for header, line, ref in (("po_a_header", "po_a_line", "PO-A"),
                              ("po_b_header", "po_b_line", "PO-B")):
        records[header] = {
            "headers": ["ref", "vendor", "date"],
            "rows": [[ref, "Vendor A", "2026-11-10 09:00:00"]]}
        records[line] = {
            "headers": ["order_ref", "product", "qty", "name"],
            "rows": [[ref, "SKU-1", "2", f"{ref} line"]]}
    preview = build_preview(workspace, records, adapter)
    assert preview["importable"], preview["rows"]
    assert preview["counts"]["create"] == 4
    execution = execute(preview, authorized=authorize(
        preview, uid="operator", now="2026-11-08T00:00:00Z",
        preview_sha256=preview["preview_sha256"]), adapter=adapter)
    assert execution["status"] == "imported", execution
    assert readback(preview, execution, adapter)["passed"]
    assert len(adapter.records["purchase.order.line"]) == 2
