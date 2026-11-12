"""Live Odoo: a two-product cancelled commitment becomes two replacement orders."""
from __future__ import annotations

import os
import uuid
import xmlrpc.client
from datetime import UTC, datetime, timedelta

import pytest

from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config
from forgeflow.integrations.odoo20.business import Odoo20BusinessService
from forgeflow.superagent.plan_effects import prepare_replacement_plan

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD)),
                                reason="Odoo 20 credentials not configured")


def test_two_product_commitment_is_planned_and_executed() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    suffix = uuid.uuid4().hex[:8]
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {
        "name": f"Multi supplier {suffix}", "supplier_rank": 1, "company_id": 1})
    products = []
    for index in (1, 2):
        product = adapter.create("product.product", {
            "name": f"Multi material {suffix}-{index}",
            "default_code": f"MP-{suffix}-{index}", "type": "consu",
            "uom_id": 1, "company_id": 1})
        template = adapter.search_read("product.product", [("id", "=", product)],
                                       ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
        models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
            "partner_id": vendor, "product_tmpl_id": template, "min_qty": 0,
            "price": 3.0 + index, "delay": 0, "company_id": 1}], ctx)
        products.append(product)
    due = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d 09:00:00")
    order = models.execute_kw(DB, uid, PASSWORD, "purchase.order", "create", [{
        "partner_id": vendor, "company_id": 1, "partner_ref": f"MP-{suffix}",
        "date_order": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "order_line": [
            (0, 0, {"product_id": products[0], "product_qty": 4.0,
                    "price_unit": 4.0, "date_planned": due}),
            (0, 0, {"product_id": products[1], "product_qty": 3.0,
                    "price_unit": 5.0, "date_planned": due}),
        ]}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "purchase.order", "button_confirm",
                      [[order]], ctx)
    try:
        models.execute_kw(DB, uid, PASSWORD, "purchase.order", "button_cancel",
                          [[order]], ctx)
    except xmlrpc.client.Fault:
        pass
    service = Odoo20BusinessService(adapter)
    payload = prepare_replacement_plan(service, order_id=int(order),
                                       tenant_id=f"multi-{suffix}")
    assert len(payload["plans"]) == 2
    assert len(payload["actions"]) == 2
    prepared = payload["prepared"]
    service.approve(prepared, approver_uid=2, comment="multi-product approval")
    result = service.apply_plan(service.load_prepared(prepared.plan_id))
    assert result.succeeded and len(result.actions) == 2
    verification = service.verify_plan(service.load_prepared(prepared.plan_id))
    assert verification.verified
    created = adapter.search_read(
        "purchase.order", [("partner_id", "=", vendor),
                           ("state", "in", ["purchase", "done"])],
        ["id", "order_line"], limit=10)
    assert len(created) == 2
