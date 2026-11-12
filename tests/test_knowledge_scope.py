"""Live per-principal tenant resolution. Requires the control database."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.knowledge.scope import server_scope
from forgeflow.superagent import ControlStore, bind_odoo_user

CONTROL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not CONTROL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_scope_uses_principal_binding(monkeypatch) -> None:
    for key, value in {
        "QDRANT_URL": "https://qdrant.example", "QDRANT_API_KEY": "q",
        "SEMANTIC_API_KEY": "s", "EMBEDDING_API_URL": "https://embed.example/v1",
        "EMBEDDING_MODEL_ID": "embed", "RERANKER_API_URL": "https://rerank.example/v1",
        "RERANKER_MODEL_ID": "rerank", "FORGEFLOW_KNOWLEDGE_TENANT": "forgeflow-project",
    }.items():
        monkeypatch.setenv(key, value)
    principal = f"scope-{uuid.uuid4().hex}"
    store = ControlStore(CONTROL)
    try:
        store.migrate()
        # This test used to rely on ambient tenant state; seed what it needs so
        # it is meaningful against an empty control database.
        with store.connection.transaction():
            store.connection.execute(
                "INSERT INTO control_tenants(id, display_name, policy_version) "
                "VALUES ('forgeflow-project', 'ForgeFlow project', 1) "
                "ON CONFLICT (id) DO NOTHING")
        bind_odoo_user(store, principal_id=principal, tenant_id="forgeflow-project",
                       odoo_user_id=2, role="approver")
        scope = server_scope("zh", principal_id=principal)
        assert scope is not None and scope.tenant_id == "forgeflow-project"
        assert scope.role == "approver"
        with pytest.raises(PermissionError, match="binding_required"):
            server_scope("zh", principal_id=f"unbound-{uuid.uuid4().hex}")
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_odoo_bindings WHERE principal_id=%s", (principal,))
        store.close()


def test_an_account_role_overrides_the_invite_binding(monkeypatch) -> None:
    for key, value in {
        "QDRANT_URL": "https://qdrant.example", "QDRANT_API_KEY": "q",
        "SEMANTIC_API_KEY": "s", "EMBEDDING_API_URL": "https://embed.example/v1",
        "EMBEDDING_MODEL_ID": "embed", "RERANKER_API_URL": "https://rerank.example/v1",
        "RERANKER_MODEL_ID": "rerank", "FORGEFLOW_KNOWLEDGE_TENANT": "forgeflow-project",
    }.items():
        monkeypatch.setenv(key, value)
    principal = f"scope-role-{uuid.uuid4().hex}"
    store = ControlStore(CONTROL)
    try:
        store.migrate()
        bind_odoo_user(store, principal_id=principal, tenant_id="forgeflow-project",
                       odoo_user_id=2, role="approver")
        observer = server_scope("zh", principal_id=principal, account_role="observer")
        assert observer is not None and observer.role == "observer"
        assert observer.acl_scopes == frozenset({"project"})
        admin = server_scope("zh", principal_id=principal, account_role="admin")
        assert admin is not None and admin.role == "admin"
        assert admin.acl_scopes == frozenset({"project", "engineering"})
        with pytest.raises(ValueError, match="invalid_knowledge_role"):
            server_scope("zh", principal_id=principal, account_role="root")
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_odoo_bindings WHERE principal_id=%s", (principal,))
        store.close()
