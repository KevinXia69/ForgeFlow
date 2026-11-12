"""Semantic document ingestion into a tenant-scoped Qdrant collection."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from .config import SemanticConfig
from .embedding import embed_texts

MAX_CHUNK_CHARS = 1800
MAX_CHUNK_LINES = 60
BATCH_SIZE = 8
_SKIP_PARTS = frozenset({".git", "runs", "vendor", ".runtime", "node_modules", "__pycache__"})
_SKIP_DOC_NAMES = frozenset({
    "LOCAL_WORKBENCH.md", "访谈提纲_鼎捷.md", "局限与边界.md", "PUBLIC_REPO.md",
    "30秒演示脚本.md", "ACTIVITY_TRACE.md", "UI_BENCHMARK_AND_REDESIGN.md",
    "市场与开源对标研究.md", "对标研究计划.md", "国内赛与黑客松提交要求对照.md",
    "NEXT_PHASE_DSH_REUSE.md", "NEXT_PHASE_DEVELOPMENT_PLAN.md",
    "INTERACTION_SEMANTICS_PLAN.md", "INTERACTION_SEMANTICS_REPAIR_PLAN.md",
    "INVESTIGATION_CONTINUATION_PLAN.md", "INVESTIGATION_DEVELOPMENT_PLAN.md",
    "RECOVERY_DECISION_REVIEW_PLAN.md", "REVIEW_UTILITY_DEVELOPMENT_PLAN.md",
    "STRUCTURED_RECOVERY_DEVELOPMENT_PLAN.md", "RECORDING_CORRECTIONS.md",
})
_SKIP_DOC_PREFIXES = ("ForgeFlow_下一阶段", "V9_", "V10_")
_SKIP_ROOT_PREFIXES = ("ForgeFlow_下一阶段方案", "ForgeFlow_下一阶段计划", "ForgeFlow_下一阶段开发方案_v8")


_CJK = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_document_locale(text: str) -> str:
    """Enterprise documents have no filename locale prefix; infer from content.

    A document with no CJK characters is English. Mixed Chinese documents keep
    ``zh`` even when they contain English product names or codes. This is only a
    metadata hint; retrieval can always search across languages.
    """
    value = str(text or "")
    cjk = len(_CJK.findall(value))
    latin = len(_LATIN.findall(value))
    return "en" if cjk == 0 and latin > 0 else "zh"


@dataclass(frozen=True)
class Chunk:
    path: str
    heading: str
    start_line: int
    end_line: int
    text: str
    locale: str = "zh"
    acl_scope: str = "project"
    #: Citable position inside an enterprise document (page/paragraph/cell/row).
    locator: str | None = None
    block_type: str | None = None
    document_id: str | None = None
    document_version: int | None = None

    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def point_id(self, tenant_id: str, generation: str | None = None) -> str:
        # Qdrant point IDs are collection-global. Tenant identity, generation and
        # the exact content hash are part of the identity; a path/line pair alone
        # would let two tenants or two generations overwrite each other.
        identity = "\0".join((tenant_id, generation or "", self.path,
                               str(self.start_line), self.locator or "",
                               self.content_hash()))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def _heading(line: str) -> str:
    return line.lstrip("#").strip() if line.startswith("#") else ""


def locale_for_path(relative: str) -> str:
    parts = Path(relative).parts
    return "en" if parts[:2] == ("docs", "en") or parts[:1] == ("en",) else "zh"


def visible_for_locale(relative: str, locale: str) -> bool:
    parts = Path(relative).parts
    if any(part in _SKIP_PARTS for part in parts):
        return False
    name = Path(relative).name
    if name in _SKIP_DOC_NAMES or name.startswith(_SKIP_DOC_PREFIXES):
        return False
    if len(parts) == 1 and name.startswith(_SKIP_ROOT_PREFIXES):
        return False
    is_english = locale_for_path(relative) == "en"
    if locale == "en":
        return is_english
    if locale == "zh":
        return not is_english
    return False


def acl_scope_for_path(relative: str) -> str:
    parts = Path(relative).parts
    return "engineering" if "evidence" in parts or "research" in parts else "project"


def chunks_from_markdown(path: Path, root: Path, locale: str | None = None,
                         acl_scope: str | None = None) -> list[Chunk]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    relative = path.relative_to(root).as_posix()
    resolved_locale = locale or locale_for_path(relative)
    resolved_scope = acl_scope or acl_scope_for_path(relative)
    chunks: list[Chunk] = []
    heading = ""
    buffer: list[str] = []
    start = 1
    end = 1

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if text:
            chunks.append(Chunk(relative, heading, start, end, text, resolved_locale, resolved_scope))
        buffer = []

    for number, line in enumerate(lines, 1):
        if line.startswith("#"):
            flush()
            heading = _heading(line)
            start = number
        elif len("\n".join(buffer)) + len(line) > MAX_CHUNK_CHARS or len(buffer) >= MAX_CHUNK_LINES:
            flush()
            start = number
        buffer.append(line)
        end = number
    flush()
    return chunks


def ensure_collection(config: SemanticConfig, client: httpx.Client) -> None:
    url = f"{config.qdrant_url}/collections/{config.collection}"
    response = client.get(url, headers={"api-key": config.qdrant_api_key})
    if response.status_code not in (200, 404):
        raise RuntimeError("qdrant_collection_check_failed")
    if response.status_code == 404:
        created = client.put(
            url,
            headers={"api-key": config.qdrant_api_key},
            json={"vectors": {"size": config.vector_size, "distance": "Cosine", "on_disk": True}},
        )
        if created.status_code not in (200, 201):
            raise RuntimeError("qdrant_collection_create_failed")
    for field in (config.tenant_field, "path", "locale", "generation", "status", "acl_scope"):
        indexed = client.put(
            f"{url}/index",
            headers={"api-key": config.qdrant_api_key},
            json={"field_name": field, "field_schema": "keyword"},
        )
        if indexed.status_code not in (200, 201, 409):
            raise RuntimeError("qdrant_index_create_failed")


def delete_source_points(config: SemanticConfig, tenant_id: str, paths: set[str], *,
                         exclude_ids: set[str] | None = None,
                         client: httpx.Client | None = None) -> None:
    if not tenant_id or not paths:
        raise ValueError("tenant_and_paths_required")
    owns = client is None
    client = client or httpx.Client(timeout=60, trust_env=False)
    try:
        for path in paths:
            query: dict = {"must": [
                {"key": config.tenant_field, "match": {"value": tenant_id}},
                {"key": "path", "match": {"value": path}},
            ]}
            if exclude_ids:
                query["must_not"] = [{"has_id": sorted(exclude_ids)}]
            response = client.post(
                f"{config.qdrant_url}/collections/{config.collection}/points/delete?wait=true",
                headers={"api-key": config.qdrant_api_key},
                json={"filter": query},
            )
            if response.status_code not in (200, 201):
                raise RuntimeError("qdrant_delete_failed")
    finally:
        if owns:
            client.close()


def delete_tenant_points(config: SemanticConfig, tenant_id: str, *,
                         client: httpx.Client | None = None) -> None:
    if not tenant_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", tenant_id):
        raise ValueError("invalid_tenant")
    owns = client is None
    client = client or httpx.Client(timeout=60, trust_env=False)
    try:
        response = client.post(
            f"{config.qdrant_url}/collections/{config.collection}/points/delete?wait=true",
            headers={"api-key": config.qdrant_api_key},
            json={"filter": {"must": [{"key": config.tenant_field, "match": {"value": tenant_id}}]}},
        )
        if response.status_code not in (200, 201):
            raise RuntimeError("qdrant_delete_failed")
    finally:
        if owns:
            client.close()


def upsert_chunks(config: SemanticConfig, tenant_id: str, chunks: list[Chunk], *,
                  replace_sources: bool = False, generation: str | None = None,
                  status: str | None = None,
                  client: httpx.Client | None = None) -> int:
    if not tenant_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", tenant_id):
        raise ValueError("invalid_tenant")
    owns = client is None
    client = client or httpx.Client(timeout=120, trust_env=False)
    try:
        ensure_collection(config, client)
        written = 0
        by_path: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            by_path.setdefault(chunk.path, []).append(chunk)
        for path, path_chunks in by_path.items():
            # Embed the complete new source before touching the old vectors.
            # A timeout or provider error therefore leaves the previous source
            # index intact.
            vectors: list[list[float]] = []
            for start in range(0, len(path_chunks), BATCH_SIZE):
                batch = path_chunks[start:start + BATCH_SIZE]
                vectors.extend(embed_texts(config, [chunk.text for chunk in batch], client=client))
            points = [
                {
                    "id": chunk.point_id(tenant_id, generation),
                    "vector": vector,
                    "payload": {
                        "tenant_id": tenant_id,
                        "path": chunk.path,
                        "heading": chunk.heading,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "text": chunk.text,
                        "content_hash": chunk.content_hash(),
                        "locale": chunk.locale,
                        "acl_scope": chunk.acl_scope,
                        **({"locator": chunk.locator} if chunk.locator else {}),
                        **({"block_type": chunk.block_type} if chunk.block_type else {}),
                        **({"document_id": chunk.document_id}
                           if chunk.document_id else {}),
                        **({"document_version": chunk.document_version}
                           if chunk.document_version is not None else {}),
                        **({"generation": generation} if generation else {}),
                        **({"status": status} if status else {}),
                    },
                }
                for chunk, vector in zip(path_chunks, vectors, strict=True)
            ]
            for start in range(0, len(points), BATCH_SIZE):
                response = client.put(
                    f"{config.qdrant_url}/collections/{config.collection}/points?wait=true",
                    headers={"api-key": config.qdrant_api_key},
                    json={"points": points[start:start + BATCH_SIZE]},
                )
                if response.status_code not in (200, 201):
                    raise RuntimeError("qdrant_upsert_failed")
            written += len(points)
            if replace_sources:
                # Activate the new IDs first; only then remove stale versions.
                # A failed delete leaves duplicates, but never removes the last
                # usable copy of a source.
                delete_source_points(
                    config, tenant_id, {path},
                    exclude_ids={point["id"] for point in points}, client=client,
                )
        return written
    finally:
        if owns:
            client.close()


def publish_generation(config: SemanticConfig, tenant_id: str, generation: str, *,
                       client: httpx.Client | None = None) -> None:
    owns = client is None
    client = client or httpx.Client(timeout=60, trust_env=False)
    try:
        response = client.post(
            f"{config.qdrant_url}/collections/{config.collection}/points/payload?wait=true",
            headers={"api-key": config.qdrant_api_key},
            json={
                "payload": {"status": "published"},
                "filter": {"must": [
                    {"key": config.tenant_field, "match": {"value": tenant_id}},
                    {"key": "generation", "match": {"value": generation}},
                ]},
            },
        )
        if response.status_code not in (200, 201):
            raise RuntimeError("qdrant_publish_failed")
    finally:
        if owns:
            client.close()


def index_root(config: SemanticConfig, root: Path, tenant_id: str,
               *, generation: str | None = None,
               catalog=None) -> int:
    root = Path(root)
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        locale = locale_for_path(relative)
        if not visible_for_locale(relative, locale):
            continue
        chunks.extend(chunks_from_markdown(path, root, locale=locale,
                                            acl_scope=acl_scope_for_path(relative)))
    if catalog is not None:
        generation = generation or catalog.new_generation(tenant_id)
        written = upsert_chunks(config, tenant_id, chunks, generation=generation,
                                status="staging", replace_sources=False)
        publish_generation(config, tenant_id, generation)
        catalog.publish(tenant_id, generation)
        return written
    return upsert_chunks(config, tenant_id, chunks, replace_sources=True,
                         generation=generation)


class IndexQuotaExceeded(RuntimeError):
    """The tenant or the deployment exhausted its daily index-task quota."""

    code = "index_quota_exhausted"

    def __init__(self, decision: dict | None = None) -> None:
        super().__init__(self.code)
        self.decision = decision or {}


def consume_index_task(store, tenant_id: str, *, amount: int = 1,
                       quotas=None) -> dict:
    """Charge one index task before embeddings are requested.

    Indexing costs vendor money even though it writes no ERP document, so it is
    governed by the same per-tenant/global daily quota as model and tool work.
    Zero (the default) keeps existing deployments unlimited.
    """
    from ..superagent.quotas import QuotaLimits, QuotaStore

    guard = quotas if quotas is not None else QuotaStore(store, QuotaLimits.from_env())
    decision = guard.consume(tenant_id, "index_tasks", max(1, int(amount)))
    if not decision.get("allowed"):
        raise IndexQuotaExceeded(decision)
    return decision


def chunks_from_text(text: str, *, path: str, acl_scope: str = "project",
                     locale: str = "zh", heading: str = "") -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    buffer: list[str] = []
    start = 1
    end = 1

    def flush() -> None:
        nonlocal buffer
        value = "\n".join(buffer).strip()
        if value:
            chunks.append(Chunk(path, heading, start, end, value, locale, acl_scope))
        buffer = []

    for number, line in enumerate(lines, 1):
        if len("\n".join(buffer)) + len(line) > MAX_CHUNK_CHARS or len(buffer) >= MAX_CHUNK_LINES:
            flush()
            start = number
        buffer.append(line)
        end = number
    flush()
    return chunks


def catalog_block_chunks(store, tenant_id: str, *,
                         source_path: str | None = None) -> list[Chunk]:
    """Build retrieval chunks from the structured block tree.

    A chunk carries the locator of its first block, so a citation can point at a
    PDF page and line, a Word paragraph or table cell, an Excel sheet and cell, or
    a CSV row and field instead of a character offset in a flattened string.
    """
    rows = store.connection.execute(
        "SELECT d.id, d.source_path, d.filename, d.version, d.acl_scope, "
        "d.content, b.id AS block_id, b.parent_id, b.table_index, "
        "b.ord, b.block_type, b.text, b.locator, b.line_start, b.line_end, "
        "b.page, b.sheet, b.cell_range, b.section_path "
        "FROM knowledge_documents d "
        "JOIN knowledge_document_blocks b ON b.document_id = d.id "
        "AND b.version = d.version AND b.tenant_id = d.tenant_id "
        "WHERE d.tenant_id=%s AND d.status='ready' AND d.withdrawn_at IS NULL "
        + ("AND d.source_path=%s " if source_path is not None else "")
        + "ORDER BY d.source_path, b.ord",
        (tenant_id, source_path) if source_path is not None else (tenant_id,)).fetchall()
    store.connection.commit()
    chunks: list[Chunk] = []
    buffer: list = []
    size = 0
    locale_by_document = {
        str(row["id"]): detect_document_locale(str(row["content"] or ""))
        for row in rows}
    # Native parsers expand tables into rows/cells. DocMind can instead put
    # all table content in the table block, with no children to index.
    children = [row for row in rows if row["block_type"] in {"row", "cell"}
                and str(row["text"]).strip()]
    expanded_parents = {row["parent_id"] for row in children
                        if row["parent_id"] is not None}
    expanded_tables = {(row["id"], row["version"], row["table_index"]) for row in children
                       if row["table_index"] is not None}

    def flush() -> None:
        nonlocal size
        if not buffer:
            return
        head = buffer[0]
        chunks.append(Chunk(
            path=f"document://{head['source_path']}",
            heading=str(head["filename"]),
            start_line=int(head["line_start"] or 1),
            end_line=int(buffer[-1]["line_end"] or head["line_start"] or 1),
            text="\n".join(str(item["text"]) for item in buffer),
            acl_scope=str(head["acl_scope"]),
            locator=str(head["locator"]),
            block_type=str(head["block_type"]),
            document_id=str(head["id"]),
            document_version=int(head["version"]),
            locale=locale_by_document[str(head["id"])]))
        buffer.clear()
        size = 0

    for row in rows:
        block_type = str(row["block_type"])
        text = str(row["text"])
        if block_type == "page" or not text.strip():
            continue
        if block_type == "table":
            if (row["block_id"] in expanded_parents
                    or (row["id"], row["version"], row["table_index"]) in expanded_tables):
                continue
            # Keep a content-only table's own locator instead of citing the
            # preceding heading or paragraph for its prices and quantities.
            flush()
        if buffer and (size + len(text) > MAX_CHUNK_CHARS
                       or buffer[0]["source_path"] != row["source_path"]
                       or len(buffer) >= MAX_CHUNK_LINES):
            flush()
        buffer.append(row)
        size += len(text)
        if block_type == "table":
            flush()
    flush()
    return chunks


def _catalog_text_chunks(row) -> list[Chunk]:
    """Preserve catalog identity even for legacy documents without blocks.

    Such documents retain line citations; do not invent a structured locator
    that is absent from the persisted block tree.
    """
    return [replace(chunk, document_id=str(row["id"]),
                    document_version=int(row["version"]))
            for chunk in chunks_from_text(
                str(row["content"]), path=f"document://{row['source_path']}",
                acl_scope=str(row["acl_scope"]),
                locale=detect_document_locale(str(row["content"] or "")),
                heading=f"{row['filename']} v{row['version']}")]


def catalog_chunks(store, tenant_id: str,
                   exclude_paths: set[str] | None = None) -> list[Chunk]:
    """Convert persisted enterprise-document content into semantic chunks.

    ``exclude_paths`` lets a caller index the documents that have structure with
    ``catalog_block_chunks`` and fall back to this flattened view for the rest, one
    document at a time instead of all-or-nothing per tenant.
    """
    rows = store.connection.execute(
        "SELECT id, source_path, filename, content, acl_scope, version "
        "FROM knowledge_documents WHERE tenant_id=%s AND status='ready' AND withdrawn_at IS NULL "
        "ORDER BY source_path", (tenant_id,)).fetchall()
    chunks: list[Chunk] = []
    excluded = exclude_paths or set()
    for row in rows:
        if str(row["source_path"]) in excluded:
            continue
        chunks.extend(_catalog_text_chunks(row))
    return chunks


def _catalog_source_chunks(store, tenant_id: str, source_path: str) -> list[Chunk]:
    # Every publication path uses the same persisted structure and citation
    # identity, including uploads into an already published generation.
    chunks = catalog_block_chunks(store, tenant_id, source_path=source_path)
    if chunks:
        return chunks
    row = store.connection.execute(
        "SELECT id, source_path, content, acl_scope, filename, version "
        "FROM knowledge_documents WHERE tenant_id=%s AND source_path=%s",
        (tenant_id, source_path)).fetchone()
    store.connection.commit()
    if row is None:
        raise LookupError("document_not_found")
    return _catalog_text_chunks(row)


def _mark_catalog_chunks_indexed(store, tenant_id: str, generation: str,
                                 chunks: list[Chunk], document_catalog=None) -> None:
    from ..knowledge.documents_catalog import DocumentCatalog

    revisions = {(chunk.path[len("document://"):], chunk.document_id, chunk.document_version)
                 for chunk in chunks if chunk.path.startswith("document://")
                 and chunk.document_id and chunk.document_version is not None}
    if not revisions:
        return
    documents = document_catalog if document_catalog is not None else DocumentCatalog(store)
    for source_path, document_id, version in sorted(revisions):
        documents.mark_indexed(tenant_id=tenant_id, source_path=source_path,
                               generation=generation, document_id=document_id, version=version)


def index_catalog_documents(config: SemanticConfig, store, tenant_id: str,
                            catalog, generation: str | None = None, *,
                            document_catalog=None) -> int:
    """Stage and publish catalog documents through the existing generation flow."""
    consume_index_task(store, tenant_id)
    generation = generation or catalog.new_generation(tenant_id)
    # Per document, not per tenant: a single document with structure must not
    # hide every older document that only has flattened text.
    block_chunks = catalog_block_chunks(store, tenant_id)
    covered = {chunk.path[len("document://"):] for chunk in block_chunks}
    chunks = block_chunks + catalog_chunks(store, tenant_id, exclude_paths=covered)
    written = upsert_chunks(config, tenant_id, chunks, generation=generation,
                            status="staging", replace_sources=False)
    publish_generation(config, tenant_id, generation)
    catalog.publish(tenant_id, generation)
    _mark_catalog_chunks_indexed(store, tenant_id, generation, chunks, document_catalog)
    return written


def _delete_stale_generation_points(config: SemanticConfig, tenant_id: str,
                                    generation: str, path: str,
                                    keep_ids: set[str], *, client: httpx.Client) -> None:
    query = {"must": [
        {"key": config.tenant_field, "match": {"value": tenant_id}},
        {"key": "generation", "match": {"value": generation}},
        {"key": "path", "match": {"value": path}},
    ], "must_not": [{"has_id": sorted(keep_ids)}] if keep_ids else []}
    response = client.post(
        f"{config.qdrant_url}/collections/{config.collection}/points/delete?wait=true",
        headers={"api-key": config.qdrant_api_key}, json={"filter": query})
    if response.status_code not in (200, 201):
        raise RuntimeError("qdrant_delete_failed")


def index_document_generation(config: SemanticConfig, store, tenant_id: str,
                              source_path: str, generation: str,
                              client: httpx.Client) -> int:
    """Index one catalog document into an explicit (staging) generation."""
    chunks = _catalog_source_chunks(store, tenant_id, source_path)
    return upsert_chunks(config, tenant_id, chunks, generation=generation,
                         status="staging", replace_sources=False, client=client)


def ensure_document_indexed(config: SemanticConfig, store, tenant_id: str,
                           source_path: str, catalog,
                           document_catalog=None) -> int:
    """Make a parsed document searchable, bootstrapping the first generation."""
    if catalog.published(tenant_id) is None:
        return index_catalog_documents(config, store, tenant_id, catalog,
                                        document_catalog=document_catalog)
    return index_single_document(
        config, store, tenant_id, source_path, catalog, document_catalog)


def index_single_document(config: SemanticConfig, store, tenant_id: str,
                          source_path: str, catalog,
                          document_catalog=None) -> int:
    """Refresh one catalog document inside the current published generation."""
    consume_index_task(store, tenant_id)
    chunks = _catalog_source_chunks(store, tenant_id, source_path)
    generation = catalog.published(tenant_id)
    if not generation:
        raise ValueError("published_generation_missing")
    path = f"document://{source_path}"
    with httpx.Client(timeout=120, trust_env=False) as client:
        written = upsert_chunks(config, tenant_id, chunks, generation=generation,
                                status="published", replace_sources=False, client=client)
        ids = {chunk.point_id(tenant_id, generation) for chunk in chunks}
        _delete_stale_generation_points(config, tenant_id, generation, path, ids, client=client)
    _mark_catalog_chunks_indexed(store, tenant_id, generation, chunks, document_catalog)
    return written
