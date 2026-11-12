"""Knowledge tenant isolation, ACL revocation, publication and withdrawal."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from forgeflow.knowledge.catalog import KnowledgeCatalog, PublicationConflict
from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.knowledge.semantic_answer import (
    KnowledgeScope,
    resolve_acl_scopes,
    semantic_answer,
)
from forgeflow.semantic import SemanticConfig
from forgeflow.superagent import ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _config(tenant_id: str = "forgeflow-project") -> SemanticConfig:
    """A config bound to one tenant; the tenant is never a request parameter."""
    return SemanticConfig(
        qdrant_url="https://qdrant.example",
        qdrant_api_key="qdrant-secret",
        tenant_id=tenant_id,
        embedding_url="https://embed.example/v1/embeddings",
        embedding_model="Qwen/Qwen3-Embedding-8B",
        embedding_api_key="semantic-secret",
        reranker_url="https://rerank.example/v1/rerank",
        reranker_model="Qwen/Qwen3-Reranker-8B",
        reranker_api_key="semantic-secret",
    )


def _tenant(store: ControlStore, prefix: str) -> str:
    tenant = f"{prefix}-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, prefix, 1, (("planner-a", "Planner", "planner"),))
    return tenant


def _drop(store: ControlStore, *tenants: str) -> None:
    with store.connection.transaction():
        for tenant in tenants:
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
    store.close()


def _doc(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_same_filename_in_two_tenants_does_not_collide(tmp_path: Path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant_a, tenant_b = _tenant(store, "ka"), _tenant(store, "kb")
    try:
        catalog = DocumentCatalog(store)
        path_a = _doc(tmp_path, "supplier.md", "tenant A lead time is four days")
        path_b = _doc(tmp_path, "supplier2.md", "tenant B lead time is nine days")
        record_a = catalog.ingest_file(tenant_id=tenant_a, source_path="supplier.md",
                                       path=path_a)
        record_b = catalog.ingest_file(tenant_id=tenant_b, source_path="supplier.md",
                                       path=path_b)
        assert record_a.id != record_b.id
        assert catalog.list_documents(tenant_a)[0]["version"] == 1
        assert catalog.delete(tenant_id=tenant_a, source_path="supplier.md") is True
        remaining = catalog.list_documents(tenant_b)
        assert [row["source_path"] for row in remaining] == ["supplier.md"]
    finally:
        _drop(store, tenant_a, tenant_b)


def test_empty_authorised_source_set_reads_nothing(tmp_path: Path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr("forgeflow.knowledge.semantic_answer.search",
                        lambda *a, **k: called.append(1) or [])
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store, "k-empty")
    try:
        result = semantic_answer(
            "lead time", tmp_path, config=_config(tenant), store=store,
            scope=KnowledgeScope(tenant, generation="gen-1",
                                 source_paths=frozenset()))
        assert result["found"] is False and result["reason"] == "no_readable_sources"
        assert called == [], "an empty source set must not reach the vector store"
    finally:
        _drop(store, tenant)


def test_acl_revocation_takes_effect_on_the_next_query() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store, "k-acl")
    try:
        assert resolve_acl_scopes(tenant, "planner", store=store) == frozenset({"project"})
        with store.transaction() as cursor:
            cursor.execute(
                "DELETE FROM knowledge_role_scopes WHERE tenant_id=%s AND role='planner'",
                (tenant,))
        assert resolve_acl_scopes(tenant, "planner", store=store) == frozenset()
    finally:
        _drop(store, tenant)


def test_acl_revocation_blocks_retrieval_entirely(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("forgeflow.knowledge.semantic_answer.search",
                        lambda *a, **k: [{"path": "doc.md", "start_line": 1,
                                          "end_line": 1, "text": "x", "score": 1.0}])
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store, "k-acl2")
    try:
        with store.transaction() as cursor:
            cursor.execute(
                "DELETE FROM knowledge_role_scopes WHERE tenant_id=%s AND role='observer'",
                (tenant,))
        result = semantic_answer("q", tmp_path, config=_config(tenant), store=store,
                                 scope=KnowledgeScope(tenant, generation="g",
                                                      acl_scopes=frozenset()))
        assert result["found"] is False and result["reason"] == "no_authorized_scope"
    finally:
        _drop(store, tenant)


def test_no_published_generation_reads_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("forgeflow.knowledge.semantic_answer.search",
                        lambda *a, **k: [{"path": "doc.md", "start_line": 1,
                                          "end_line": 1, "text": "x", "score": 1.0}])
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store, "k-gen")
    try:
        result = semantic_answer("q", tmp_path, config=_config(tenant), store=store,
                                 scope=KnowledgeScope(tenant))
        assert result["found"] is False and result["reason"] == "no_published_generation"
    finally:
        _drop(store, tenant)


def test_withdrawal_removes_logical_visibility_before_vectors() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store, "k-withdraw")
    try:
        catalog = DocumentCatalog(store)
        knowledge = KnowledgeCatalog(store)
        tmp = Path("/tmp") / f"withdraw-{uuid.uuid4().hex}.md"
        tmp.write_text("contract clause 7", encoding="utf-8")
        catalog.ingest_file(tenant_id=tenant, source_path="contract.md", path=tmp)
        generation = knowledge.new_generation(tenant)
        knowledge.publish(tenant, generation)
        catalog.mark_indexed(tenant_id=tenant, source_path="contract.md",
                             generation=generation)
        state = catalog.document_state(tenant_id=tenant, source_path="contract.md")
        assert state["searchable"] is True and state["indexed"] is True
        withdrawal = catalog.withdraw(tenant_id=tenant, source_path="contract.md")
        assert withdrawal["vector_path"] == "document://contract.md"
        state = catalog.document_state(tenant_id=tenant, source_path="contract.md")
        assert state["withdrawn"] is True and state["searchable"] is False
        # The document is already invisible even though vector cleanup has not run.
        assert catalog.list_documents(tenant) [0]["status"] == "withdrawn"
        tmp.unlink()
    finally:
        _drop(store, tenant)


def test_concurrent_publication_conflict_is_detected() -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store, "k-publish")
    try:
        catalog = KnowledgeCatalog(store)
        first = catalog.new_generation(tenant)
        published = catalog.publish(tenant, first)
        assert published.sequence >= 1
        second = catalog.new_generation(tenant)
        # A publisher that prepared before the first publish must not win.
        with pytest.raises(PublicationConflict, match="publication_stale"):
            catalog.publish(tenant, second, expected_sequence=published.sequence - 1)
        newer = catalog.publish(tenant, second, expected_sequence=published.sequence)
        assert newer.sequence > published.sequence
        assert catalog.published(tenant) == second
        states = {row["generation"]: row["state"] for row in catalog.list_generations(tenant)}
        assert states[first] == "retired" and states[second] == "published"
    finally:
        _drop(store, tenant)


def test_document_states_are_reported_separately(tmp_path: Path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store, "k-states")
    try:
        catalog = DocumentCatalog(store)
        path = _doc(tmp_path, "sop.md", "# Receiving\nCheck the delivery against the PO.\n")
        catalog.ingest_file(tenant_id=tenant, source_path="sop.md", path=path)
        state = catalog.document_state(tenant_id=tenant, source_path="sop.md")
        assert state["saved"] and state["parsed"]
        assert state["indexed"] is False and state["searchable"] is False
        assert state["needs_review"] is False
        blocks = catalog.read_blocks(tenant_id=tenant, source_path="sop.md")
        assert [block["block_type"] for block in blocks] == ["heading", "line"]
        assert blocks[0]["locator"] == "md:l1" and blocks[1]["locator"] == "md:l2"
        # The list projection carries the same six lifecycle states, so the
        # interface never has to infer them from raw columns.
        lifecycle = catalog.list_documents(tenant)[0]["lifecycle"]
        assert lifecycle == {"saved": True, "parsed": True, "needs_review": False,
                             "indexed": False, "searchable": False, "withdrawn": False}
    finally:
        _drop(store, tenant)
