"""Live Odoo 20 adapter checks. Credentials are env-only and never committed."""
from __future__ import annotations

import os

import pytest

from forgeflow.integrations.odoo20.adapter import Odoo20Adapter, Odoo20Config

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD)),
                               reason="Odoo 20 integration credentials not configured")


def test_contract_and_records_are_real() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    version = adapter.version()
    assert version.major == 20
    check = adapter.check_contract()
    assert check.ok, (check.missing_models, check.missing_fields)
    products = adapter.search_read("product.product", [], ["name", "default_code"], limit=3)
    assert isinstance(products, list)
    created = adapter.create("forgeflow.exception_event", {
        "name": "Adapter integration test", "event_type": "adapter_test",
        "occurred_at": "2026-11-07 12:00:00", "description": "isolated adapter check",
    })
    assert isinstance(created, int) and created > 0
    rows = adapter.search_read("forgeflow.exception_event", [("id", "=", created)],
                               ["name", "state"], limit=1)
    assert rows and rows[0]["state"] == "reported"


def test_approved_cancel_is_idempotent_and_reconcilable() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    vendor = adapter.create("res.partner", {"name": "W3 Adapter Supplier", "supplier_rank": 1})
    product = adapter.create("product.product", {"name": "W3 Adapter Component"})
    order = adapter.create("purchase.order", {
        "partner_id": vendor,
        "order_line": [(0, 0, {
            "product_id": product, "product_qty": 2, "price_unit": 10,
            "date_planned": "2026-11-20 12:00:00",
        })],
    })
    assert adapter.confirm_purchase_order(order) is True
    assert adapter.search_read("purchase.order", [("id", "=", order)], ["state"])[0]["state"] == "purchase"
    from forgeflow.integrations.odoo20.business import Odoo20BusinessService

    service = Odoo20BusinessService(adapter)
    prepared = service.prepare_cancel(order, actor_uid=2, reason="live adapter test")
    service.approve(prepared, approver_uid=2, comment="live adapter test")
    key = f"adapter-op-{order}"
    first = adapter.apply_approved_plan(prepared.plan_id, key, "cancel_purchase_order",
                                        "purchase.order", order, {})
    second = adapter.apply_approved_plan(prepared.plan_id, key, "cancel_purchase_order",
                                         "purchase.order", order, {})
    assert first["state"] == "succeeded" and second["operation_id"] == first["operation_id"]
    assert adapter.reconcile_plan(prepared.plan_id, key)["state"] == "succeeded"
    assert adapter.search_read("purchase.order", [("id", "=", order)], ["state"])[0]["state"] == "cancel"


def test_unapproved_plan_cannot_change_native_order() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    vendor = adapter.create("res.partner", {"name": "W3 Unapproved Supplier", "supplier_rank": 1})
    product = adapter.create("product.product", {"name": "W3 Unapproved Component"})
    order = adapter.create("purchase.order", {
        "partner_id": vendor,
        "order_line": [(0, 0, {
            "product_id": product, "product_qty": 1, "price_unit": 5,
            "date_planned": "2026-11-21 12:00:00",
        })],
    })
    assert adapter.confirm_purchase_order(order) is True
    exception = adapter.create("forgeflow.exception_event", {
        "name": "Unapproved cancel test", "event_type": "supplier_disruption",
        "occurred_at": "2026-11-07 12:00:00",
    })
    plan = adapter.create("forgeflow.recovery_plan", {
        "name": "Unapproved plan", "exception_id": exception,
        "plan_hash": "e" * 64, "fact_snapshot_hash": "f" * 64,
    })
    with pytest.raises(Exception):
        adapter.apply_approved_plan(plan, f"unapproved-{order}", "cancel_purchase_order",
                                    "purchase.order", order, {})
    assert adapter.search_read("purchase.order", [("id", "=", order)], ["state"])[0]["state"] == "purchase"


def test_independent_verifier_reads_authoritative_native_state() -> None:
    from forgeflow.integrations.odoo20.verifier import Odoo20BusinessVerifier
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    vendor = adapter.create("res.partner", {"name": "W3 Verify Supplier", "supplier_rank": 1})
    product = adapter.create("product.product", {"name": "W3 Verify Component"})
    order = adapter.create("purchase.order", {
        "partner_id": vendor,
        "order_line": [(0, 0, {
            "product_id": product, "product_qty": 1, "price_unit": 7,
            "date_planned": "2026-11-22 12:00:00",
        })],
    })
    assert adapter.confirm_purchase_order(order) is True
    from forgeflow.integrations.odoo20.business import Odoo20BusinessService

    service = Odoo20BusinessService(adapter)
    prepared = service.prepare_cancel(order, actor_uid=2, reason="verifier live test")
    service.approve(prepared, approver_uid=2, comment="verifier live test")
    key = f"verify-op-{order}"
    adapter.apply_approved_plan(prepared.plan_id, key, "cancel_purchase_order",
                                "purchase.order", order, {})
    result = Odoo20BusinessVerifier(adapter).verify_purchase_cancel(
        prepared.plan_id, key)
    assert result.status == "verified"
    assert {check.code for check in result.checks} >= {
        "operation_present", "operation_succeeded", "receipt_state",
        "readback_cancel", "native_cancelled"}


def test_business_service_prepare_approve_apply_verify() -> None:
    from forgeflow.integrations.odoo20.business import Odoo20BusinessService
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    vendor = adapter.create("res.partner", {"name": "W3 Service Supplier", "supplier_rank": 1})
    product = adapter.create("product.product", {"name": "W3 Service Component"})
    order = adapter.create("purchase.order", {
        "partner_id": vendor,
        "order_line": [(0, 0, {
            "product_id": product, "product_qty": 3, "price_unit": 11,
            "date_planned": "2026-11-23 12:00:00",
        })],
    })
    assert adapter.confirm_purchase_order(order) is True
    service = Odoo20BusinessService(adapter)
    prepared = service.prepare_cancel(order, actor_uid=2, reason="supplier cancellation")
    loaded = service.load_prepared(prepared.plan_id)
    assert loaded.plan_hash == prepared.plan_hash and loaded.facts["order"]["id"] == order
    service.approve(prepared, approver_uid=2, comment="approved live service test")
    key = f"service-op-{order}"
    applied = service.apply_cancel(prepared, idempotency_key=key)
    assert applied.state == "succeeded"
    assert service.reconcile(prepared, idempotency_key=key).operation_id == applied.operation_id
    verified = service.verify(prepared, idempotency_key=key)
    assert verified.status == "verified"
    assert adapter.search_read("purchase.order", [("id", "=", order)], ["state"])[0]["state"] == "cancel"
