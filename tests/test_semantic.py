from __future__ import annotations

import httpx
import pytest

from forgeflow.semantic import (
    EmbeddingError,
    QdrantError,
    SemanticConfig,
    SemanticConfigError,
    check_collection,
    embed_texts,
    rerank,
)


def _config(**changes) -> SemanticConfig:
    values = dict(
        qdrant_url="https://qdrant.example",
        qdrant_api_key="qdrant-secret",
        embedding_url="https://embed.example/v1/embeddings",
        embedding_model="text-embedding-v4",
        embedding_api_key="dash-secret",
        reranker_url="https://rerank.example/text-rerank",
        reranker_model="qwen3-rerank",
        reranker_api_key="dash-secret",
    )
    values.update(changes)
    return SemanticConfig(**values)


def test_dimension_must_be_the_collection_size() -> None:
    with pytest.raises(SemanticConfigError, match="4096"):
        _config(vector_size=2048)


def test_secrets_do_not_appear_in_repr() -> None:
    text = repr(_config())
    assert "qdrant-secret" not in text and "dash-secret" not in text


def test_collection_schema_is_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {
            "config": {"params": {"vectors": {"": {
                "size": 4096, "distance": "Cosine", "on_disk": True,
            }}}},
            "payload_schema": {"tenant_id": {}},
            "points_count": 0,
        }})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        info = check_collection(_config(), client=client)
    assert info["vector_size"] == 4096
    assert info["tenant_field"] == "tenant_id"


def test_wrong_dimension_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.0] * 2048}]})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(EmbeddingError, match="embedding_dimension_mismatch"):
            embed_texts(_config(), ["probe"], client=client)


def test_reranker_native_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"results": [
            {"index": 0, "relevance_score": 0.9, "document": {"text": "relevant"}},
            {"index": 1, "relevance_score": 0.1, "document": {"text": "irrelevant"}},
        ]}})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        rows = rerank(_config(), "query", ["relevant", "irrelevant"], top_n=2, client=client)
    assert rows[0]["score"] == 0.9


def test_qdrant_unauthorized_is_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(QdrantError, match="qdrant_unauthorized"):
            check_collection(_config(), client=client)


def test_qdrant_point_ids_are_tenant_and_content_bound() -> None:
    from forgeflow.semantic.index import Chunk

    chunk = Chunk("docs/same.md", "Same", 1, 2, "identical body")
    assert chunk.point_id("tenant-a") != chunk.point_id("tenant-b")
    assert chunk.point_id("tenant-a") == chunk.point_id("tenant-a")
    changed = Chunk("docs/same.md", "Same", 1, 2, "changed body")
    assert chunk.point_id("tenant-a") != changed.point_id("tenant-a")


def test_upsert_writes_tenant_bound_ids() -> None:
    from forgeflow.semantic.index import Chunk, upsert_chunks

    chunk = Chunk("docs/same.md", "Same", 1, 1, "same content across tenants")
    written = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collections/forgeflow_knowledge_v1"):
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/index"):
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4096}]})
        if request.url.path.endswith("/points"):
            import json
            written.append(json.loads(request.content)["points"][0]["id"])
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        upsert_chunks(_config(), "tenant-a", [chunk], client=client)
        upsert_chunks(_config(), "tenant-b", [chunk], client=client)
    assert len(written) == 2 and written[0] != written[1]


def test_source_reindex_deletes_old_source_before_upsert() -> None:
    from forgeflow.semantic.index import Chunk, upsert_chunks

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.method, request.content))
        if request.url.path.endswith("/collections/forgeflow_knowledge_v1"):
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/index"):
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/points/delete"):
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4096}]})
        if request.url.path.endswith("/points"):
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(404)

    chunk = Chunk("docs/same.md", "Same", 1, 1, "body")
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        upsert_chunks(_config(), "tenant-a", [chunk], replace_sources=True, client=client)
    deletes = [call for call in calls if call[0].endswith("/points/delete")]
    assert len(deletes) == 1
    assert b'"path"' in deletes[0][2] and b'"tenant-a"' in deletes[0][2]


def test_tenant_is_config_bound_not_a_request_parameter() -> None:
    config = _config()
    assert config.tenant_id == "forgeflow-project"
    with pytest.raises(SemanticConfigError, match="(?i)tenant"):
        _config(tenant_id="../other")


