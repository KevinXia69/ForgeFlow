"""Live Odoo: revoke an approval, re-approve and execute the new decision once."""
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


def test_revoke_then_reapprove_executes_the_new_identity_once() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    suffix = uuid.uuid4().hex[:8]
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {
        "name": f"Revoke supplier {suffix}", "supplier_rank": 1, "company_id": 1})
    product = adapter.create("product.product", {
        "name": f"Revoke material {suffix}", "default_code": f"RVK-{suffix}",
        "type": "consu", "uom_id": 1, "company_id": 1})
    template = adapter.search_read("product.product", [("id", "=", product)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": template, "min_qty": 0,
        "price": 5.0, "delay": 0, "company_id": 1}], ctx)
    due = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d 09:00:00")
    order = models.execute_kw(DB, uid, PASSWORD, "purchase.order", "create", [{
        "partner_id": vendor, "company_id": 1, "partner_ref": f"RVK-{suffix}",
        "date_order": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "order_line": [(0, 0, {"product_id": product, "product_qty": 6.0,
                               "price_unit": 5.0, "date_planned": due})]}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "purchase.order", "button_confirm", [[order]], ctx)
    try:
        models.execute_kw(DB, uid, PASSWORD, "purchase.order", "button_cancel",
                          [[order]], ctx)
    except xmlrpc.client.Fault:
        pass
    service = Odoo20BusinessService(adapter)
    prepared = prepare_replacement_plan(
        service, order_id=int(order), tenant_id=f"revoke-{suffix}")["prepared"]
    service.approve(prepared, approver_uid=2, comment="first")
    old_identity = service.load_prepared(prepared.plan_id).execution_identity
    service.revoke(prepared, reason="operator changed mind")
    revoked_identity = service.load_prepared(prepared.plan_id).execution_identity
    assert revoked_identity == "", "a revoked decision must not authorize execution"
    service.approve(service.load_prepared(prepared.plan_id), approver_uid=2,
                    comment="second")
    reloaded = service.load_prepared(prepared.plan_id)
    assert reloaded.execution_identity and reloaded.execution_identity != "tenant:1:old"
    # The cancelled approval's identity cannot execute the new decision.
    with pytest.raises(Exception):
        service.apply_plan(reloaded, execution_identity=old_identity)
    result = service.apply_plan(reloaded)
    assert result.succeeded
    operations = adapter.search_read(
        "forgeflow.execution_operation", [("plan_id", "=", prepared.plan_id)],
        ["id", "idempotency_key"], limit=10)
    assert len(operations) == 1
    assert operations[0]["idempotency_key"].startswith("plan:")
