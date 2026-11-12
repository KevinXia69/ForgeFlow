"""Business search preserves relation matches and bound company restrictions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _matches(domain, row):
    tokens = iter(domain)

    def expression(token):
        if token in ("|", "&"):
            left, right = expression(next(tokens)), expression(next(tokens))
            return left or right if token == "|" else left and right
        field, operator, value = token
        actual = row.get(field)
        if operator == "ilike":
            return str(value).casefold() in str(actual or "").casefold()
        if operator == "in":
            return actual in value
        raise AssertionError(operator)

    return all([expression(token) for token in tokens])


@pytest.fixture
def business_adapter(monkeypatch):
    from forgeflow.integrations import odoo20
    from forgeflow.superagent import bindings, store

    class Adapter:
        def __init__(self):
            self.calls = []
            self.companies = (1,)
            self.orders = [
                {"id": 1, "name": "PO-ALPHA", "partner_id.name": "Vendor One",
                 "state": "purchase", "company_id": 1},
                {"id": 2, "name": "PO-BETA", "partner_id.name": "Vendor Two",
                 "state": "cancel", "company_id": 1},
                {"id": 3, "name": "PO-FOREIGN", "partner_id.name": "Vendor One",
                 "state": "purchase", "company_id": 2},
                {"id": 4, "name": "PO-DONE", "partner_id.name": "Vendor One",
                 "state": "done", "company_id": 1},
            ]
            self.lines = [
                {"product_id.name": "Special component", "order_id.state": "purchase",
                 "order_id.company_id": company, "order_id": [identifier, "PO"]}
                for identifier, company in ((1, 1), (3, 2))
            ]

        def user_company_ids(self, user):
            assert user == 7
            return self.companies

        def search_read(self, model, domain, fields, **kwargs):
            self.calls.append((model, domain))
            rows = self.lines if model == "purchase.order.line" else self.orders
            return [row for row in rows if _matches(domain, row)][:kwargs["limit"]]

    adapter = Adapter()
    for name in ("URL", "DB", "LOGIN", "PASSWORD"):
        monkeypatch.setenv(f"FORGEFLOW_ODOO20_{name}", "test")
    monkeypatch.setenv("FORGEFLOW_CONTROL_DATABASE_URL", "test")
    monkeypatch.setattr(odoo20, "Odoo20Adapter", lambda config: adapter)
    monkeypatch.setattr(store, "ControlStore", lambda url: SimpleNamespace(
        migrate=lambda: None, close=lambda: None))
    monkeypatch.setenv("FORGEFLOW_CONTROL_TENANT_ID", "unrelated-default-tenant")
    monkeypatch.setattr(bindings, "principal_context", lambda *a, **kw:
                        SimpleNamespace(tenant_id="bound-principal-tenant"))

    def bound_user(*a, **kwargs):
        assert kwargs["tenant_id"] == "bound-principal-tenant"
        return 7

    monkeypatch.setattr(bindings, "odoo_user_id_for", bound_user)
    return adapter


@pytest.mark.parametrize(("query", "expected"), [
    ("purchase", [1]), (" cancel ", [2]), ("PURCH", [1]),
    ("PO-BETA", [2]), ("Vendor One", [1]), ("Special component", [1]),
    ("", [1, 2]), ("done", []), ("not present", []),
])
def test_purchase_search_matches_state_order_vendor_and_product_with_company_scope(
        client, settings, business_adapter, query, expected):
    response = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[0]})
    auth = {"cookie": response.headers["set-cookie"].split(";", 1)[0]}
    result = client.get("/api/odoo20/business-data", params={
        "kind": "purchase_order", "q": query, "limit": 50}, headers=auth)
    assert result.status_code == 200, result.text
    assert [row["id"] for row in result.json()["records"]] == expected
    assert ("company_id", "in", [1]) in business_adapter.calls[-1][1]
    if query.strip():
        assert ("order_id.company_id", "in", [1]) in business_adapter.calls[0][1]


def test_purchase_search_rejects_empty_bound_company_scope(client, settings, business_adapter):
    business_adapter.companies = ()
    response = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[0]})
    result = client.get("/api/odoo20/business-data?q=purchase", headers={
        "cookie": response.headers["set-cookie"].split(";", 1)[0]})
    assert result.status_code == 403
    assert result.json()["detail"] == "odoo_company_scope_missing"
    assert business_adapter.calls == []


@pytest.mark.parametrize("endpoint", [
    "/api/odoo20/business-data?kind=purchase_order", "/api/odoo20/business-data?kind=stock",
    "/api/odoo20/business-data?kind=sale_order", "/api/odoo20/business-data?kind=bom",
    "/api/odoo20/resolve?reference=PO-ALPHA",
])
@pytest.mark.parametrize("failure", ["unbound", "missing_user", "lookup_error", "missing_control", "control_error"])
def test_business_reads_fail_closed_when_company_scope_cannot_be_resolved(
        client, settings, business_adapter, monkeypatch, endpoint, failure):
    from forgeflow.superagent import bindings, store

    expected = 403
    if failure == "unbound":
        monkeypatch.setattr(bindings, "principal_context", lambda *a, **kw: None)
    elif failure == "missing_user":
        monkeypatch.setattr(bindings, "odoo_user_id_for", lambda *a, **kw: None)
    elif failure == "lookup_error":
        def unavailable(_user):
            raise RuntimeError("synthetic scope lookup failure")
        monkeypatch.setattr(business_adapter, "user_company_ids", unavailable)
        expected = 503
    elif failure == "missing_control":
        monkeypatch.delenv("FORGEFLOW_CONTROL_DATABASE_URL")
        expected = 503
    else:
        def unavailable(_url):
            raise RuntimeError("synthetic control connection failure")
        monkeypatch.setattr(store, "ControlStore", unavailable)
        expected = 503
    response = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[0]})
    result = client.get(endpoint, headers={"cookie": response.headers["set-cookie"].split(";", 1)[0]})
    assert result.status_code == expected, result.text
    assert business_adapter.calls == []
