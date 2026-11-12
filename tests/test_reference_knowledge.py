from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.knowledge.reference_pack import (
    ReferencePackError,
    install_reference_pack,
    load_reference_pack,
)
from forgeflow.semantic.index import chunks_from_text

ROOT = Path(__file__).resolve().parents[1] / "knowledge/reference/supply-recovery-v1"


def test_reference_facts_and_citations_share_one_search_chunk():
    pack = load_reference_pack(ROOT)
    assert len(pack.documents) == 12
    for document in pack.documents:
        chunks = chunks_from_text(document.parsed.text, path=document.source_path)
        assert len(chunks) == 1
        assert document.metadata["original_url"] in chunks[0].text
        assert document.metadata["locator"] in chunks[0].text
        assert document.metadata["source_version"] in chunks[0].text
        assert document.parsed.classification == "research_note"
        assert document.parsed.source_kind == "curated_reference"
        assert document.parsed.review_state == "not_required"
        assert document.parsed.detail["parser"] == "local_markdown"
    assert "Odoo 19.0" in pack.documents[-1].parsed.text
    assert "Odoo 20" in pack.documents[-1].parsed.text


def test_tampered_or_external_file_fails_before_install(tmp_path):
    root = tmp_path / "pack"
    shutil.copytree(ROOT, root)
    path = root / "K01.md"
    path.write_text("tampered")
    with pytest.raises(ReferencePackError, match="reference_hash_mismatch:K01"):
        load_reference_pack(root)
    path.unlink()
    path.symlink_to(ROOT / "K01.md")
    with pytest.raises(ReferencePackError, match="reference_path_invalid"):
        load_reference_pack(root)


def test_manifest_cannot_claim_company_policy(tmp_path):
    shutil.copytree(ROOT, tmp_path / "pack")
    manifest = tmp_path / "pack/manifest.json"
    data = json.loads(manifest.read_text())
    data["documents"][0]["material_class"] = "approved_company_policy"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ReferencePackError, match="reference_material_class_invalid"):
        load_reference_pack(tmp_path / "pack")


class Connection:
    def __init__(self):
        self.rows = {}
        self.tenant_exists = True
        self.unlocks = 0

    def execute(self, query, params):
        if "FROM control_tenants" in query:
            value = {"id": params[0]} if self.tenant_exists else None
            return SimpleNamespace(fetchone=lambda: value)
        if "FROM knowledge_documents" in query:
            return SimpleNamespace(fetchall=lambda: [self.rows[p] for p in params[1]
                                                     if p in self.rows])
        if "pg_advisory_unlock" in query:
            self.unlocks += 1
        return SimpleNamespace()

    def commit(self):
        pass

    def rollback(self):
        pass


class Documents:
    def __init__(self, connection):
        self.connection = connection
        self.writes = 0
        self.marks = []

    def upsert(self, **kwargs):
        path = kwargs["source_path"]
        parsed = kwargs["parsed"]
        previous = self.connection.rows.get(path, {})
        digest = hashlib.sha256(kwargs["raw"]).hexdigest()
        changed = digest != previous.get("sha256")
        version = previous.get("version", 0) + int(changed)
        if changed:
            self.writes += 1
            self.connection.rows[path] = {
                "id": previous.get("id", f"document-{path}"),
                "source_path": path, "source_kind": parsed.source_kind,
                "parse_detail": parsed.detail, "status": "ready", "withdrawn_at": None,
                "review_state": parsed.review_state, "indexed_generation": None,
                "published_generation": None, "sha256": digest, "version": version}
        return SimpleNamespace(changed=changed, version=version)

    def mark_indexed(self, *, source_path, generation, document_id=None, version=None, **kwargs):
        self.marks.append((source_path, document_id, version))
        row = self.connection.rows[source_path]
        if (row["status"] != "ready" or row["withdrawn_at"] is not None
                or (document_id is not None and row["id"] != document_id)
                or (version is not None and row["version"] != version)):
            return
        row.update(
            indexed_generation=generation, published_generation=generation)


def setup_install(generation="existing-generation"):
    connection = Connection()
    store = SimpleNamespace(connection=connection)
    documents = Documents(connection)
    state = {"generation": generation, "calls": [], "fail_path": None}
    knowledge = SimpleNamespace(published=lambda tenant: state["generation"])

    def indexer(config, store, tenant, source_path, knowledge, documents):
        state["calls"].append(source_path)
        if source_path == state["fail_path"]:
            raise RuntimeError("embedding unavailable")
        # Match ensure_document_indexed: bootstrap publishes every ready
        # document; later calls publish only the requested source snapshot.
        snapshots = {path: dict(row) for path, row in connection.rows.items()
                     if (state["generation"] is None or path == source_path)
                     and row.get("status") == "ready" and row.get("withdrawn_at") is None}
        state["generation"] = state["generation"] or "first-generation"
        if state.get("during_index") is not None:
            state["during_index"]()
        for path, row in snapshots.items():
            documents.mark_indexed(source_path=path, generation=state["generation"],
                                   document_id=row["id"], version=row["version"])
        return len(snapshots)

    kwargs = dict(tenant_id="tenant", store=store, config=object(), documents=documents,
                  knowledge=knowledge, indexer=indexer)
    return connection, documents, state, kwargs


