"""Live catalog-document embedding/Qdrant generation test."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest

from forgeflow.knowledge.catalog import KnowledgeCatalog
from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.semantic import SemanticConfig
from forgeflow.semantic.index import (
    delete_tenant_points,
    index_catalog_documents,
    index_single_document,
)
from forgeflow.superagent import ControlStore

CONTROL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(
        not (CONTROL and os.environ.get("QDRANT_URL") and os.environ.get("SEMANTIC_API_KEY")),
        reason="semantic/control credentials not set"),
    pytest.mark.usefixtures("live_semantic_env"),
]


def test_document_content_enters_a_published_generation(tmp_path: Path) -> None:
    from docx import Document
    tenant = f"doc-index-{uuid.uuid4().hex}"
    path = tmp_path / "supplier.docx"
    document = Document()
    document.add_paragraph("Supplier lead time is fourteen days.")
    document.save(path)
    store = ControlStore(CONTROL)
    catalog = DocumentCatalog(store)
    knowledge = KnowledgeCatalog(store)
    try:
        store.seed_tenant(tenant, "Doc index", 1, (("planner-a", "Planner", "planner"),))
        catalog.ingest_file(tenant_id=tenant, source_path="supplier.docx",
                            path=path, acl_scope="project")
        written = index_catalog_documents(SemanticConfig.from_env(), store, tenant, knowledge)
        assert written == 1
        generation = knowledge.published(tenant)
        assert generation
        config = SemanticConfig.from_env()
        document = Document()
        document.add_paragraph("Supplier lead time is twenty days.")
        document.save(path)
        catalog.ingest_file(tenant_id=tenant, source_path="supplier.docx",
                            path=path, acl_scope="project")
        assert index_single_document(SemanticConfig.from_env(), store, tenant,
                                     "supplier.docx", knowledge, catalog) == 1
        indexed = store.connection.execute(
            "SELECT indexed_generation FROM knowledge_documents WHERE tenant_id=%s", (tenant,)
        ).fetchone()["indexed_generation"]
        assert indexed == generation
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.post(
                f"{config.qdrant_url}/collections/{config.collection}/points/count",
                headers={"api-key": config.qdrant_api_key},
                json={"exact": True, "filter": {"must": [
                    {"key": config.tenant_field, "match": {"value": tenant}},
                    {"key": "generation", "match": {"value": generation}},
                    {"key": "path", "match": {"value": "document://supplier.docx"}},
                ]}})
        assert response.status_code == 200 and response.json()["result"]["count"] == 1
    finally:
        config = SemanticConfig.from_env()
        delete_tenant_points(config, tenant)
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        catalog.close()
        knowledge.close()
