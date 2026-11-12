"""PostgreSQL publication catalog integration; skipped without an explicit DB URL."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.knowledge.catalog import KnowledgeCatalog
from forgeflow.superagent.store import ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_generation_is_atomic_and_only_one_is_published() -> None:
    tenant = f"catalog-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    try:
        store.migrate()
        store.seed_tenant(tenant, "Catalog test", 1)
        catalog = KnowledgeCatalog(store)
        first = catalog.new_generation(tenant)
        catalog.publish(tenant, first)
        assert catalog.published(tenant) == first
        second = catalog.new_generation(tenant)
        catalog.publish(tenant, second)
        assert catalog.published(tenant) == second
        rows = store.connection.execute(
            "SELECT generation, state FROM knowledge_generations WHERE tenant_id=%s "
            "ORDER BY created_at", (tenant,)).fetchall()
        assert {str(row["generation"]): row["state"] for row in rows} == {
            first: "retired", second: "published"}
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()
