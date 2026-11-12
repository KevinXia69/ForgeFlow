"""Versioned document records bound to tenant, ACL scope and parse completeness."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..superagent.store import ControlStore
from .documents import (
    DocumentBlock,
    DocumentError,
    ParsedDocument,
    parse_document,
)

VALID_ACL_SCOPES = frozenset({"project", "engineering"})


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: str
    tenant_id: str
    source_path: str
    filename: str
    kind: str
    acl_scope: str
    sha256: str
    version: int
    changed: bool
    parse_status: str = "complete"
    unparsed_scope: tuple[str, ...] = ()
    block_count: int = 0
    review_state: str = "not_required"

    @property
    def complete(self) -> bool:
        return self.parse_status == "complete"

    @property
    def needs_review(self) -> bool:
        return self.review_state == "required"


class DocumentCatalog:
    def __init__(self, store: ControlStore) -> None:
        self.store = store
        self.store.migrate()

    def close(self) -> None:
        self.store.close()

    # -- ingestion -----------------------------------------------------------
    def upsert(self, *, tenant_id: str, source_path: str, parsed: ParsedDocument,
               raw: bytes, acl_scope: str = "project",
               classification: str | None = None) -> DocumentRecord:
        if acl_scope not in VALID_ACL_SCOPES:
            raise ValueError("invalid_acl_scope")
        if not source_path or source_path.startswith("/") or ".." in Path(source_path).parts:
            raise ValueError("invalid_source_path")
        digest = hashlib.sha256(raw).hexdigest()
        content_hash = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()
        resolved_classification = classification or parsed.classification
        with self.store.transaction() as cursor:
            existing = cursor.execute(
                "SELECT id, sha256, version, acl_scope, parse_status, review_state "
                "FROM knowledge_documents WHERE tenant_id=%s AND source_path=%s",
                (tenant_id, source_path)).fetchone()
            if (existing and existing["sha256"] == digest
                    and str(existing["acl_scope"]) == acl_scope):
                return DocumentRecord(
                    str(existing["id"]), tenant_id, source_path, parsed.filename,
                    parsed.kind, acl_scope, digest, int(existing["version"]), False,
                    str(existing["parse_status"]), (), 0, str(existing["review_state"]))
            version = (int(existing["version"]) + 1) if existing else 1
            document_id = str(existing["id"]) if existing else str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO knowledge_documents(id, tenant_id, source_path, filename, kind, "
                "acl_scope, sha256, version, content, size_bytes, status, parse_status, "
                "parse_detail, unparsed_scope, review_state, source_kind, classification, "
                "content_hash, withdrawn_at, published_generation) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ready', %s, %s, %s, %s, "
                "%s, %s, %s, NULL, NULL) "
                "ON CONFLICT (tenant_id, source_path) DO UPDATE SET "
                "filename=EXCLUDED.filename, kind=EXCLUDED.kind, acl_scope=EXCLUDED.acl_scope, "
                "sha256=EXCLUDED.sha256, version=EXCLUDED.version, content=EXCLUDED.content, "
                "size_bytes=EXCLUDED.size_bytes, status='ready', "
                "parse_status=EXCLUDED.parse_status, parse_detail=EXCLUDED.parse_detail, "
                "unparsed_scope=EXCLUDED.unparsed_scope, review_state=EXCLUDED.review_state, "
                "source_kind=EXCLUDED.source_kind, classification=EXCLUDED.classification, "
                "content_hash=EXCLUDED.content_hash, withdrawn_at=NULL, "
                "published_generation=NULL, indexed_generation=NULL, indexed_at=NULL, "
                "updated_at=now()",
                (document_id, tenant_id, source_path, parsed.filename, parsed.kind,
                 acl_scope, digest, version, parsed.text, len(raw),
                 parsed.parse_status, _json(parsed.detail), list(parsed.unparsed_scope),
                 parsed.review_state, parsed.source_kind, resolved_classification,
                 content_hash))
            cursor.execute(
                "INSERT INTO knowledge_document_versions(id, document_id, tenant_id, "
                "source_path, version, sha256, content, size_bytes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (document_id, version) DO NOTHING",
                (str(uuid.uuid4()), document_id, tenant_id, source_path, version,
                 digest, parsed.text, len(raw)))
            self._write_blocks(cursor, tenant_id=tenant_id, document_id=document_id,
                               version=version, blocks=parsed.blocks)
        return DocumentRecord(document_id, tenant_id, source_path, parsed.filename,
                              parsed.kind, acl_scope, digest, version, True,
                              parsed.parse_status, tuple(parsed.unparsed_scope),
                              len(parsed.blocks), parsed.review_state)

    def ingest_file(self, *, tenant_id: str, source_path: str, path: str | Path,
                    acl_scope: str = "project",
                    classification: str | None = None) -> DocumentRecord:
        target = Path(path)
        raw = target.read_bytes()
        parsed = parse_document(target, classification=classification)
        return self.upsert(tenant_id=tenant_id, source_path=source_path, parsed=parsed,
                           raw=raw, acl_scope=acl_scope, classification=classification)

    def _write_blocks(self, cursor, *, tenant_id: str, document_id: str, version: int,
                      blocks: tuple[DocumentBlock, ...]) -> None:
        if not blocks:
            return
        ord_to_id: dict[int, str] = {}
        rows = []
        for block in blocks:
            block_id = str(uuid.uuid4())
            ord_to_id[block.ord] = block_id
            rows.append((block_id, document_id, tenant_id, version, block.ord,
                         block.block_type, block.text, block.locator, block.content_hash))
        cursor.executemany(
            "INSERT INTO knowledge_document_blocks(id, document_id, tenant_id, version, "
            "ord, block_type, text, locator, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (document_id, version, ord) DO NOTHING", rows)
        # Locational detail is written separately so the bulk insert stays small.
        for block in blocks:
            cursor.execute(
                "UPDATE knowledge_document_blocks SET "
                "section_path=%s, page=%s, paragraph_index=%s, table_index=%s, "
                "row_index=%s, cell_ref=%s, sheet=%s, cell_range=%s, "
                "line_start=%s, line_end=%s, parent_id=%s "
                "WHERE document_id=%s AND version=%s AND ord=%s",
                (block.section_path, block.page, block.paragraph_index, block.table_index,
                 block.row_index, block.cell_ref, block.sheet, block.cell_range,
                 block.line_start, block.line_end,
                 ord_to_id.get(block.parent_ord) if block.parent_ord is not None else None,
                 document_id, version, block.ord))

    # -- lifecycle -----------------------------------------------------------
    def mark_indexed(self, *, tenant_id: str, source_path: str, generation: str,
                     document_id: str | None = None, version: int | None = None) -> None:
        # A publication can finish after another upload or a withdrawal. Only
        # the exact document revision represented by its vectors becomes ready.
        query = (
            "UPDATE knowledge_documents SET indexed_generation=%s, indexed_at=now(), "
            "published_generation=%s WHERE tenant_id=%s AND source_path=%s "
            "AND status='ready' AND withdrawn_at IS NULL")
        params: list = [generation, generation, tenant_id, source_path]
        if document_id is not None:
            query += " AND id=%s"
            params.append(document_id)
        if version is not None:
            query += " AND version=%s"
            params.append(version)
        with self.store.transaction() as cursor:
            cursor.execute(query, tuple(params))

    def clear_index_state(self, *, tenant_id: str, source_path: str) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_documents SET indexed_generation=NULL, indexed_at=NULL "
                "WHERE tenant_id=%s AND source_path=%s", (tenant_id, source_path))

    def withdraw(self, *, tenant_id: str, source_path: str) -> dict:
        """Remove a document from logical visibility before touching vectors.

        The vector cleanup runs afterwards; if it fails, the document is already
        unreadable for users, which is the ordering the plan requires.
        """
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "UPDATE knowledge_documents SET status='withdrawn', withdrawn_at=now(), "
                "published_generation=NULL, indexed_generation=NULL, updated_at=now() "
                "WHERE tenant_id=%s AND source_path=%s RETURNING id, version, kind",
                (tenant_id, source_path)).fetchone()
            if row is None:
                raise LookupError("document_not_found")
        return {"source_path": source_path, "vector_path": f"document://{source_path}",
                "document_id": str(row["id"]), "version": int(row["version"])}

    def reinstate(self, *, tenant_id: str, source_path: str) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_documents SET status='ready', withdrawn_at=NULL, "
                "updated_at=now() WHERE tenant_id=%s AND source_path=%s",
                (tenant_id, source_path))

    def set_review_state(self, *, tenant_id: str, source_path: str, state: str) -> None:
        if state not in {"not_required", "required", "confirmed", "rejected"}:
            raise ValueError("invalid_review_state")
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_documents SET review_state=%s, updated_at=now() "
                "WHERE tenant_id=%s AND source_path=%s", (state, tenant_id, source_path))

    def list_versions(self, *, tenant_id: str, source_path: str) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT version, sha256, size_bytes, created_at FROM knowledge_document_versions "
            "WHERE tenant_id=%s AND source_path=%s ORDER BY version DESC",
            (tenant_id, source_path)).fetchall()
        self.store.connection.commit()
        return [dict(row) for row in rows]

    def restore_version(self, *, tenant_id: str, source_path: str,
                        version: int) -> DocumentRecord:
        with self.store.transaction() as cursor:
            document = cursor.execute(
                "SELECT id, filename, kind, acl_scope, version, parse_status, "
                "review_state FROM knowledge_documents "
                "WHERE tenant_id=%s AND source_path=%s FOR UPDATE",
                (tenant_id, source_path)).fetchone()
            if document is None:
                raise LookupError("document_not_found")
            source = cursor.execute(
                "SELECT content, sha256, size_bytes FROM knowledge_document_versions "
                "WHERE document_id=%s AND version=%s", (document["id"], int(version))).fetchone()
            if source is None:
                raise LookupError("document_version_not_found")
            new_version = int(document["version"]) + 1
            cursor.execute(
                "UPDATE knowledge_documents SET content=%s, sha256=%s, size_bytes=%s, "
                "version=%s, status='ready', withdrawn_at=NULL, indexed_generation=NULL, "
                "indexed_at=NULL, published_generation=NULL, updated_at=now() WHERE id=%s",
                (source["content"], source["sha256"], source["size_bytes"], new_version,
                 document["id"]))
            cursor.execute(
                "INSERT INTO knowledge_document_versions(id, document_id, tenant_id, "
                "source_path, version, sha256, content, size_bytes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), document["id"], tenant_id, source_path, new_version,
                 source["sha256"], source["content"], source["size_bytes"]))
            # Blocks travel with the version so citations stay valid after a restore.
            cursor.execute(
                "INSERT INTO knowledge_document_blocks(id, document_id, tenant_id, version, "
                "ord, block_type, text, locator, content_hash, section_path, page, "
                "paragraph_index, table_index, row_index, cell_ref, sheet, cell_range, "
                "line_start, line_end) "
                "SELECT gen_random_uuid(), document_id, tenant_id, %s, ord, block_type, "
                "text, locator, content_hash, section_path, page, paragraph_index, "
                "table_index, row_index, cell_ref, sheet, cell_range, line_start, line_end "
                "FROM knowledge_document_blocks WHERE document_id=%s AND version=%s "
                "ON CONFLICT (document_id, version, ord) DO NOTHING",
                (new_version, document["id"], int(version)))
        return DocumentRecord(str(document["id"]), tenant_id, source_path,
                              str(document["filename"]), str(document["kind"]),
                              str(document["acl_scope"]), str(source["sha256"]),
                              new_version, True, str(document["parse_status"]), (), 0,
                              str(document["review_state"]))

    def delete(self, *, tenant_id: str, source_path: str) -> bool:
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "DELETE FROM knowledge_documents WHERE tenant_id=%s AND source_path=%s "
                "RETURNING id", (tenant_id, source_path)).fetchone()
        return row is not None

    # -- reads ---------------------------------------------------------------
    def list_documents(self, tenant_id: str) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT source_path, filename, kind, acl_scope, version, status, "
            "indexed_generation, indexed_at, updated_at, parse_status, unparsed_scope, "
            "review_state, classification, withdrawn_at, published_generation, "
            "(SELECT count(*) FROM knowledge_document_blocks b "
            " WHERE b.document_id=knowledge_documents.id "
            " AND b.version=knowledge_documents.version) AS block_count "
            "FROM knowledge_documents WHERE tenant_id=%s ORDER BY source_path",
            (tenant_id,)).fetchall()
        self.store.connection.commit()
        return [dict(row) | {"lifecycle": _lifecycle(row)} for row in rows]

    def read_blocks(self, *, tenant_id: str, source_path: str,
                    version: int | None = None) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT b.ord, b.block_type, b.text, b.locator, b.section_path, b.page, "
            "b.paragraph_index, b.table_index, b.row_index, b.cell_ref, b.sheet, "
            "b.cell_range, b.line_start, b.line_end, b.version "
            "FROM knowledge_document_blocks b "
            "JOIN knowledge_documents d ON d.id=b.document_id AND d.tenant_id=b.tenant_id "
            "WHERE b.tenant_id=%s AND d.source_path=%s AND b.version=%s "
            "ORDER BY b.ord",
            (tenant_id, source_path, int(version) if version else _current_version(
                self.store, tenant_id, source_path))).fetchall()
        self.store.connection.commit()
        return [dict(row) for row in rows]

    def document_state(self, *, tenant_id: str, source_path: str) -> dict:
        """The document lifecycle as separate, non-interchangeable states."""
        row = self.store.connection.execute(
            "SELECT status, parse_status, review_state, indexed_generation, "
            "published_generation, withdrawn_at, updated_at "
            "FROM knowledge_documents WHERE tenant_id=%s AND source_path=%s",
            (tenant_id, source_path)).fetchone()
        self.store.connection.commit()
        if row is None:
            raise LookupError("document_not_found")
        return _lifecycle(row) | {
            "parse_status": str(row["parse_status"]),
            "review_state": str(row["review_state"]),
        }


def _lifecycle(row) -> dict:
    """The six document states the interface must keep distinct.

    "Confirmed" (a person accepted the extracted rule or mapping) is deliberately
    not the same as "imported" or "indexed": a document can be saved and parsed
    while still needing review, and indexed while no longer searchable.
    """
    withdrawn = row.get("withdrawn_at") is not None
    parsed_status = str(row.get("parse_status") or "")
    searchable = (row.get("published_generation") is not None
                  and str(row.get("status")) == "ready" and not withdrawn)
    return {
        "saved": True,
        "parsed": parsed_status in {"complete", "partial"},
        "needs_review": str(row.get("review_state")) == "required"
        or parsed_status == "needs_review",
        "indexed": row.get("indexed_generation") is not None and not withdrawn,
        "searchable": searchable,
        "withdrawn": withdrawn,
    }


def _current_version(store: ControlStore, tenant_id: str, source_path: str) -> int:
    row = store.connection.execute(
        "SELECT version FROM knowledge_documents WHERE tenant_id=%s AND source_path=%s",
        (tenant_id, source_path)).fetchone()
    if row is None:
        raise LookupError("document_not_found")
    return int(row["version"])


def _json(value: dict) -> str:
    import json

    return json.dumps(value or {}, ensure_ascii=False, default=str)


def canonical_block_text(blocks: list[DocumentBlock] | list[dict]) -> str:
    """Deterministic text used for content hashing and citation checks."""
    parts = []
    for block in blocks:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "")))
        else:
            parts.append(block.text)
    return "\n".join(parts)


def parse_error_code(error: DocumentError) -> str:
    return error.code
