"""Knowledge tools resolve tenant/role from each task's ToolScope."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.knowledge.catalog import KnowledgeCatalog
from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.superagent import ControlStore
from forgeflow.superagent.identity import ObjectScope, ToolScope
from forgeflow.superagent.tools import ScopedKnowledgeToolBox

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _tenant(store, role="planner"):
    tenant = f"scope-{uuid.uuid4().hex}"
    store.migrate()
    store.seed_tenant(tenant, "Scope", 1, ((role, "User", role),))
    with store.connection.transaction():
        store.connection.execute(
            "INSERT INTO knowledge_role_scopes(tenant_id, role, acl_scope) "
            "VALUES (%s,%s,'project') ON CONFLICT DO NOTHING", (tenant, role))
    return tenant


def _scope(tenant, role="planner"):
    return ToolScope(tenant_id=tenant, principal_id=role, ticket_id="t1",
                     assignment_id="a1", mandate_id="m1", capability="assess",
                     allowed_tools=frozenset({"knowledge_search",
                                              "knowledge_read_source"}),
                     object_scope=ObjectScope.all_objects(), budget_tokens=1000,
                     expires_at=0.0, run_token="run-1")


def test_scoped_tools_read_only_tenant_acls_and_use_published_generation(
        tmp_path, monkeypatch) -> None:
    import importlib

    answer_module = importlib.import_module("forgeflow.knowledge.answer")
    real_answer = answer_module.answer

    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    other = _tenant(store)
    source = tmp_path / "supplier.md"
    source.write_text("# Supplier note\n\nCopper lead time is 21 days.\n",
                      encoding="utf-8")
    try:
        DocumentCatalog(store).ingest_file(
            tenant_id=tenant, source_path="supplier.md", path=source,
            acl_scope="project")
        catalog = KnowledgeCatalog(store)
        generation = catalog.new_generation(tenant)
        catalog.publish(tenant, generation, actor="test", document_count=1,
                        point_count=0)
        captured = {}

        def fake_answer(question, root, limit=5, scope=None):
            captured["tenant"] = scope.tenant_id
            captured["principal"] = scope.principal_id
            captured["acl"] = set(scope.acl_scopes or ())
            return {"passages": [{"text": "21 days"}], "citations": ["supplier.md"]}

        monkeypatch.setattr(answer_module, "answer", fake_answer)
        box = ScopedKnowledgeToolBox(store=store, root=tmp_path)
        found = box.call("knowledge_read_source", {"source_path": "supplier.md"},
                         scope=_scope(tenant))
        assert found["result"]["blocks"]
        searched = box.call("knowledge_search", {"query": "lead time"},
                            scope=_scope(tenant))
        assert captured["tenant"] == tenant and captured["principal"] == "planner"
        assert captured["acl"] == {"project"}
        assert searched["citations"] == ["supplier.md"]
        with pytest.raises(Exception, match="knowledge_unavailable"):
            box.call("knowledge_search", {"query": "x"}, scope=_scope(other))
        # KnowledgeCatalog is the authority for generation state; the tool does
        # not pass a DocumentCatalog into published().
        assert real_answer is not fake_answer
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_tenants WHERE id=ANY(%s)", ([tenant, other],))
        store.close()
