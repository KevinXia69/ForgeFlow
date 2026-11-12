"""Turn a Document Mind result into a catalogue document and index entry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .docmind import layouts_to_blocks
from .documents import PARSE_COMPLETE, DocumentBlock, ParsedDocument
from .documents_catalog import DocumentCatalog


def persist_original(root: str | Path, *, tenant_id: str, sha256: str,
                     extension: str, raw: bytes) -> Path:
    """Store the original bytes once, privately, under a content address."""
    folder = Path(root) / "knowledge-originals" / hashlib.sha256(
        tenant_id.encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / f"{sha256}.{extension.lstrip('.') or 'bin'}"
    if not target.exists():
        temporary = folder / f".{sha256}.tmp"
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        temporary.replace(target)
    return target


def parsed_document(result: dict[str, Any], *, job: dict[str, Any],
                    filename: str, extension: str) -> ParsedDocument:
    raw_blocks = layouts_to_blocks(
        result, document_id=str(job["document_id"]),
        version=int(job["document_version"]),
        source_sha256=str(job["source_sha256"]))
    blocks = tuple(
        DocumentBlock(
            ord=int(item["ord"]), block_type=str(item["block_type"]),
            text=str(item["text"]), locator=str(item["locator"]),
            page=item.get("page"))
        for item in raw_blocks)
    page_count = (result.get("page_count")
                  or max((item["page"] or 0 for item in raw_blocks), default=0))
    return ParsedDocument(
        filename=filename, kind=extension, text="\n\n".join(b.text for b in blocks),
        blocks=blocks, page_count=int(page_count or 0),
        parse_status=PARSE_COMPLETE,
        review_state="not_required",
        detail={"source": "document_mind", "vendor_job_id": job.get("vendor_job_id"),
                "layout_count": len(raw_blocks),
                "positions": [item.get("pos") for item in raw_blocks if item.get("pos")]},
        source_kind="upload")


def write_result(store, *, job: dict[str, Any], result: dict[str, Any],
                 acl_scope: str = "project", catalog=None) -> dict[str, Any]:
    """Write the parsed document to the catalogue; indexing is the caller's job."""
    conversion = _json(job.get("conversion_json"))
    acl_scope = str(conversion.get("acl_scope") or acl_scope)
    filename = str(conversion.get("filename") or Path(job["source_path"]).name)
    extension = str(conversion.get("extension")
                    or Path(filename).suffix.lstrip("."))
    parsed = parsed_document(result, job=job, filename=filename,
                             extension=extension)
    original = Path(str(conversion.get("original_path") or ""))
    raw = original.read_bytes() if original.is_file() else b""
    record = (catalog or DocumentCatalog(store)).upsert(
        tenant_id=str(job["tenant_id"]), source_path=str(job["source_path"]),
        parsed=parsed, raw=raw, acl_scope=acl_scope,
        classification=None)
    return {"document_id": record.id, "version": record.version,
            "blocks": record.block_count, "page_count": parsed.page_count,
            "source_path": record.source_path}


def _json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