def test_install_is_idempotent_and_preserves_other_documents_and_generation():
    pack = load_reference_pack(ROOT)
    connection, documents, state, kwargs = setup_install()
    connection.rows["company-policy.md"] = {"sha256": "unchanged"}
    first = install_reference_pack(pack, **kwargs)
    second = install_reference_pack(pack, **kwargs)
    assert documents.writes == 12
    assert len(state["calls"]) == 12
    assert first["written_chunks"] == 12
    assert second["written_chunks"] == 0
    assert all(not row["changed"] and not row["index_refreshed"]
               for row in second["documents"])
    assert first["generation"] == second["generation"] == "existing-generation"
    assert connection.rows["company-policy.md"] == {"sha256": "unchanged"}
    assert connection.unlocks == 2


@pytest.mark.parametrize("alteration,error", [
    ({"source_kind": "upload"}, "reference_path_conflict"),
    ({"parse_detail": {"reference_pack": {"pack_id": "another"}}}, "reference_path_conflict"),
    ({"status": "withdrawn", "withdrawn_at": "2026-11-10"}, "reference_document_inactive"),
    ({"review_state": "rejected"}, "reference_document_inactive"),
])
def test_preflight_never_overwrites_conflicts_or_reinstates_withdrawn(alteration, error):
    pack = load_reference_pack(ROOT)
    connection, documents, state, kwargs = setup_install()
    document = pack.documents[-1]
    connection.rows[document.source_path] = {
        "source_path": document.source_path,
        "source_kind": "curated_reference", "parse_detail": document.parsed.detail,
        "status": "ready", "withdrawn_at": None, "review_state": "not_required",
        **alteration}
    with pytest.raises(ReferencePackError, match=error):
        install_reference_pack(pack, **kwargs)
    assert documents.writes == 0 and not state["calls"]
    assert connection.unlocks == 1


def test_index_failure_can_resume_without_duplicate_versions():
    pack = load_reference_pack(ROOT)
    connection, documents, state, kwargs = setup_install()
    state["fail_path"] = pack.documents[2].source_path
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        install_reference_pack(pack, **kwargs)
    state["fail_path"] = None
    result = install_reference_pack(pack, **kwargs)
    assert result["written_chunks"] == 10
    assert documents.writes == 12
    assert all(row["version"] == 1 for row in result["documents"])
    assert all(row["indexed_generation"] == "existing-generation"
               for row in connection.rows.values())


def test_new_tenant_indexes_pack_once_and_marks_all_documents_searchable():
    pack = load_reference_pack(ROOT)
    connection, documents, state, kwargs = setup_install(None)
    result = install_reference_pack(pack, **kwargs)
    assert result["generation"] == "first-generation"
    assert result["written_chunks"] == 12
    assert len(state["calls"]) == 1
    assert all(row["published_generation"] == "first-generation"
               for row in connection.rows.values())


@pytest.mark.parametrize("mutation", ["update", "withdraw", "replace"])
def test_installer_never_overrides_indexers_revision_guard(mutation):
    pack = load_reference_pack(ROOT)
    connection, documents, state, kwargs = setup_install(None)
    changed_path = pack.documents[1].source_path

    def change_after_index_snapshot():
        row = connection.rows[changed_path]
        if mutation == "update":
            row["version"] += 1
        elif mutation == "withdraw":
            row.update(status="withdrawn", withdrawn_at="2026-11-10")
        else:
            row["id"] = "replacement-document-with-the-same-path-and-version"

    state["during_index"] = change_after_index_snapshot
    install_reference_pack(pack, **kwargs)
    assert connection.rows[changed_path]["indexed_generation"] is None
    assert connection.rows[changed_path]["published_generation"] is None
    assert all(row["published_generation"] == "first-generation"
               for path, row in connection.rows.items() if path != changed_path)
    assert len(documents.marks) == 12
    assert all(document_id and version == 1 for _, document_id, version in documents.marks)
    assert connection.unlocks == 1


def test_missing_tenant_is_never_created():
    pack = load_reference_pack(ROOT)
    connection, documents, state, kwargs = setup_install()
    connection.tenant_exists = False
    with pytest.raises(ReferencePackError, match="reference_tenant_missing"):
        install_reference_pack(pack, **kwargs)
    assert not connection.rows and documents.writes == 0 and not state["calls"]
