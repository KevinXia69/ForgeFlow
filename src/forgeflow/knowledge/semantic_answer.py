"""Server-bound semantic answer adapter for the active knowledge endpoints.

Two fail-closed rules are enforced here, because both used to fail open:

* an **empty** authorised source set means "readable sources: none", never "no
  restriction";
* an **empty** ACL scope set means the caller may read nothing.

ACL is applied before recall and therefore before the reranker sees any text.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..semantic import SemanticConfig, SemanticConfigError
from ..semantic.search import search
from .citation_metadata import reference_metadata

#: Default enterprise policy, used only when no control store is configured.
DEFAULT_ROLE_SCOPES: dict[str, frozenset[str]] = {
    "admin": frozenset({"project", "engineering"}),
    "planner": frozenset({"project"}),
    "approver": frozenset({"project"}),
    "observer": frozenset({"project"}),
}


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    """Server-resolved retrieval scope; never constructed from request body."""

    tenant_id: str
    locale: str = "zh"
    #: ``None`` means "no source restriction"; an empty set means nothing is readable.
    source_paths: frozenset[str] | None = None
    generation: str | None = None
    role: str = "observer"
    acl_scopes: frozenset[str] | None = None
    #: Named principal, for audit and per-principal ACLs.
    principal_id: str | None = None
    #: Mandate-scoped knowledge restriction; empty means nothing readable.
    mandate_scopes: frozenset[str] | None = field(default=None)
    #: Optional source-language filter. None searches every authorized language;
    #: answer ``locale`` must not implicitly hide other language sources.
    source_locale: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"admin", "planner", "approver", "observer"}:
            raise ValueError("invalid_knowledge_role")


def resolve_acl_scopes(tenant_id: str, role: str, *,
                       store=None) -> frozenset[str]:
    """Resolve the ACL scopes a role may read in this tenant.

    The store is the authority.  When it has no rows for the tenant the result is
    empty -- a role that was not granted anything reads nothing.
    """
    if store is None:
        return DEFAULT_ROLE_SCOPES.get(role, frozenset())
    rows = store.connection.execute(
        "SELECT acl_scope FROM knowledge_role_scopes WHERE tenant_id=%s AND role=%s",
        (tenant_id, role)).fetchall()
    store.connection.commit()
    return frozenset(str(row["acl_scope"]) for row in rows)


def _safe_path(root: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    base = root.resolve()
    candidates = [(base / relative).resolve(), (base / "docs" / relative).resolve()]
    try:
        for path in candidates:
            if path.is_relative_to(base) and path.is_file():
                return path
    except (OSError, RuntimeError):
        return None
    return None


def _prefilter_paths(paths: frozenset[str]) -> set[str]:
    values: set[str] = set()
    for path in paths:
        values.add(path)
        if path.startswith("docs/"):
            values.add(path[5:])
    return values


def _empty(reason: str) -> dict:
    return {"found": False, "passages": [], "citations": [], "reason": reason}


def semantic_answer(
    question: str,
    root: str | Path,
    limit: int = 5,
    locale: str = "zh",
    *,
    source_paths: Sequence[str] | None = None,
    scope: KnowledgeScope | None = None,
    config: SemanticConfig | None = None,
    store=None,
    source_locale: str | None = None,
) -> dict | None:
    """Return a server-scoped semantic result, or None when no service is configured.

    Tenant identity comes only from validated environment configuration or a
    server-built scope. Callers cannot provide it. A configured service that
    fails is an explicit error, not a BM25 fallback.
    """
    try:
        resolved = config or SemanticConfig.from_env()
    except SemanticConfigError:
        # Partially configured service must fail closed at the caller rather
        # than silently selecting the legacy path.
        if any(key in os.environ for key in (
            "QDRANT_URL", "QDRANT_API_KEY", "SEMANTIC_API_KEY",
            "EMBEDDING_API_URL", "EMBEDDING_MODEL_ID",
            "RERANKER_API_URL", "RERANKER_MODEL_ID",
        )):
            raise
        return None
    tenant_id = resolved.tenant_id
    resolved_locale = locale
    if scope is not None and scope.tenant_id != tenant_id:
        raise ValueError("knowledge_scope_tenant_mismatch")

    control_url = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
    owned_store = None
    if store is None and control_url:
        from ..superagent.store import ControlStore

        owned_store = ControlStore(control_url)
        store = owned_store
    try:
        # -- authorised sources -------------------------------------------
        if scope is not None:
            allowed_sources = scope.source_paths
        elif source_paths is not None:
            allowed_sources = frozenset(source_paths)
        else:
            allowed_sources = None
        if allowed_sources is not None and len(allowed_sources) == 0:
            return _empty("no_readable_sources")

        # -- ACL -----------------------------------------------------------
        effective_role = scope.role if scope is not None else "observer"
        acl_scopes = scope.acl_scopes if scope is not None else None
        if acl_scopes is None:
            try:
                acl_scopes = resolve_acl_scopes(tenant_id, effective_role, store=store)
            except Exception:  # a missing schema is "nothing granted", never "all"
                acl_scopes = DEFAULT_ROLE_SCOPES.get(effective_role, frozenset()) \
                    if store is None else frozenset()
        if scope is not None and scope.mandate_scopes is not None:
            acl_scopes = frozenset(acl_scopes) & frozenset(scope.mandate_scopes)
        if not acl_scopes:
            return _empty("no_authorized_scope")

        # -- published generation -----------------------------------------
        generation = scope.generation if scope is not None else None
        if generation is None and store is not None:
            from .catalog import KnowledgeCatalog

            catalog = KnowledgeCatalog(store)
            generation = catalog.published(tenant_id)
            if generation is None:
                # No published generation means no readable knowledge; searching
                # without a generation filter would surface staging content.
                return _empty("no_published_generation")

        rows = search(
            resolved,
            tenant_id,
            question,
            limit=max(20, min(50, limit * 4)),
            top_k=max(limit * 4, limit),
            locale=(source_locale if source_locale is not None
                    else (scope.source_locale if scope is not None else None)),
            source_paths=(frozenset(_prefilter_paths(allowed_sources))
                          if allowed_sources is not None else None),
            acl_scopes=frozenset(acl_scopes),
            generation=generation,
        )
        base = Path(root).resolve()
        allowed = set(allowed_sources) if allowed_sources is not None else None
        passages = []
        for row in rows:
            raw_path = str(row.get("path", ""))
            if raw_path.startswith("document://"):
                if allowed is not None and raw_path not in allowed:
                    continue
                passages.append({
                    "path": raw_path,
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                    "text": str(row["text"]),
                    "score": float(row["score"]),
                    "locator": row.get("locator"),
                    "block_type": row.get("block_type"),
                    "document_id": row.get("document_id"),
                    "document_version": row.get("document_version"),
                })
                if len(passages) >= limit:
                    break
                continue
            path = _safe_path(base, raw_path)
            if path is None:
                continue
            relative = path.relative_to(base).as_posix()
            if allowed is not None and relative not in allowed:
                continue
            passages.append({
                "path": relative,
                "start_line": int(row["start_line"]),
                "end_line": int(row["end_line"]),
                "text": str(row["text"]),
                "score": float(row["score"]),
                "locator": row.get("locator"),
            })
            if len(passages) >= limit:
                break
        # ACL can be tightened without a reindex; verify the current state before
        # anything is returned to the caller.
        passages = _verify_document_visibility(store, tenant_id, passages, acl_scopes)
        result = {
            "found": bool(passages),
            "passages": passages,
            "citations": [_citation(p) for p in passages],
            "generation": generation,
            "acl_scopes": sorted(acl_scopes),
        }
        if resolved_locale != "zh":
            result["locale"] = resolved_locale
        return result
    finally:
        if owned_store is not None:
            owned_store.close()


def _verify_document_visibility(store, tenant_id: str, passages: list[dict],
                                acl_scopes: frozenset[str] | set[str]) -> list[dict]:
    """Re-check the *current* catalogue state of every enterprise passage.

    Vectors carry the ACL they were indexed with, and tightening a document from
    ``project`` to ``engineering`` does not rewrite the old points.  Without this
    check there is a window -- from the ACL change until the reindex finishes --
    in which a role that lost access can still read the old content.

    A document that is not in the catalogue cannot be verified, so it is dropped:
    "not found" is not "not restricted".
    """
    if store is None:
        return passages
    managed = {passage["path"] for passage in passages
               if str(passage.get("path", "")).startswith("document://")}
    if not managed:
        return passages
    paths = [path[len("document://"):] for path in managed]
    rows = store.connection.execute(
        "SELECT source_path, acl_scope, status, parse_status, withdrawn_at, "
        "id, version, sha256, source_kind, classification, parse_detail "
        "FROM knowledge_documents WHERE tenant_id=%s AND source_path = ANY(%s)",
        (tenant_id, paths)).fetchall()
    store.connection.commit()
    current = {f"document://{row['source_path']}": row for row in rows}
    kept: list[dict] = []
    for passage in passages:
        path = str(passage.get("path", ""))
        if not path.startswith("document://"):
            kept.append(passage)
            continue
        row = current.get(path)
        if row is None:
            continue
        if row["withdrawn_at"] is not None or str(row["status"]) != "ready":
            continue
        if str(row["parse_status"]) == "failed":
            continue
        if str(row["acl_scope"]) not in acl_scopes:
            continue
        # Labels are derived only after the current ACL/lifecycle check, and
        # only when the captured version matches the provenance's content hash.
        metadata = reference_metadata(row, passage)
        captured = {key: value for key, value in passage.items() if key != "source_metadata"}
        kept.append({**captured, **({"source_metadata": metadata} if metadata else {})})
    return kept


def _citation(passage: dict) -> str:
    if passage.get("locator"):
        return f"{passage['path']}#{passage['locator']}"
    return f"{passage['path']}:{passage['start_line']}-{passage['end_line']}"
