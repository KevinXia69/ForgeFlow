from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from forgeflow.knowledge.semantic_answer import semantic_answer
from forgeflow.semantic import SemanticConfig

answer_module = importlib.import_module("forgeflow.knowledge.answer")


def _config(tenant_id: str = "forgeflow-project") -> SemanticConfig:
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


def test_semantic_answer_resolves_indexed_docs_path(tmp_path: Path, monkeypatch) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("target architecture", encoding="utf-8")
    monkeypatch.setattr(
        "forgeflow.knowledge.semantic_answer.search",
        lambda *args, **kwargs: [{
            "path": "guide.md", "start_line": 1, "end_line": 1,
            "text": "target architecture", "score": 0.9,
        }],
    )
    # The server resolves the published generation and passes it in the scope;
    # retrieval never falls back to "all generations".
    from forgeflow.knowledge.semantic_answer import KnowledgeScope

    result = semantic_answer(
        "architecture", tmp_path, limit=1, config=_config(),
        scope=KnowledgeScope(_config().tenant_id, generation="gen-test",
                             acl_scopes=frozenset({"project"})))
    assert result is not None and result["found"] is True
    assert result["citations"] == ["docs/guide.md:1-1"]


def test_answer_returns_semantic_result_without_legacy_index(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "notes.md").write_text("semantic answer", encoding="utf-8")
    monkeypatch.setattr(
        answer_module, "semantic_answer",
        lambda *args, **kwargs: {"found": True, "passages": [], "citations": []},
    )
    assert answer_module.answer("semantic", tmp_path)["found"] is True


def test_configured_semantic_failure_is_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "notes.md").write_text("semantic answer", encoding="utf-8")

    def fail(*args, **kwargs):
        raise RuntimeError("semantic_unavailable")

    monkeypatch.setattr(answer_module, "semantic_answer", fail)
    with pytest.raises(RuntimeError, match="semantic_unavailable"):
        answer_module.answer("semantic", tmp_path)


def test_missing_semantic_config_is_explicit_not_legacy(tmp_path: Path, monkeypatch) -> None:
    from forgeflow.knowledge.answer import KnowledgeUnavailable
    (tmp_path / "notes.md").write_text("legacy content", encoding="utf-8")
    # The condition under test is "no semantic service configured"; clear any
    # ambient configuration instead of hoping the test host has none.
    for key in ("QDRANT_URL", "QDRANT_API_KEY", "SEMANTIC_API_KEY",
                "EMBEDDING_API_URL", "EMBEDDING_MODEL_ID",
                "RERANKER_API_URL", "RERANKER_MODEL_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(answer_module, "_TEST_ADAPTER", None)
    monkeypatch.setattr(answer_module, "_TEST_MARKET_ADAPTER", None)
    with pytest.raises(KnowledgeUnavailable, match="semantic_configuration_required"):
        answer_module.answer("legacy", tmp_path)


def test_server_scope_is_derived_from_environment(monkeypatch) -> None:
    from forgeflow.knowledge.scope import server_scope
    values = {
        "QDRANT_URL": "https://qdrant.example", "QDRANT_API_KEY": "q",
        "SEMANTIC_API_KEY": "s", "EMBEDDING_API_URL": "https://embed.example/v1",
        "EMBEDDING_MODEL_ID": "embed", "RERANKER_API_URL": "https://rerank.example/v1",
        "RERANKER_MODEL_ID": "rerank", "FORGEFLOW_KNOWLEDGE_TENANT": "tenant-a",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FORGEFLOW_CONTROL_DATABASE_URL", raising=False)
    scope = server_scope("en")
    assert scope is not None and scope.tenant_id == "tenant-a" and scope.locale == "en"


def test_document_uri_is_kept_as_an_authorized_passage(tmp_path: Path, monkeypatch) -> None:
    """An enterprise document is cited only while the catalogue says it is readable.

    The passage is served from vectors indexed under the old scope, so the answer
    path re-checks the current catalogue entry before returning it.
    """
    import os
    import uuid

    from forgeflow.knowledge.documents_catalog import DocumentCatalog
    from forgeflow.knowledge.semantic_answer import KnowledgeScope
    from forgeflow.superagent import ControlStore

    control = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
    if not control:
        pytest.skip("FORGEFLOW_CONTROL_DATABASE_URL not set")
    store = ControlStore(control)
    store.migrate()
    tenant = f"doc-uri-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, "Doc URI", 1, (("planner-a", "Planner", "planner"),))
    path = tmp_path / "supplier.md"
    path.write_text("supplier lead time is fourteen days", encoding="utf-8")
    try:
        DocumentCatalog(store).ingest_file(tenant_id=tenant, source_path="supplier.md",
                                           path=path, acl_scope="project")
        monkeypatch.setattr(
            "forgeflow.knowledge.semantic_answer.search",
            lambda *args, **kwargs: [{
                "path": "document://supplier.md", "start_line": 1, "end_line": 1,
                "text": "supplier lead time", "score": 0.9,
            }])

        result = semantic_answer(
            "lead time", tmp_path, limit=1, config=_config(tenant), store=store,
            scope=KnowledgeScope(tenant, generation="gen-test",
                                 acl_scopes=frozenset({"project"})))
        assert result is not None and result["found"] is True
        assert result["citations"] == ["document://supplier.md:1-1"]
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_scope_generation_is_passed_to_search(tmp_path: Path, monkeypatch) -> None:
    from forgeflow.knowledge.semantic_answer import KnowledgeScope
    captured = {}
    def fake_search(*args, **kwargs):
        captured.update(kwargs)
        return []
    monkeypatch.setattr("forgeflow.knowledge.semantic_answer.search", fake_search)
    semantic_answer("q", tmp_path, limit=1, config=_config(),
                    scope=KnowledgeScope(_config().tenant_id, "zh", generation="gen-1"))
    assert captured["generation"] == "gen-1"


def test_answer_locale_does_not_filter_sources_by_default(tmp_path: Path, monkeypatch) -> None:
    from forgeflow.knowledge.semantic_answer import KnowledgeScope

    calls = []
    def fake_search(*args, **kwargs):
        calls.append(kwargs.get("locale"))
        return [{"path": "doc.md", "start_line": 1, "end_line": 1,
                 "text": "approval code", "score": 0.9}]
    monkeypatch.setattr("forgeflow.knowledge.semantic_answer.search", fake_search)
    (tmp_path / "doc.md").write_text("approval code", encoding="utf-8")
    semantic_answer("approval", tmp_path, limit=1, locale="en", config=_config(),
                    scope=KnowledgeScope(_config().tenant_id, generation="g",
                                         acl_scopes=frozenset({"project"})))
    assert calls == [None]
    semantic_answer("approval", tmp_path, limit=1, locale="en", config=_config(),
                    scope=KnowledgeScope(_config().tenant_id, generation="g",
                                         acl_scopes=frozenset({"project"}),
                                         source_locale="en"))
    assert calls[-1] == "en"
