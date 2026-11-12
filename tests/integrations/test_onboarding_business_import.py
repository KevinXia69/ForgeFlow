"""Live Odoo: import a purchase order with a line and a BOM with a component."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config
from forgeflow.onboarding_import import authorize, build_preview, execute, readback

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD)),
                                reason="Odoo 20 credentials not configured")


def _mapping(source, source_field, model, target):
    return {"id": f"{source}:{source_field}", "source_id": source,
            "source_field": source_field, "target_model": model,
            "target_field": target, "status": "confirmed"}


def test_live_purchase_order_bom_and_lines_import() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    suffix = uuid.uuid4().hex[:8]
    vendor = adapter.create("res.partner", {
        "name": f"Import vendor {suffix}", "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Import component {suffix}", "default_code": f"IMP-C-{suffix}",
        "type": "consu"})
    finished = adapter.create("product.product", {
        "name": f"Import finished {suffix}", "default_code": f"IMP-F-{suffix}",
        "type": "consu"})
    mappings = [
        _mapping("po", "ref", "purchase.order", "partner_ref"),
        _mapping("po", "vendor", "purchase.order", "partner_id"),
        _mapping("po", "date", "purchase.order", "date_order"),
        _mapping("pol", "order_ref", "purchase.order.line", "order_id"),
        _mapping("pol", "product", "purchase.order.line", "product_id"),
        _mapping("pol", "qty", "purchase.order.line", "product_qty"),
        _mapping("pol", "price", "purchase.order.line", "price_unit"),
        _mapping("pol", "due", "purchase.order.line", "date_planned"),
        _mapping("pol", "description", "purchase.order.line", "name"),
        _mapping("bom", "code", "mrp.bom", "code"),
        _mapping("bom", "product", "mrp.bom", "product_tmpl_id"),
        _mapping("bom", "qty", "mrp.bom", "product_qty"),
        _mapping("bom", "type", "mrp.bom", "type"),
        _mapping("boml", "bom_code", "mrp.bom.line", "bom_id"),
        _mapping("boml", "component", "mrp.bom.line", "product_id"),
        _mapping("boml", "qty", "mrp.bom.line", "product_qty"),
        _mapping("uom", "name", "uom.uom", "name"),
        _mapping("uom", "factor", "uom.uom", "factor"),
        _mapping("wh", "code", "stock.warehouse", "code"),
        _mapping("wh", "name", "stock.warehouse", "name"),
        _mapping("wc", "code", "mrp.workcenter", "code"),
        _mapping("wc", "name", "mrp.workcenter", "name"),
    ]
    workspace = {"id": "a" * 32,
                 "mapping_adoption": {"mapping_sha256": "b" * 64},
                 "mappings": mappings}
    records = {
        "po": {"headers": ["ref", "vendor", "date"],
               "rows": [[f"PO-IMP-{suffix}", vendor, "2026-11-12 09:00:00"]]},
        "pol": {"headers": ["order_ref", "product", "qty", "price", "due",
                            "description"],
                "rows": [[f"PO-IMP-{suffix}", component, "5", "2.5",
                          "2026-11-12 09:00:00", "Imported component"]]},
        "bom": {"headers": ["code", "product", "qty", "type"],
                "rows": [[f"BOM-{suffix}", finished, "1", "normal"]]},
        "boml": {"headers": ["bom_code", "component", "qty"],
                 "rows": [[f"BOM-{suffix}", component, "2"]]},
        "uom": {"headers": ["name", "factor"],
                "rows": [[f"U-{suffix}", "1"]]},
        "wh": {"headers": ["code", "name"],
               "rows": [[f"WH{suffix[:3]}", f"WH {suffix}"]]},
        "wc": {"headers": ["code", "name"],
               "rows": [[f"WC{suffix[:3]}", f"WC {suffix}"]]},
    }
    preview = build_preview(workspace, records, adapter)
    assert preview["importable"], preview["rows"]
    execution = execute(preview, authorized=authorize(
        preview, uid="operator", now="2026-11-08T00:00:00Z",
        preview_sha256=preview["preview_sha256"]), adapter=adapter)
    assert execution["status"] == "imported", execution
    report = readback(preview, execution, adapter)
    assert report["passed"], report
    orders = adapter.search_read("purchase.order", [("partner_ref", "=",
                                                    f"PO-IMP-{suffix}")],
                                 ["id", "order_line"], limit=1)
    assert len(orders) == 1 and orders[0]["order_line"]
    boms = adapter.search_read("mrp.bom", [("code", "=", f"BOM-{suffix}")],
                               ["id"], limit=1)
    assert len(boms) == 1
    lines = adapter.search_read("mrp.bom.line", [("bom_id", "=", boms[0]["id"])],
                                ["id"], limit=10)
    assert len(lines) == 1
    assert adapter.search_read("uom.uom", [("name", "=", f"U-{suffix}")],
                               ["id"], limit=1)
    assert adapter.search_read("stock.warehouse", [("code", "=", f"WH{suffix[:3]}")],
                               ["id"], limit=1)
    assert adapter.search_read("mrp.workcenter", [("code", "=", f"WC{suffix[:3]}")],
                               ["id"], limit=1)
