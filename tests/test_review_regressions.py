"""Regression tests for three review findings.

P1  Tightening a document's ACL must take effect immediately, not after a reindex.
P2a Restarting a service must not re-enable a source an operator turned off.
P2b A stop that lands between storing a result and projecting it must win.
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest

from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.knowledge.semantic_answer import KnowledgeScope, semantic_answer
from forgeflow.semantic import SemanticConfig
from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Role,
    TenantContext,
    ensure_subscription,
    list_subscriptions,
    upsert_subscription,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _config(tenant: str) -> SemanticConfig:
    return SemanticConfig(
        qdrant_url="https://qdrant.example", qdrant_api_key="q",
        tenant_id=tenant,
        embedding_url="https://embed.example/v1",
        embedding_model="Qwen/Qwen3-Embedding-8B", embedding_api_key="s",
        reranker_url="https://rerank.example/v1",
        reranker_model="Qwen/Qwen3-Reranker-8B", reranker_api_key="s")


# --------------------------------------------------------------- P1: ACL window
def test_tightening_acl_hides_old_vectors_immediately(tmp_path: Path, monkeypatch) -> None:
    """Vectors still say ``project``; the current catalogue says ``engineering``."""
    store = ControlStore(DATABASE_URL)
    store.migrate()
    tenant = f"acl-window-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, "ACL window", 1, (("planner-a", "Planner", "planner"),))
    path = tmp_path / "contract.md"
    path.write_text("contract clause seven", encoding="utf-8")
    try:
        catalog = DocumentCatalog(store)
        catalog.ingest_file(tenant_id=tenant, source_path="contract.md", path=path,
                            acl_scope="project")
        # The indexer has not run again, so the retrieved passage still carries the
        # old scope.  The answer path must not trust it.
        monkeypatch.setattr(
            "forgeflow.knowledge.semantic_answer.search",
            lambda *args, **kwargs: [{
                "path": "document://contract.md", "start_line": 1, "end_line": 1,
                "text": "contract clause seven", "score": 0.9}])

        before = semantic_answer(
            "clause", tmp_path, limit=1, config=_config(tenant), store=store,
            scope=KnowledgeScope(tenant, generation="g1",
                                 acl_scopes=frozenset({"project"})))
        assert before["found"] is True

        # Tighten the document.  No reindex, no new generation.
        catalog.upsert(tenant_id=tenant, source_path="contract.md",
                       parsed=__import__(
                           "forgeflow.knowledge.documents", fromlist=["parse_document"]
                       ).parse_document(path),
                       raw=path.read_bytes(), acl_scope="engineering")

        after = semantic_answer(
            "clause", tmp_path, limit=1, config=_config(tenant), store=store,
            scope=KnowledgeScope(tenant, generation="g1",
                                 acl_scopes=frozenset({"project"})))
        assert after["found"] is False
        # The role that is allowed to see engineering still reads it.
        allowed = semantic_answer(
            "clause", tmp_path, limit=1, config=_config(tenant), store=store,
            scope=KnowledgeScope(tenant, generation="g1",
                                 acl_scopes=frozenset({"project", "engineering"})))
        assert allowed["found"] is True
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_withdrawn_document_is_not_returned_from_stale_vectors(
        tmp_path: Path, monkeypatch) -> None:
    store = ControlStore(DATABASE_URL)
    store.migrate()
    tenant = f"acl-withdraw-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, "Withdraw", 1, (("planner-a", "Planner", "planner"),))
    path = tmp_path / "sop.md"
    path.write_text("receiving procedure", encoding="utf-8")
    try:
        catalog = DocumentCatalog(store)
        catalog.ingest_file(tenant_id=tenant, source_path="sop.md", path=path)
        monkeypatch.setattr(
            "forgeflow.knowledge.semantic_answer.search",
            lambda *args, **kwargs: [{
                "path": "document://sop.md", "start_line": 1, "end_line": 1,
                "text": "receiving procedure", "score": 0.9}])
        scope = KnowledgeScope(tenant, generation="g1",
                               acl_scopes=frozenset({"project"}))
        assert semantic_answer("procedure", tmp_path, config=_config(tenant),
                               store=store, scope=scope)["found"] is True
        catalog.withdraw(tenant_id=tenant, source_path="sop.md")
        assert semantic_answer("procedure", tmp_path, config=_config(tenant),
                               store=store, scope=scope)["found"] is False
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_unmanaged_document_uri_cannot_be_verified(tmp_path: Path, monkeypatch) -> None:
    """A path with no catalogue row is not readable: absence is not permission."""
    store = ControlStore(DATABASE_URL)
    store.migrate()
    tenant = f"acl-unknown-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, "Unknown", 1, (("planner-a", "Planner", "planner"),))
    try:
        monkeypatch.setattr(
            "forgeflow.knowledge.semantic_answer.search",
            lambda *args, **kwargs: [{
                "path": "document://never-ingested.md", "start_line": 1, "end_line": 1,
                "text": "leaked", "score": 0.9}])
        result = semantic_answer(
            "leak", tmp_path, config=_config(tenant), store=store,
            scope=KnowledgeScope(tenant, generation="g1",
                                 acl_scopes=frozenset({"project", "engineering"})))
        assert result["found"] is False
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


# ------------------------------------------------- P2a: restart must not clobber
def test_restart_does_not_re_enable_a_disabled_source() -> None:
    store = ControlStore(DATABASE_URL)
    store.migrate()
    tenant = f"subs-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, "Subs", 1, (("planner-a", "Planner", "planner"),))
    try:
        created = ensure_subscription(store, tenant_id=tenant,
                                     source_type="odoo20.purchase_order",
                                     interval_seconds=60)
        assert created["enabled"] is True
        # An operator turns the source off and slows it down.
        upsert_subscription(store, tenant_id=tenant,
                            source_type="odoo20.purchase_order", enabled=False,
                            interval_seconds=900,
                            config={"policy": "observe", "horizon_days": 7})
        # A restart runs the bootstrap again.
        again = ensure_subscription(store, tenant_id=tenant,
                                    source_type="odoo20.purchase_order",
                                    interval_seconds=60)
        assert again["enabled"] is False
        assert int(again["interval_seconds"]) == 900
        assert again["config"] == {"policy": "observe", "horizon_days": 7}
        stored = list_subscriptions(store, tenant)
        assert len(stored) == 1
        assert stored[0]["enabled"] is False and stored[0]["interval_seconds"] == 900
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


# ------------------------------------------------ P2b: stop wins over projection
def test_attention_cannot_be_opened_on_a_cancelled_task() -> None:
    store = ControlStore(DATABASE_URL)
    store.migrate()
    tenant = f"race-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, "Race", 1, (("planner-a", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    engine = ControlEngine(store)
    try:
        admitted = engine.admit_trigger(ctx, source_id="t", external_event_id="r1",
                                        payload_hash="a" * 64, object_ref="PO-1",
                                        goal="race")
        engine.cancel_ticket(ctx, ticket_id=admitted.ticket_id)
        # The projection runs after the stop: it must not resurrect the task.
        assert engine.open_attention(ctx, ticket_id=admitted.ticket_id,
                                     need_type="fact", question="late") is None
        assert engine.list_attentions(ctx) == []
        assert engine.get_ticket(ctx, admitted.ticket_id)["state"] == "cancelled"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_attention_creation_waits_for_the_stop_and_then_gives_up() -> None:
    """Deterministic proof of the lock: a stop in flight blocks the projection.

    Both sides use their own database session, as two processes would.  While the
    stopping session holds the ticket row lock, opening an attention must block;
    once the stop commits, the projection must return "no attention" rather than
    leaving an open item on a cancelled task.
    """
    import time

    control_store = ControlStore(DATABASE_URL)
    control_store.migrate()
    tenant = f"race-{uuid.uuid4().hex}"
    control_store.seed_tenant(tenant, "Race", 1, (("planner-a", "Planner", "planner"),))
    planner = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    stopper = ControlStore(DATABASE_URL)
    projecter = ControlStore(DATABASE_URL)
    try:
        admitted = ControlEngine(control_store).admit_trigger(
            planner, source_id="t", external_event_id="race-1", payload_hash="a" * 64,
            object_ref="PO-1", goal="race")
        engine_stop = ControlEngine(stopper)
        engine_project = ControlEngine(projecter)
        outcome: dict = {}

        with stopper.connection.transaction():
            cursor = stopper.connection.cursor()
            # This is the lock a stop takes first.
            cursor.execute(
                "SELECT state FROM control_tickets WHERE id=%s AND tenant_id=%s "
                "FOR UPDATE", (admitted.ticket_id, tenant))
            thread = threading.Thread(target=lambda: outcome.setdefault(
                "attention", engine_project.open_attention(
                    planner, ticket_id=admitted.ticket_id, need_type="fact",
                    question="late fact")))
            thread.start()
            time.sleep(0.3)
            assert thread.is_alive(), (
                "opening an attention must wait for the ticket row lock")
            cursor.execute(
                "UPDATE control_tickets SET state='cancelled', updated_at=now() "
                "WHERE id=%s", (admitted.ticket_id,))
            cursor.execute(
                "UPDATE control_attentions SET state='dismissed', updated_at=now() "
                "WHERE tenant_id=%s AND ticket_id=%s AND state='open'",
                (tenant, admitted.ticket_id))
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert outcome["attention"] is None
        assert engine_stop.list_attentions(planner) == []
        assert engine_stop.get_ticket(planner, admitted.ticket_id)["state"] == "cancelled"
    finally:
        with control_store.connection.transaction():
            control_store.connection.execute(
                "DELETE FROM control_tenants WHERE id=%s", (tenant,))
        for store in (control_store, stopper, projecter):
            store.close()
