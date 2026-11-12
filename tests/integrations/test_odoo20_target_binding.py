from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config, Odoo20Error
from forgeflow.integrations.odoo20.business import Odoo20BusinessService

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD)),
                               reason="Odoo 20 credentials not configured")


def test_approved_plan_cannot_cancel_another_purchase_order() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    vendor = adapter.create("res.partner", {"name": f"Target vendor {uuid.uuid4().hex[:6]}",
                                            "supplier_rank": 1})
    product = adapter.create("product.product", {"name": "Target component"})
    def order() -> int:
        return adapter.create("purchase.order", {
            "partner_id": vendor,
            "order_line": [(0, 0, {"product_id": product, "product_qty": 1,
                                   "price_unit": 3,
                                   "date_planned": "2026-11-25 12:00:00"})],
        })
    first, second = order(), order()
    assert adapter.confirm_purchase_order(first) and adapter.confirm_purchase_order(second)
    service = Odoo20BusinessService(adapter)
    prepared = service.prepare_cancel(first, actor_uid=2, reason="target binding")
    service.approve(prepared, approver_uid=2, comment="approve A")
    with pytest.raises(Odoo20Error, match="plan_apply_rejected"):
        adapter.apply_approved_plan(prepared.plan_id, f"target-test-{uuid.uuid4().hex}",
                                    "cancel_purchase_order", "purchase.order", second, {})
    assert adapter.search_read("purchase.order", [("id", "=", second)],
                               ["state"])[0]["state"] == "purchase"