def test_embedding_failure_keeps_old_source_index() -> None:
    from forgeflow.semantic.index import Chunk, upsert_chunks

    deletes = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collections/forgeflow_knowledge_v1"):
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/index"):
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/points/delete"):
            deletes.append(request)
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(503, text="provider unavailable")
        return httpx.Response(404)

    chunk = Chunk("docs/same.md", "Same", 1, 1, "new body")
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(EmbeddingError):
            upsert_chunks(_config(), "tenant-a", [chunk], replace_sources=True, client=client)
    assert deletes == []


def test_semantic_visibility_and_locale_rules() -> None:
    from forgeflow.semantic.index import locale_for_path, visible_for_locale

    assert locale_for_path("docs/en/guide.md") == "en"
    assert locale_for_path("en/guide.md") == "en"
    assert visible_for_locale("docs/en/guide.md", "en") is True
    assert visible_for_locale("docs/en/guide.md", "zh") is False
    assert visible_for_locale("docs/guide.md", "zh") is True
    assert visible_for_locale("docs/guide.md", "en") is False
    assert visible_for_locale("docs/LOCAL_WORKBENCH.md", "zh") is False
    assert visible_for_locale("docs/V9_plan.md", "zh") is False


def test_semantic_search_applies_locale_filter() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4096}]})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
        if request.url.path.endswith("/points/search"):
            return httpx.Response(200, json={"result": []})
        return httpx.Response(404)

    from forgeflow.semantic.search import search
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        search(_config(), "tenant-a", "query", locale="en", client=client)
    assert b'"locale"' in bodies[-1] and b'"en"' in bodies[-1]


def test_search_filters_sources_before_recall() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4096}]})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
        if request.url.path.endswith("/points/search"):
            return httpx.Response(200, json={"result": []})
        return httpx.Response(404)

    from forgeflow.semantic.search import search
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        search(_config(), "tenant-a", "query", locale="en", generation="g1",
               source_paths={"docs/guide.md"}, client=client)
    payload = bodies[-1]
    assert b'"should"' in payload and b'"docs/guide.md"' in payload
    assert b'"generation"' in payload and b'"g1"' in payload
    assert b'"status"' in payload and b'"published"' in payload


def test_scope_tenant_mismatch_is_rejected() -> None:
    from forgeflow.knowledge.semantic_answer import KnowledgeScope, semantic_answer
    with pytest.raises(ValueError, match="tenant_mismatch"):
        semantic_answer("query", ".", scope=KnowledgeScope("other-tenant"), config=_config())


def test_generation_changes_point_identity_and_publish_filter() -> None:
    from forgeflow.semantic.index import Chunk, publish_generation

    chunk = Chunk("docs/guide.md", "Guide", 1, 1, "body")
    assert chunk.point_id("tenant-a", "g1") != chunk.point_id("tenant-a", "g2")
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"result": {}})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        publish_generation(_config(), "tenant-a", "g1", client=client)
    assert b'"status"' in bodies[0] and b'"published"' in bodies[0]
    assert b'"generation"' in bodies[0] and b'"g1"' in bodies[0]


@pytest.mark.parametrize(("status", "error"), [
    (401, "SemanticAuthentication"), (429, "SemanticRateLimited"), (503, "SemanticUnavailable"),
])
def test_search_exposes_semantic_service_failures(status: int, error: str) -> None:
    from forgeflow import semantic as semantic_pkg

    error_type = getattr(semantic_pkg, error)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4096}]})
        if request.url.path.endswith("/points/search"):
            return httpx.Response(status, json={"error": "provider"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        with pytest.raises(error_type):
            __import__("forgeflow.semantic.search", fromlist=["search"]).search(
                _config(), "tenant-a", "query", client=client)


def test_search_applies_acl_scope_before_recall() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.0] * 4096}]})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
        if request.url.path.endswith("/points/search"):
            return httpx.Response(200, json={"result": []})
        return httpx.Response(404)

    from forgeflow.semantic.search import search
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        search(_config(), "tenant-a", "query", acl_scopes={"project"}, client=client)
    assert b'"acl_scope"' in bodies[-1] and b'"project"' in bodies[-1]


def test_acl_scope_for_engineering_evidence() -> None:
    from forgeflow.semantic.index import acl_scope_for_path
    assert acl_scope_for_path("evidence/run.json") == "engineering"
    assert acl_scope_for_path("docs/TARGET_ARCHITECTURE.md") == "project"
