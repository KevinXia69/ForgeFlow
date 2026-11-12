"""Hosted import authority and company boundaries, with no real ERP writes."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings
from forgeflow.onboarding_import import WRITABLE_FIELDS
from forgeflow.superagent.identity import Role


class ImportAdapter:
    def __init__(self):
        self.records = {model: {} for model in [*WRITABLE_FIELDS, "res.company"]}
        self.records["res.company"] = {1: {"id": 1}, 2: {"id": 2}}
        self.companies = (1,)
        self.creates = []
        self.writes = []
        self.reads = []
        self.next_id = 100
        self.selected_company = None

    def for_company(self, company_id):
        self.selected_company = company_id
        return self

    def user_company_ids(self, uid):
        return self.companies

    def search_read(self, model, domain, fields, limit=100):
        self.reads.append((model, domain))

        def match(row):
            tokens = iter(domain)

            def expression(token):
                if token in ("|", "&"):
                    left, right = expression(next(tokens)), expression(next(tokens))
                    return left or right if token == "|" else left and right
                field, operator, expected = token
                actual = row.get(field, False)
                if operator == "=":
                    return actual == expected
                if operator == "in":
                    return actual in expected
                raise AssertionError(operator)

            return all([expression(token) for token in tokens])

        return [{field: row.get(field, False) for field in fields} | {"id": row["id"]}
                for row in self.records[model].values() if match(row)][:limit]

    def create(self, model, values):
        self.creates.append((model, dict(values)))
        self.next_id += 1
        self.records[model][self.next_id] = {"id": self.next_id, **values}
        return self.next_id

    def write(self, model, ids, values):
        self.writes.append((model, ids, dict(values)))
        for identifier in ids:
            self.records[model][identifier].update(values)
        return True


@pytest.fixture
def imports(tmp_path, monkeypatch):
    from forgeflow.integrations import odoo20
    from forgeflow.superagent import bindings, store

    adapter = ImportAdapter()
    identity = SimpleNamespace(role="admin", odoo_uid=7, tenant="tenant-a")
    for key in ("URL", "DB", "LOGIN", "PASSWORD"):
        monkeypatch.setenv(f"FORGEFLOW_ODOO20_{key}", "synthetic")
    monkeypatch.setenv("FORGEFLOW_CONTROL_DATABASE_URL", "synthetic")
    monkeypatch.setattr(odoo20, "Odoo20Adapter", lambda config: adapter)
    monkeypatch.setattr(store, "ControlStore", lambda url: SimpleNamespace(
        migrate=lambda: None, close=lambda: None))
    monkeypatch.setattr(bindings, "principal_context", lambda *a, **kw:
                        SimpleNamespace(tenant_id=identity.tenant,
                                        roles=frozenset({Role(identity.role)}))
                        if identity.role else None)
    monkeypatch.setattr(bindings, "odoo_user_id_for", lambda *a, **kw: identity.odoo_uid)
    settings = create_settings({
        "FORGEFLOW_DEPLOYMENT": "hosted", "FORGEFLOW_TRUSTED_HOSTS": "testserver",
        "FORGEFLOW_ENVIRONMENT_GENERATION": "a" * 32,
        "FORGEFLOW_SESSION_SECRET": "synthetic-session-secret-0123456789",
        "FORGEFLOW_INVITE_CODES": "synthetic-import-invite-0001,synthetic-import-invite-0002",
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
    })
    with TestClient(create_app(settings)) as client:
        login = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[0]})
        auth = {"cookie": login.headers["set-cookie"].split(";", 1)[0]}
        yield SimpleNamespace(client=client, auth=auth, adapter=adapter, identity=identity,
                              settings=settings)


def workspace(imports, *, csv=b"name,ref,email\nSynthetic,EXT-1,new@example.test\n",
              model="res.partner"):
    client, auth = imports.client, imports.auth
    response = client.post("/api/workspaces", json={
        "name": "Synthetic import", "industry": "test", "modules": []}, headers=auth)
    assert response.status_code == 201, response.text
    url = "/api/workspaces/" + response.json()["id"]
    response = client.post(url + "/sources", json={"filename": "synthetic.csv",
        "media_type": "text/csv", "content": base64.b64encode(csv).decode()}, headers=auth)
    assert response.status_code == 200, response.text
    mappings = [{"id": row["id"], "target_model": model,
                 "target_field": row["source_field"]} for row in response.json()["mappings"]]
    response = client.post(url + "/confirm", json={"mappings": mappings}, headers=auth)
    assert response.status_code == 200, response.text
    return url


def preview(imports, url, query=""):
    response = imports.client.post(url + "/import/preview" + query, headers=imports.auth)
    assert response.status_code == 200, response.text
    return response.json()["import"]


@pytest.mark.parametrize("role", [None, "observer"])
@pytest.mark.parametrize("stage", ["preview", "authorize", "apply", "readback"])
def test_unbound_or_observer_cannot_read_authorize_or_execute_import(imports, role, stage):
    url = workspace(imports)
    imports.identity.role = role
    response = imports.client.post(url + "/import/" + stage,
        json={"preview_sha256": "a" * 64}, headers=imports.auth)
    assert response.status_code == 403
    assert imports.adapter.creates == imports.adapter.writes == imports.adapter.reads == []


def test_admin_import_is_company_bound_and_idempotent(imports):
    url = workspace(imports)
    block = preview(imports, url)
    assert block["preview"]["importable"]
    assert block["preview"]["access_scope"]["company_id"] == 1
    body = {"preview_sha256": block["preview_sha256"]}
    for stage in ("authorize", "apply", "apply", "readback"):
        response = imports.client.post(url + "/import/" + stage, json=body, headers=imports.auth)
        assert response.status_code == 200, response.text
    assert response.json()["status"] == "readback_passed"
    assert len(imports.adapter.creates) == 1
    assert imports.adapter.creates[0][1]["company_id"] == 1
    assert imports.adapter.selected_company == 1


@pytest.mark.parametrize("change", ["observer", "planner", "company", "user", "tenant", "unbound"])
def test_authority_is_rechecked_after_preview_before_any_write(imports, change):
    url = workspace(imports)
    block = preview(imports, url)
    body = {"preview_sha256": block["preview_sha256"]}
    assert imports.client.post(url + "/import/authorize", json=body,
                               headers=imports.auth).status_code == 200
    if change in ("observer", "planner"):
        imports.identity.role = change
    elif change == "company":
        imports.adapter.companies = (2,)
    elif change == "user":
        imports.identity.odoo_uid = 8
    elif change == "tenant":
        imports.identity.tenant = "tenant-b"
    else:
        imports.identity.odoo_uid = None
    result = imports.client.post(url + "/import/apply", json=body, headers=imports.auth)
    assert result.status_code == 403, result.text
    assert imports.adapter.creates == imports.adapter.writes == []


def test_multi_company_requires_explicit_selection_and_rejects_foreign_company(imports):
    url = workspace(imports)
    imports.adapter.companies = (1, 2)
    for query in ("", "?company_id=3"):
        response = imports.client.post(url + "/import/preview" + query, headers=imports.auth)
        assert response.status_code == 403
    block = preview(imports, url, "?company_id=2")
    assert block["preview"]["rows"][0]["values"]["company_id"] == 2


def test_foreign_identity_is_not_read_or_updated_and_shared_update_is_blocked(imports):
    imports.adapter.records["res.partner"][1] = {
        "id": 1, "name": "Foreign", "ref": "EXT-1", "company_id": 2}
    url = workspace(imports)
    block = preview(imports, url)
    assert block["preview"]["rows"][0]["action"] == "create"
    assert "Foreign" not in str(block)
    imports.adapter.records["res.partner"][2] = {
        "id": 2, "name": "Shared", "ref": "EXT-1", "company_id": False}
    block = preview(imports, url)
    assert not block["preview"]["importable"]
    assert "import_record_scope_denied" in block["preview"]["rows"][0]["problems"]


def test_foreign_relation_cannot_be_imported_by_id(imports):
    imports.adapter.records["res.partner"][12] = {
        "id": 12, "name": "Foreign vendor", "company_id": 2}
    url = workspace(imports, csv=b"name,partner_id\nPO-SYNTHETIC,12\n",
                    model="purchase.order")
    block = preview(imports, url)
    assert not block["preview"]["importable"]
    assert "relation_not_found:partner_id" in block["preview"]["rows"][0]["problems"]


def test_record_moved_outside_company_after_preview_is_not_written(imports):
    imports.adapter.records["res.partner"][1] = {
        "id": 1, "name": "Old", "ref": "EXT-1", "company_id": 1}
    url = workspace(imports)
    block = preview(imports, url)
    assert block["preview"]["rows"][0]["action"] == "update"
    body = {"preview_sha256": block["preview_sha256"]}
    assert imports.client.post(url + "/import/authorize", json=body,
                               headers=imports.auth).status_code == 200
    imports.adapter.records["res.partner"][1]["company_id"] = 2
    result = imports.client.post(url + "/import/apply", json=body, headers=imports.auth)
    assert result.status_code == 200
    assert result.json()["status"] == "import_partial"
    assert imports.adapter.writes == []


def test_other_principal_cannot_authorize_owned_preview(imports):
    url = workspace(imports)
    block = preview(imports, url)
    login = imports.client.post("/api/auth/session", json={
        "invite_code": imports.settings.invite_codes[1]})
    response = imports.client.post(url + "/import/authorize", json={
        "preview_sha256": block["preview_sha256"]}, headers={
        "cookie": login.headers["set-cookie"].split(";", 1)[0]})
    assert response.status_code == 404
    assert imports.adapter.creates == imports.adapter.writes == []


@pytest.mark.parametrize(("role", "status", "can_authorize"), [
    (None, 403, None), ("observer", 403, None), ("planner", 200, False),
    ("approver", 200, True), ("admin", 200, True),
])
def test_company_selector_returns_only_bound_companies(imports, role, status, can_authorize):
    imports.identity.role = role
    response = imports.client.get("/api/onboarding/companies", headers=imports.auth)
    assert response.status_code == status
    if status == 200:
        assert [row["id"] for row in response.json()["companies"]] == [1]
        assert response.json()["can_authorize"] is can_authorize


def test_partial_import_can_retry_after_readback_using_same_authorized_digest(imports):
    url = workspace(imports, csv=b"name,ref\nAlpha,A-1\nBeta,B-2\n")
    block = preview(imports, url)
    body = {"preview_sha256": block["preview_sha256"]}
    assert imports.client.post(url + "/import/authorize", json=body,
                               headers=imports.auth).status_code == 200
    create = imports.adapter.create
    fail = [True]

    def flaky(model, values):
        if fail.pop() if fail else False:
            raise RuntimeError("temporary_unavailable")
        return create(model, values)

    imports.adapter.create = flaky
    result = imports.client.post(url + "/import/apply", json=body, headers=imports.auth)
    assert result.json()["status"] == "import_partial"
    result = imports.client.post(url + "/import/readback", headers=imports.auth)
    assert result.json()["status"] == "readback_failed"
    rejected = imports.client.post(url + "/import/apply", json={
        "preview_sha256": "f" * 64}, headers=imports.auth)
    assert rejected.status_code == 409
    result = imports.client.post(url + "/import/apply", json=body, headers=imports.auth)
    assert result.json()["status"] == "imported"
    assert len(imports.adapter.creates) == 2
    result = imports.client.post(url + "/import/readback", headers=imports.auth)
    assert result.json()["status"] == "readback_passed"
    assert imports.client.post(url + "/import/apply", json=body,
                               headers=imports.auth).json()["status"] == "readback_passed"
    assert len(imports.adapter.creates) == 2
