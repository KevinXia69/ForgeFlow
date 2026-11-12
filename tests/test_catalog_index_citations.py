"""Persisted document blocks must survive every indexing path as citations."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from forgeflow.knowledge.catalog import KnowledgeCatalog
from forgeflow.knowledge.docmind_intake import parsed_document
from forgeflow.knowledge.documents import DocumentBlock, ParsedDocument
from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.semantic import index
from forgeflow.semantic.config import VECTOR_SIZE, SemanticConfig
from forgeflow.semantic.search import search
from forgeflow.superagent import ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


@pytest.fixture
def catalog_context():
    tenant = f"citation-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    documents = DocumentCatalog(store)
    store.seed_tenant(tenant, "Citation indexing", 1)
    try:
        yield tenant, store, documents, KnowledgeCatalog(store)
    finally:
        store.connection.rollback()
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        documents.close()


@pytest.fixture
def semantic_transport(monkeypatch):
    # Only external services are simulated. Ingestion, versioning, block reads,
    # chunking, HTTP payload serialization and retrieval all use production code.
    points = {}

    def matches(point, query):
        for condition in query.get("must", []):
            if point["payload"].get(condition["key"]) != condition["match"]["value"]:
                return False
        return not any(point["id"] in condition.get("has_id", [])
                       for condition in query.get("must_not", []))

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/embeddings":
            result = {"data": [{"embedding": [0.01] * VECTOR_SIZE} for _ in body["input"]]}
        elif path == "/rerank":
            result = {"results": [{"index": i, "relevance_score": 0.9}
                                  for i in range(min(body["top_n"], len(body["documents"])))]}
        elif path.endswith("/points") and request.method == "PUT":
            points.update({point["id"]: point for point in body["points"]})
            result = {"result": {"status": "completed"}}
        elif path.endswith("/points/payload"):
            for point in points.values():
                if matches(point, body["filter"]):
                    point["payload"].update(body["payload"])
            result = {"result": {"status": "completed"}}
        elif path.endswith("/points/delete"):
            removed = [key for key, point in points.items() if matches(point, body["filter"])]
            for key in removed:
                del points[key]
            result = {"result": {"status": "completed"}}
        elif path.endswith("/points/search"):
            result = {"result": [point for point in points.values() if matches(point, body["filter"])]}
        else:
            assert path.startswith("/collections/")
            result = {"result": {}}
        return httpx.Response(200, json=result)

    original_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handler)))
    config = SemanticConfig(
        qdrant_url="https://qdrant.test", qdrant_api_key="test",
        embedding_url="https://models.test/embeddings", embedding_model="test",
        embedding_api_key="test", reranker_url="https://models.test/rerank",
        reranker_model="test", reranker_api_key="test")
    return config, points


@pytest.mark.parametrize("filename,first,second", [
    ("rules.md", "# Lead time\n\nInitial lead time is fourteen days.\n",
     "# Lead time\n\nUpdated lead time is twenty days.\n"),
    ("rules.csv", "material,lead_days\nA,14\n", "material,lead_days\nA,20\n"),
])
def test_catalog_citations_survive_bootstrap_incremental_and_staging(
        catalog_context, semantic_transport, tmp_path, filename, first, second):
    tenant, store, documents, knowledge = catalog_context
    config, points = semantic_transport
    source_path = f"reference/{filename}"
    path = tmp_path / filename
    path.write_text(first, encoding="utf-8")
    original = documents.ingest_file(tenant_id=tenant, source_path=source_path,
                                     path=path, acl_scope="engineering")
    first_blocks = documents.read_blocks(tenant_id=tenant, source_path=source_path, version=1)
    assert first_blocks

    assert index.ensure_document_indexed(config, store, tenant, source_path, knowledge, documents) > 0
    generation = knowledge.published(tenant)
    listed = documents.list_documents(tenant)[0]
    assert listed["indexed_generation"] == generation
    assert listed["published_generation"] == generation
    assert listed["lifecycle"]["indexed"] and listed["lifecycle"]["searchable"]
    bootstrap = search(config, tenant, "lead time", generation=generation)
    assert len(bootstrap) == 1
    passage = bootstrap[0]
    assert passage["document_id"] == original.id
    assert passage["document_version"] == 1
    assert passage["locator"] == first_blocks[0]["locator"]
    assert passage["acl_scope"] == "engineering"
    assert passage["path"] == f"document://{source_path}"

    # Refreshing unchanged content and explicitly staging it must preserve the
    # same chunks and metadata as the first-generation publication.
    assert index.index_single_document(config, store, tenant, source_path, knowledge, documents) == 1
    assert search(config, tenant, "lead time", generation=generation) == bootstrap
    staging = knowledge.new_generation(tenant)
    with httpx.Client() as client:
        assert index.index_document_generation(config, store, tenant, source_path, staging, client) == 1
    staged = [point["payload"] for point in points.values()
              if point["payload"]["generation"] == staging]
    assert len(staged) == 1 and staged[0]["status"] == "staging"
    for key in ("path", "text", "heading", "locator", "block_type", "document_id",
                "document_version", "start_line", "end_line", "acl_scope"):
        assert staged[0][key] == passage[key]

    # A subsequent upload must expose its version while the saved answer can
    # still resolve its original catalog blocks, rather than silently drifting.
    path.write_text(second, encoding="utf-8")
    revised = documents.ingest_file(tenant_id=tenant, source_path=source_path,
                                    path=path, acl_scope="engineering")
    assert revised.version == 2
    assert index.ensure_document_indexed(config, store, tenant, source_path, knowledge, documents) == 1
    current = search(config, tenant, "lead time", generation=generation)
    assert len(current) == 1
    assert current[0]["document_version"] == 2
    assert current[0]["document_id"] == original.id
    assert current[0]["text"] != passage["text"]
    current_blocks = documents.read_blocks(tenant_id=tenant, source_path=source_path, version=2)
    assert current[0]["locator"] == current_blocks[0]["locator"]
    historical = documents.read_blocks(tenant_id=tenant, source_path=source_path,
                                       version=passage["document_version"])
    assert historical == first_blocks


def test_legacy_catalog_fallback_keeps_document_identity(catalog_context, semantic_transport):
    tenant, store, documents, knowledge = catalog_context
    config, points = semantic_transport
    document_id = str(uuid.uuid4())
    with store.connection.transaction():
        store.connection.execute(
            "INSERT INTO knowledge_documents(id, tenant_id, source_path, filename, "
            "kind, acl_scope, sha256, version, content, size_bytes, status, parse_status) "
            "VALUES (%s, %s, 'legacy.md', 'legacy.md', 'md', 'project', %s, 3, "
            "'Legacy lead time is fourteen days.', 34, 'ready', 'complete')",
            (document_id, tenant, uuid.uuid4().hex))
    index.ensure_document_indexed(config, store, tenant, "legacy.md", knowledge, documents)
    generation = knowledge.published(tenant)
    expected = search(config, tenant, "lead time", generation=generation)
    assert len(expected) == 1
    assert expected[0]["document_id"] == document_id
    assert expected[0]["document_version"] == 3
    assert expected[0]["locator"] is None
    index.index_single_document(config, store, tenant, "legacy.md", knowledge, documents)
    assert search(config, tenant, "lead time", generation=generation) == expected
    with httpx.Client() as client:
        staging = knowledge.new_generation(tenant)
        index.index_document_generation(config, store, tenant, "legacy.md", staging, client)
    staged = [point["payload"] for point in points.values()
              if point["payload"]["generation"] == staging]
    assert staged[0]["document_id"] == document_id
    assert staged[0]["document_version"] == 3


@pytest.mark.parametrize("leading_type", ["title", "text", None],
                         ids=["heading-table", "paragraph-table", "table-only"])
@pytest.mark.parametrize("table_format", ["markdown", "html"])
def test_docmind_tables_survive_bootstrap_incremental_and_staging(
        catalog_context, semantic_transport, leading_type, table_format):
    tenant, store, documents, knowledge = catalog_context
    config, points = semantic_transport
    source_path = "supplier-quote.pdf"
    job = {"document_id": uuid.uuid4().hex, "document_version": 1,
           "source_sha256": "a" * 64}
    original_blocks = None
    generation = None
    for version, price in ((1, "3179"), (2, "4261")):
        table = (f"| Part | Unit price |\n| --- | --- |\n| DC24 | CNY {price} |"
                 if table_format == "markdown" else
                 "<table><tr><th>Part</th><th>Unit price</th></tr>"
                 f"<tr><td>DC24</td><td>CNY {price}</td></tr></table>")
        layouts = ([{"type": leading_type, "text": "Supplier quote", "pageNum": 0}]
                   if leading_type else [])
        layouts.append({"type": "table", "markdownContent": table, "pageNum": 1})
        parsed = parsed_document(
            {"layouts": layouts}, job={**job, "document_version": version},
            filename=source_path, extension="pdf")
        record = documents.upsert(
            tenant_id=tenant, source_path=source_path, parsed=parsed,
            raw=f"synthetic-quote-v{version}".encode(), acl_scope="engineering")
        assert record.version == version
        blocks = documents.read_blocks(
            tenant_id=tenant, source_path=source_path, version=version)
        table_block = next(block for block in blocks if block["block_type"] == "table")
        assert table_block["text"] == table
        if version == 1:
            original_blocks = blocks

        assert index.ensure_document_indexed(
            config, store, tenant, source_path, knowledge, documents) > 0
        if version == 1:
            generation = knowledge.published(tenant)
        assert knowledge.published(tenant) == generation
        passages = search(config, tenant, "DC24 unit price", generation=generation)
        table_passage, = [passage for passage in passages if table in passage["text"]]
        assert table_passage["locator"] == table_block["locator"]
        assert table_passage["block_type"] == "table"
        assert table_passage["document_id"] == record.id
        assert table_passage["document_version"] == version
        assert table_passage["acl_scope"] == "engineering"
        assert table_passage["path"] == f"document://{source_path}"
        assert "\n".join(p["text"] for p in passages).count(table) == 1
        if leading_type:
            assert any("Supplier quote" in p["text"] for p in passages)
        if version == 2:
            assert all("3179" not in p["text"] for p in passages)
        assert documents.document_state(tenant_id=tenant, source_path=source_path)["searchable"]

        staging = knowledge.new_generation(tenant)
        with httpx.Client() as client:
            assert index.index_document_generation(
                config, store, tenant, source_path, staging, client) == len(passages)
        staged = [point["payload"] for point in points.values()
                  if point["payload"]["generation"] == staging]
        staged_table, = [payload for payload in staged if table in payload["text"]]
        assert staged_table["status"] == "staging"
        for key in ("path", "text", "heading", "locator", "block_type", "document_id",
                    "document_version", "start_line", "end_line", "acl_scope"):
            assert staged_table[key] == table_passage[key]
        assert search(config, tenant, "DC24 unit price", generation=generation) == passages

    assert documents.read_blocks(
        tenant_id=tenant, source_path=source_path, version=1) == original_blocks


@pytest.mark.parametrize("relationship", ["parent", "table_index"])
@pytest.mark.parametrize("child_type", ["row", "cell"])
def test_expanded_tables_do_not_duplicate_or_hide_another_documents_table(
        catalog_context, relationship, child_type):
    tenant, store, documents, _ = catalog_context
    expanded = ParsedDocument("expanded.docx", "docx", "Expanded table data", (
        DocumentBlock(0, "heading", "Expanded document", "docx:p0"),
        DocumentBlock(1, "table", "DUPLICATED_TABLE_COPY", "docx:t0", table_index=0),
        DocumentBlock(2, child_type, "Expanded table data", "docx:t0:r0",
                      parent_ord=1 if relationship == "parent" else None,
                      table_index=0 if relationship == "table_index" else None),
    ))
    independent = ParsedDocument("independent.docx", "docx", "Independent table data", (
        DocumentBlock(0, "heading", "Independent document", "docx:p0"),
        DocumentBlock(1, "table", "Independent table data", "docx:t0", table_index=0),
    ))
    for parsed in (expanded, independent):
        documents.upsert(tenant_id=tenant, source_path=parsed.filename,
                         parsed=parsed, raw=parsed.text.encode())
    chunks = index.catalog_block_chunks(store, tenant)
    text = "\n".join(chunk.text for chunk in chunks)
    assert "DUPLICATED_TABLE_COPY" not in text
    assert text.count("Expanded table data") == 1
    assert text.count("Independent table data") == 1
    independent_table, = [chunk for chunk in chunks if chunk.block_type == "table"]
    assert independent_table.path == "document://independent.docx"
    assert independent_table.locator == "docx:t0"


def test_empty_table_blocks_do_not_create_retrieval_chunks(catalog_context):
    tenant, store, documents, _ = catalog_context
    parsed = ParsedDocument("empty-table.pdf", "pdf", "Only a heading", (
        DocumentBlock(0, "heading", "Only a heading", "page:1#0"),
        DocumentBlock(1, "table", " \n\t ", "page:1#1"),
    ))
    documents.upsert(tenant_id=tenant, source_path=parsed.filename,
                     parsed=parsed, raw=b"synthetic-empty-table")
    chunk, = index.catalog_block_chunks(store, tenant)
    assert chunk.text == "Only a heading"
    assert chunk.block_type == "heading"


@pytest.mark.parametrize("relationship", ["parent", "table_index"])
def test_empty_children_do_not_count_as_indexed_table_content(catalog_context, relationship):
    tenant, store, documents, _ = catalog_context
    parsed = ParsedDocument("quote.docx", "docx", "DC24 costs CNY 3179", (
        DocumentBlock(0, "heading", "Supplier quote", "docx:p0"),
        DocumentBlock(1, "table", "DC24 costs CNY 3179", "docx:t0", table_index=0),
        DocumentBlock(2, "row", " \n\t ", "docx:t0:r0",
                      parent_ord=1 if relationship == "parent" else None,
                      table_index=0 if relationship == "table_index" else None),
    ))
    documents.upsert(tenant_id=tenant, source_path=parsed.filename,
                     parsed=parsed, raw=b"synthetic-empty-children")
    heading, table = index.catalog_block_chunks(store, tenant)
    assert heading.block_type == "heading"
    assert table.block_type == "table"
    assert table.text == "DC24 costs CNY 3179"
    assert table.locator == "docx:t0"


def test_bootstrap_marks_only_documents_with_published_chunks(
        catalog_context, semantic_transport, tmp_path):
    tenant, store, documents, knowledge = catalog_context
    config, points = semantic_transport
    for name, content in (("first.txt", "First document."),
                          ("second.txt", "Another document."),
                          ("empty.txt", "\n \n"),
                          ("withdrawn.txt", "Withdraw this document.")):
        path = tmp_path / name
        path.write_text(content)
        documents.ingest_file(tenant_id=tenant, source_path=name, path=path)
    documents.withdraw(tenant_id=tenant, source_path="withdrawn.txt")
    assert index.index_catalog_documents(config, store, tenant, knowledge) == 2
    generation = knowledge.published(tenant)
    rows = {row["source_path"]: row for row in documents.list_documents(tenant)}
    for name in ("first.txt", "second.txt"):
        assert rows[name]["indexed_generation"] == generation
        assert rows[name]["published_generation"] == generation
        assert rows[name]["lifecycle"]["searchable"]
    for name in ("empty.txt", "withdrawn.txt"):
        assert rows[name]["indexed_generation"] is None
        assert rows[name]["published_generation"] is None
        assert not rows[name]["lifecycle"]["searchable"]
    assert {point["payload"]["path"] for point in points.values()} == {
        "document://first.txt", "document://second.txt"}
    # The incremental path must not mark an empty source ready either.
    assert index.ensure_document_indexed(config, store, tenant, "empty.txt", knowledge, documents) == 0
    assert not documents.document_state(tenant_id=tenant, source_path="empty.txt")["searchable"]


@pytest.mark.parametrize("failure_stage", ["vectors", "catalog"])
def test_failed_bootstrap_publication_does_not_mark_document_ready(
        catalog_context, semantic_transport, tmp_path, monkeypatch, failure_stage):
    tenant, store, documents, knowledge = catalog_context
    config, _ = semantic_transport
    path = tmp_path / "first.txt"
    path.write_text("New tenant document.")
    documents.ingest_file(tenant_id=tenant, source_path=path.name, path=path)

    def fail(*args, **kwargs):
        raise RuntimeError("publication_failed")

    if failure_stage == "vectors":
        monkeypatch.setattr(index, "publish_generation", fail)
    else:
        monkeypatch.setattr(knowledge, "publish", fail)
    with pytest.raises(RuntimeError, match="publication_failed"):
        index.ensure_document_indexed(config, store, tenant, path.name, knowledge, documents)
    assert knowledge.published(tenant) is None
    row = documents.list_documents(tenant)[0]
    assert row["indexed_generation"] is None and row["published_generation"] is None
    assert not row["lifecycle"]["indexed"] and not row["lifecycle"]["searchable"]


@pytest.mark.parametrize("incremental", [False, True], ids=["bootstrap", "incremental"])
@pytest.mark.parametrize("mutation", ["update", "withdraw", "replace"])
def test_publication_cannot_mark_a_newer_or_withdrawn_document_ready(
        catalog_context, semantic_transport, tmp_path, monkeypatch, incremental, mutation):
    tenant, store, documents, knowledge = catalog_context
    config, _ = semantic_transport
    path = tmp_path / "changing.txt"
    path.write_text("Initial document.")
    documents.ingest_file(tenant_id=tenant, source_path=path.name, path=path)
    if incremental:
        index.ensure_document_indexed(config, store, tenant, path.name, knowledge, documents)
        path.write_text("Revision being indexed.")
        documents.ingest_file(tenant_id=tenant, source_path=path.name, path=path)
    upsert = index.upsert_chunks

    def mutate_after_upsert(*args, **kwargs):
        written = upsert(*args, **kwargs)
        if mutation == "withdraw":
            documents.withdraw(tenant_id=tenant, source_path=path.name)
        else:
            if mutation == "replace":
                assert documents.delete(tenant_id=tenant, source_path=path.name)
            path.write_text("Content uploaded after the indexed snapshot.")
            documents.ingest_file(tenant_id=tenant, source_path=path.name, path=path)
        return written

    monkeypatch.setattr(index, "upsert_chunks", mutate_after_upsert)
    assert index.ensure_document_indexed(config, store, tenant, path.name, knowledge, documents) == 1
    assert knowledge.published(tenant) is not None
    row = documents.list_documents(tenant)[0]
    assert row["indexed_generation"] is None and row["published_generation"] is None
    assert not row["lifecycle"]["indexed"] and not row["lifecycle"]["searchable"]


def test_new_tenant_first_upload_and_index_via_real_api(
        catalog_context, semantic_transport, tmp_path, monkeypatch):
    from forgeflow.api.app import create_app
    from forgeflow.api.auth import verify_cookie
    from forgeflow.api.settings import create_settings
    from forgeflow.superagent.bindings import bind_odoo_user

    tenant, store, documents, knowledge = catalog_context
    config, points = semantic_transport
    monkeypatch.setenv("FORGEFLOW_DOCMIND_MODE", "off")
    monkeypatch.delenv("FORGEFLOW_ACCOUNTS_DB", raising=False)
    monkeypatch.setattr(SemanticConfig, "from_env", classmethod(lambda cls, source=None: config))
    invite = f"bootstrap-invite-{uuid.uuid4().hex}"
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
        "FORGEFLOW_SESSION_SECRET": "bootstrap-session-secret-0123456789abcdef",
        "FORGEFLOW_INVITE_CODES": invite,
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_DEPLOYMENT": "local",
    })
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        response = client.post("/api/auth/session", json={"invite_code": invite})
        assert response.status_code == 201
        principal = verify_cookie(settings, client.cookies.get(settings.cookie_name),
                                  audience="testserver")
        assert principal
        bind_odoo_user(store, principal_id=principal, tenant_id=tenant,
                       odoo_user_id=1, role="planner")
        assert knowledge.published(tenant) is None
        assert client.get("/api/knowledge/documents").json() == []
        response = client.post("/api/knowledge/documents", files={
            "file": ("first-upload.txt", b"Supplier lead time is fourteen days.", "text/plain")})
        assert response.status_code == 200
        assert response.json()["source_path"] == "first-upload.txt"
        before = client.get("/api/knowledge/documents").json()[0]
        assert before["lifecycle"]["saved"] and not before["lifecycle"]["searchable"]
        response = client.post("/api/knowledge/documents/first-upload.txt/index")
        assert response.status_code == 200, response.text
        assert response.json()["indexed"] == 1
        after = client.get("/api/knowledge/documents").json()[0]
        generation = knowledge.published(tenant)
        assert generation and after["indexed_generation"] == generation
        assert after["published_generation"] == generation
        assert after["lifecycle"]["indexed"] and after["lifecycle"]["searchable"]
        assert any(point["payload"]["generation"] == generation
                   and point["payload"]["status"] == "published" for point in points.values())


def test_docmind_worker_first_completion_marks_catalog_searchable(
        catalog_context, semantic_transport, monkeypatch):
    """Exercise the deployed worker's completion callback against PostgreSQL."""
    import importlib.util
    from types import SimpleNamespace

    tenant, store, documents, knowledge = catalog_context
    config, _ = semantic_transport
    spec = importlib.util.spec_from_file_location(
        "docmind_bootstrap_worker", Path(__file__).resolve().parents[1]
        / "scripts/run_docmind_worker.py")
    worker_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker_module)
    job = {"tenant_id": tenant, "document_id": uuid.uuid4().hex,
           "document_version": 1, "source_path": "first-vendor-upload.pdf",
           "source_sha256": "b" * 64, "conversion_json": {}}
    completed = []

    class OneTickStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            self.set()

    class CompletedVendorJob:
        def __init__(self, jobs, client, on_complete):
            self.complete = on_complete

        def tick(self, limit):
            completed.append(self.complete(job, {"layouts": [
                {"type": "text", "text": "Parsed supplier lead time.", "pageNum": 0},
            ]}, []))

    monkeypatch.setattr(SemanticConfig, "from_env", classmethod(lambda cls, source=None: config))
    monkeypatch.setattr(worker_module, "DocMindConfig", SimpleNamespace(from_env=lambda: object()))
    monkeypatch.setattr(worker_module, "DocumentMindClient", lambda _: object())
    monkeypatch.setattr(worker_module, "ParseWorker", CompletedVendorJob)
    monkeypatch.setattr(worker_module, "threading", SimpleNamespace(Event=OneTickStop))
    monkeypatch.setattr(worker_module, "signal", SimpleNamespace(SIGINT=2, SIGTERM=15,
                                                                signal=lambda *_: None))
    assert knowledge.published(tenant) is None
    assert worker_module.main() == 0
    assert len(completed) == 1 and completed[0]["indexed"]
    generation = knowledge.published(tenant)
    row = documents.list_documents(tenant)[0]
    assert generation and row["indexed_generation"] == generation
    assert row["published_generation"] == generation
    assert row["lifecycle"]["indexed"] and row["lifecycle"]["searchable"]
    assert row["block_count"] == 1

