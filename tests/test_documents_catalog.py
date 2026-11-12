from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.semantic.index import catalog_chunks
from forgeflow.superagent import ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_document_versions_and_acl_are_persisted(tmp_path: Path) -> None:
    from docx import Document
    tenant = f"documents-{uuid.uuid4().hex}"
    path = tmp_path / "supplier.docx"
    document = Document()
    document.add_paragraph("version one")
    document.save(path)
    store = ControlStore(DATABASE_URL)
    catalog = DocumentCatalog(store)
    try:
        store.seed_tenant(tenant, "Documents", 1, (("planner-a", "Planner", "planner"),))
        first = catalog.ingest_file(tenant_id=tenant, source_path="supplier.docx",
                                    path=path, acl_scope="project")
        assert first.version == 1 and first.changed
        unchanged = catalog.ingest_file(tenant_id=tenant, source_path="supplier.docx",
                                        path=path, acl_scope="project")
        assert unchanged.version == 1 and not unchanged.changed
        document = Document()
        document.add_paragraph("version two")
        document.save(path)
        second = catalog.ingest_file(tenant_id=tenant, source_path="supplier.docx",
                                     path=path, acl_scope="engineering")
        assert second.version == 2 and second.changed and second.acl_scope == "engineering"
        listed = catalog.list_documents(tenant)
        assert listed[0]["version"] == 2 and listed[0]["acl_scope"] == "engineering"
        versions = catalog.list_versions(tenant_id=tenant, source_path="supplier.docx")
        assert [row["version"] for row in versions] == [2, 1]
        restored = catalog.restore_version(tenant_id=tenant, source_path="supplier.docx", version=1)
        assert restored.version == 3 and restored.changed is True
        assert [row["version"] for row in catalog.list_versions(
            tenant_id=tenant, source_path="supplier.docx")] == [3, 2, 1]
        row = store.connection.execute(
            "SELECT content FROM knowledge_documents WHERE tenant_id=%s", (tenant,)).fetchone()
        assert "version one" in row["content"]
        chunks = catalog_chunks(store, tenant)
        assert chunks and chunks[0].path == "document://supplier.docx"
        assert chunks[0].acl_scope == "engineering" and "version one" in chunks[0].text
        assert catalog.delete(tenant_id=tenant, source_path="supplier.docx") is True
        assert catalog.list_documents(tenant) == []
        assert catalog.delete(tenant_id=tenant, source_path="supplier.docx") is False
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        catalog.close()


def test_acl_only_change_invalidates_index(tmp_path: Path) -> None:
    from docx import Document
    tenant = f"documents-acl-{uuid.uuid4().hex}"
    path = tmp_path / "acl.docx"
    document = Document()
    document.add_paragraph("same content")
    document.save(path)
    store = ControlStore(DATABASE_URL)
    catalog = DocumentCatalog(store)
    try:
        store.seed_tenant(tenant, "ACL", 1, (("p", "P", "planner"),))
        first = catalog.ingest_file(tenant_id=tenant, source_path="acl.docx",
                                    path=path, acl_scope="project")
        catalog.mark_indexed(tenant_id=tenant, source_path="acl.docx", generation="g1")
        second = catalog.ingest_file(tenant_id=tenant, source_path="acl.docx",
                                     path=path, acl_scope="engineering")
        assert first.version == 1 and second.version == 2
        row = store.connection.execute(
            "SELECT acl_scope, indexed_generation FROM knowledge_documents "
            "WHERE tenant_id=%s AND source_path='acl.docx'", (tenant,)).fetchone()
        assert row["acl_scope"] == "engineering" and row["indexed_generation"] is None
        assert [v["version"] for v in catalog.list_versions(
            tenant_id=tenant, source_path="acl.docx")] == [2, 1]
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        catalog.close()
