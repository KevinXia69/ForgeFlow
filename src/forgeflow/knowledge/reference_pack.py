"""Install reviewed reference Markdown through the existing document catalogue.

Reference packs are operator-provided research notes. They neither create ERP
records nor confirm a company's policy, and do not claim DocumentMind parsing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .catalog import KnowledgeCatalog
from .documents import ParsedDocument, parse_document
from .documents_catalog import DocumentCatalog


class ReferencePackError(ValueError):
    """A pack or its destination failed a preflight check."""


@dataclass(frozen=True)
class ReferenceDocument:
    source_id: str
    source_path: str
    metadata: dict
    raw: bytes
    parsed: ParsedDocument


@dataclass(frozen=True)
class ReferencePack:
    pack_id: str
    release: str
    documents: tuple[ReferenceDocument, ...]


def load_reference_pack(root: str | Path) -> ReferencePack:
    """Validate the entire local pack before any catalogue or vector writes."""
    root = Path(root).resolve(strict=True)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    pack_id = str(manifest.get("pack_id", ""))
    if (manifest.get("schema_version") != 1
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", pack_id)
            or manifest.get("source_kind") != "curated_reference"
            or manifest.get("classification") != "research_note"):
        raise ReferencePackError("reference_manifest_invalid")
    release = str(manifest.get("release", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release):
        raise ReferencePackError("reference_release_invalid")
    entries = manifest.get("documents", [])
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ReferencePackError("reference_documents_invalid")
    documents = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReferencePackError("reference_document_invalid")
        source_id = str(entry.get("source_id", ""))
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", source_id) or source_id in seen:
            raise ReferencePackError("reference_source_id_invalid")
        seen.add(source_id)
        filename = str(entry.get("filename", ""))
        if filename != f"{source_id}.md":
            raise ReferencePackError("reference_filename_invalid")
        path = root / filename
        if path.is_symlink() or path.resolve(strict=True).parent != root:
            raise ReferencePackError("reference_path_invalid")
        required = ("title", "publisher", "original_url", "source_version",
                    "acquired_date", "locator", "usage", "input_summary_sha256")
        if any(not isinstance(entry.get(key), str) or not entry[key].strip()
               for key in required):
            raise ReferencePackError("reference_metadata_missing")
        url = urlsplit(entry["original_url"])
        if url.scheme != "https" or not url.netloc or url.username or url.password:
            raise ReferencePackError("reference_url_invalid")
        if entry.get("material_class") != "industry_reference":
            raise ReferencePackError("reference_material_class_invalid")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ReferencePackError(f"reference_hash_mismatch:{source_id}")
        parsed = parse_document(path, classification="research_note",
                                source_kind="curated_reference")
        # Keep the reference, locator and applicability in the same retrieval
        # chunk as the facts; a short answer can therefore expose the source URL.
        from ..semantic.index import MAX_CHUNK_CHARS, MAX_CHUNK_LINES

        if (not parsed.complete or len(parsed.text) > MAX_CHUNK_CHARS
                or len(parsed.text.splitlines()) > MAX_CHUNK_LINES
                or any(entry[key] not in parsed.text
                       for key in ("original_url", "source_version", "locator"))):
            raise ReferencePackError(f"reference_citation_context_invalid:{source_id}")
        source_path = f"reference/{pack_id}/{filename}"
        provenance = {"pack_id": pack_id, "release": release, **entry}
        parsed = replace(
            parsed, filename=f"{source_id} · {entry['title']}",
            detail={**parsed.detail, "parser": "local_markdown",
                    "reference_pack": provenance})
        documents.append(ReferenceDocument(source_id, source_path, provenance, raw, parsed))
    return ReferencePack(pack_id, release, tuple(documents))


def _check_destination(pack: ReferencePack, existing: dict[str, dict]) -> None:
    for document in pack.documents:
        row = existing.get(document.source_path)
        if row is None:
            continue
        provenance = (row.get("parse_detail") or {}).get("reference_pack", {})
        if (row.get("source_kind") != "curated_reference"
                or provenance.get("pack_id") != pack.pack_id
                or provenance.get("source_id") != document.source_id
                or provenance.get("original_url") != document.metadata["original_url"]):
            raise ReferencePackError(f"reference_path_conflict:{document.source_id}")
        # A reinstall must never silently undo an operator's withdrawal/review.
        if (row.get("status") != "ready" or row.get("withdrawn_at") is not None
                or row.get("review_state") in {"required", "rejected"}):
            raise ReferencePackError(f"reference_document_inactive:{document.source_id}")


def install_reference_pack(pack: ReferencePack, *, tenant_id: str, store, config,
                           documents=None, knowledge=None, indexer=None) -> dict:
    """Upsert owned sources and retry only missing/current-version indexes.

    Existing generations and unrelated documents are retained. The caller owns
    the store connection. Failures propagate, leaving saved records available
    for a subsequent idempotent retry; no successful receipt is fabricated.
    """
    from ..semantic.index import ensure_document_indexed

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", tenant_id):
        raise ReferencePackError("reference_tenant_invalid")
    row = store.connection.execute(
        "SELECT id FROM control_tenants WHERE id=%s", (tenant_id,)).fetchone()
    store.connection.commit()
    if row is None:
        raise ReferencePackError("reference_tenant_missing")
    documents = documents if documents is not None else DocumentCatalog(store)
    knowledge = knowledge if knowledge is not None else KnowledgeCatalog(store)
    indexer = indexer if indexer is not None else ensure_document_indexed
    lock_key = f"forgeflow-reference:{tenant_id}:{pack.pack_id}"
    # Session lock survives the small catalogue transactions. External embedding
    # calls occur with no open database transaction.
    store.connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_key,))
    store.connection.commit()
    try:
        paths = [document.source_path for document in pack.documents]
        rows = store.connection.execute(
            "SELECT source_path, source_kind, parse_detail, status, withdrawn_at, "
            "review_state, indexed_generation, published_generation "
            "FROM knowledge_documents WHERE tenant_id=%s AND source_path=ANY(%s)",
            (tenant_id, paths)).fetchall()
        store.connection.commit()
        existing = {str(row["source_path"]): dict(row) for row in rows}
        _check_destination(pack, existing)
        generation = knowledge.published(tenant_id)
        pending = []
        results = []
        for document in pack.documents:
            record = documents.upsert(
                tenant_id=tenant_id, source_path=document.source_path,
                parsed=document.parsed, raw=document.raw, acl_scope="project",
                classification="research_note")
            old = existing.get(document.source_path, {})
            needs_index = (record.changed or generation is None
                           or old.get("indexed_generation") != generation
                           or old.get("published_generation") != generation)
            if needs_index:
                pending.append(document)
            results.append({"source_id": document.source_id,
                            "source_path": document.source_path,
                            "version": record.version, "changed": record.changed,
                            "index_refreshed": needs_index})
        written = 0
        if pending and generation is None:
            # All pack records have been persisted, so bootstrap indexes them
            # together with other ready documents. The indexer owns lifecycle
            # updates for the exact document revisions it actually published.
            written += indexer(config, store, tenant_id, pending[0].source_path,
                               knowledge, documents)
            generation = knowledge.published(tenant_id)
            if generation is None:
                raise ReferencePackError("reference_generation_missing")
        else:
            for document in pending:
                written += indexer(config, store, tenant_id, document.source_path,
                                   knowledge, documents)
        if knowledge.published(tenant_id) != generation:
            raise ReferencePackError("reference_publication_changed_retry")
        return {"ok": True, "pack_id": pack.pack_id, "release": pack.release,
                "tenant": tenant_id, "documents": results, "written_chunks": written,
                "generation": generation, "parser": "local_markdown",
                "source_kind": "curated_reference"}
    finally:
        store.connection.rollback()
        store.connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock_key,))
        store.connection.commit()
