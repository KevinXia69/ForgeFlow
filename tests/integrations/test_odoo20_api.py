"""Live HTTP slice for Odoo 20 preparation, approval, apply and verification."""
from __future__ import annotations

import io
import os
import uuid
import xmlrpc.client

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings
from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
CONTROL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD, CONTROL)),
                               reason="Odoo 20/control integration credentials not configured")


def test_http_plan_approve_apply_verify(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FORGEFLOW_ODOO20_BINDING_ADMIN_TOKEN", "live-binding-admin-token")
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    vendor = adapter.create("res.partner", {"name": "W3 API Supplier", "supplier_rank": 1})
    product = adapter.create("product.product", {"name": "W3 API Component"})
    order = adapter.create("purchase.order", {
        "partner_id": vendor,
        "order_line": [(0, 0, {
            "product_id": product, "product_qty": 1, "price_unit": 8,
            "date_planned": "2026-11-25 12:00:00",
        })],
    })
    assert adapter.confirm_purchase_order(order) is True
    # The replacement planner needs a usable supplier offer; create one for the
    # product and cancel the commitment, which is what the workbench scenario is.
    template = adapter.search_read(
        "product.product", [("id", "=", product)], ["product_tmpl_id"], limit=1
    )[0]["product_tmpl_id"][0]
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": template, "min_qty": 0,
        "price": 9.0, "delay": 0, "company_id": 1}])
    try:
        models.execute_kw(DB, uid, PASSWORD, "purchase.order", "button_cancel", [[order]])
    except xmlrpc.client.Fault:
        pass  # Odoo returns None after a successful cancel; XML-RPC cannot marshal it.
    second_order = adapter.create("purchase.order", {
        "partner_id": vendor,
        "order_line": [(0, 0, {
            "product_id": product, "product_qty": 1, "price_unit": 8,
            "date_planned": "2026-11-26 12:00:00",
        })],
    })
    assert adapter.confirm_purchase_order(second_order) is True
    try:
        models.execute_kw(DB, uid, PASSWORD, "purchase.order", "button_cancel",
                          [[second_order]])
    except xmlrpc.client.Fault:
        pass
    # A fresh invite code gives this run its own principal and tenant.  Reusing a
    # fixed code made the test read tasks and attentions left by earlier runs,
    # which is not evidence that this run produced any.
    invite = f"odoo20-api-owner-{uuid.uuid4().hex[:10]}"
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_SESSION_SECRET": "odoo20-api-session-secret-0123456789",
        "FORGEFLOW_INVITE_CODES": invite,
        "FORGEFLOW_SECURE_COOKIE": "false",
    })
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        session = client.post("/api/auth/session", json={"invite_code": invite})
        assert session.status_code == 201
        bound = client.post("/api/odoo20/binding", json={
            "odoo_user_id": 2, "role": "admin",
            "admin_token": "live-binding-admin-token"})
        assert bound.status_code == 200, bound.text
        assert client.get("/api/odoo20/binding").json()["role"] == "admin"
        from docx import Document
        document = Document()
        document.add_paragraph("API supplier document")
        payload = io.BytesIO()
        document.save(payload)
        filename = f"supplier-{uuid.uuid4().hex}.docx"
        uploaded = client.post(
            "/api/knowledge/documents",
            files={"file": (filename, payload.getvalue(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            data={"acl_scope": "project"})
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["version"] == 1
        assert any(row["source_path"] == filename
                   for row in client.get("/api/knowledge/documents").json())
        subscription = client.post("/api/sources/subscriptions", json={
            "source_type": "odoo20.purchase_order", "interval_seconds": 30})
        assert subscription.status_code == 200, subscription.text
        scanned = client.post("/api/sources/scan")
        assert scanned.status_code == 200, scanned.text
        # A first scan reports a result per enabled source even when no business
        # state changed yet; it is not a promise that an event was found.
        assert isinstance(scanned.json()["scanned"], list)
        tasks = client.get("/api/tasks")
        assert tasks.status_code == 200 and isinstance(tasks.json(), list)
        health = client.get("/api/sources/health")
        assert health.status_code == 200 and health.json()["healthy"] is True
        rules = client.get("/api/rules")
        assert rules.status_code == 200 and isinstance(rules.json(), list)
        # Preparing the plan is what opens the decision attention for this task.
        prepared = client.post("/api/odoo20/plans", json={
            "purchase_order_id": order, "reason": "supplier cancellation API test"})
        assert prepared.status_code == 200, prepared.text
        plan_id = prepared.json()["plan_id"]
        assert prepared.json()["task"]["attention"] == "open"
        ticket_a = prepared.json()["task"]["ticket_id"]
        listed = client.get("/api/odoo20/tasks")
        assert listed.status_code == 200
        assert any(row["plan_id"] == plan_id and row["attention"] == "open" for row in listed.json())
        # A second plan gives a second, independent attention so the explicit
        # answer endpoint can be exercised without consuming this plan's own
        # approval attention.
        other = client.post("/api/odoo20/plans", json={
            "purchase_order_id": second_order, "reason": "second live plan"})
        assert other.status_code == 200, other.text
        ticket_b = other.json()["task"]["ticket_id"]
        assert ticket_b != ticket_a
        attentions = client.get("/api/tasks/attentions")
        assert attentions.status_code == 200 and attentions.json(), attentions.text
        other_attention = next(row for row in attentions.json()
                               if str(row["ticket_id"]) == str(ticket_b))
        answered = client.post(
            f"/api/tasks/attentions/{other_attention['attention_id']}/answer")
        assert answered.status_code == 200 and answered.json()["state"] == "answered"
        approved = client.post(f"/api/odoo20/plans/{plan_id}/approve", json={
            "comment": "API live approval",
            "expected_plan_version": prepared.json()["plan_version"],
            "expected_plan_digest": prepared.json()["plan_digest"],
            "expected_action_digest": prepared.json()["action_digest"]})
        assert approved.status_code == 200, approved.text
        assert approved.json()["task"]["attention"] == "answered"
        current = client.get(f"/api/odoo20/plans/{plan_id}")
        assert current.status_code == 200 and current.json()["task"]["attention"] == "answered"
        applied = client.post(f"/api/odoo20/plans/{plan_id}/apply", json={
            "idempotency_key": f"api-op-{order}"})
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["state"] == "succeeded" and body["verification"] == "verified"
        assert adapter.search_read("purchase.order", [("id", "=", order)], ["state"])[0]["state"] == "cancel"
