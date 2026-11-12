"""Knowledge browsing preserves authorization and the cited historical version."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def knowledge_catalog(monkeypatch):
    from forgeflow.knowledge import documents_catalog, semantic_answer
    from forgeflow.superagent import bindings, store

    state = SimpleNamespace(scopes=frozenset({"project"}), reads=[])

    class Catalog:
        def __init__(self, _store):
            pass

        def list_documents(self, tenant_id):
            assert tenant_id == "reader-tenant"
            return [
                {"source_path": "reference/policy.md", "filename": "policy.md",
                 "version": 2, "acl_scope": "project"},
                {"source_path": "restricted.md", "filename": "restricted.md",
                 "version": 1, "acl_scope": "engineering"},
            ]

        def list_versions(self, *, tenant_id, source_path):
            assert tenant_id == "reader-tenant"
            return [{"version": 2}, {"version": 1}]

        def read_blocks(self, *, tenant_id, source_path, version):
            state.reads.append((tenant_id, source_path, version))
            return [{"ord": 0, "version": version,
                     "text": "original cited text" if version == 1 else "replacement text"}]

    monkeypatch.setenv("FORGEFLOW_CONTROL_DATABASE_URL", "test")
    monkeypatch.setattr(store, "ControlStore", lambda _: SimpleNamespace(
        migrate=lambda: None, close=lambda: None))
    monkeypatch.setattr(documents_catalog, "DocumentCatalog", Catalog)
    monkeypatch.setattr(bindings, "tenant_for_principal", lambda *a, **k: "reader-tenant")
    monkeypatch.setattr(bindings, "role_for_principal", lambda *a, **k: "observer")
    monkeypatch.setattr(semantic_answer, "resolve_acl_scopes", lambda *a, **k: state.scopes)
    return state


def _auth(client, settings):
    response = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[0]})
    assert response.status_code == 201
    return {"cookie": response.headers["set-cookie"].split(";", 1)[0]}


def test_catalog_and_versions_enforce_the_same_acl_as_document_text(
        client, settings, knowledge_catalog):
    auth = _auth(client, settings)
    response = client.get("/api/knowledge/documents", headers=auth)
    assert response.status_code == 200
    assert [row["source_path"] for row in response.json()] == ["reference/policy.md"]
    for suffix in ("versions", "blocks"):
        denied = client.get(f"/api/knowledge/documents/restricted.md/{suffix}", headers=auth)
        assert denied.status_code == 404
    assert knowledge_catalog.reads == []


def test_historical_version_returns_its_own_number_and_original_text(
        client, settings, knowledge_catalog):
    auth = _auth(client, settings)
    response = client.get("/api/knowledge/documents/reference%2Fpolicy.md/blocks?version=1",
                          headers=auth)
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert response.json()["blocks"][0]["text"] == "original cited text"
    assert knowledge_catalog.reads == [("reader-tenant", "reference/policy.md", 1)]


@pytest.mark.parametrize(("query", "status"), [("?version=999", 404), ("?version=0", 422),
                                                ("?version=-1", 422)])
def test_missing_or_invalid_version_never_falls_back_to_latest(
        client, settings, knowledge_catalog, query, status):
    response = client.get("/api/knowledge/documents/reference%2Fpolicy.md/blocks" + query,
                          headers=_auth(client, settings))
    assert response.status_code == status
    assert knowledge_catalog.reads == []


def test_acl_revocation_hides_metadata_and_saved_version_on_next_request(
        client, settings, knowledge_catalog):
    auth = _auth(client, settings)
    assert client.get("/api/knowledge/documents", headers=auth).json()
    knowledge_catalog.scopes = frozenset()
    assert client.get("/api/knowledge/documents", headers=auth).json() == []
    for suffix in ("versions", "blocks?version=1"):
        assert client.get("/api/knowledge/documents/reference%2Fpolicy.md/" + suffix,
                          headers=auth).status_code == 404


def test_current_document_reads_current_version(client, settings, knowledge_catalog):
    response = client.get("/api/knowledge/documents/reference%2Fpolicy.md/blocks",
                          headers=_auth(client, settings))
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["blocks"][0]["text"] == "replacement text"
