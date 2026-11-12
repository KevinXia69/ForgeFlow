"""Dense retrieval followed by neural reranking and source-bound results."""
from __future__ import annotations

import httpx

from .config import SemanticConfig
from .embedding import embed_texts
from .errors import (
    SemanticAuthentication,
    SemanticInvalidResponse,
    SemanticRateLimited,
    SemanticUnavailable,
)
from .rerank import rerank


def search(config: SemanticConfig, tenant_id: str, query: str, *,
           limit: int = 20, top_k: int = 5, locale: str | None = None,
           source_paths: frozenset[str] | set[str] | None = None,
           acl_scopes: frozenset[str] | set[str] | None = None,
           generation: str | None = None,
           client: httpx.Client | None = None) -> list[dict]:
    if not tenant_id or not query.strip():
        raise ValueError("tenant_and_query_required")
    # Fail closed before any provider call: an empty authorised set means the
    # caller may read nothing, not that the filter is optional.
    if source_paths is not None and not source_paths:
        return []
    if acl_scopes is not None and not acl_scopes:
        return []
    owns = client is None
    client = client or httpx.Client(timeout=120, trust_env=False)
    try:
        vector = embed_texts(config, [query], client=client)[0]
        try:
            response = client.post(
                f"{config.qdrant_url}/collections/{config.collection}/points/search",
                headers={"api-key": config.qdrant_api_key},
                json={
                "vector": vector,
                "limit": max(1, min(limit, 50)),
                "with_payload": True,
                "filter": {
                    "must": [
                        {"key": config.tenant_field, "match": {"value": tenant_id}},
                        *([{"key": "locale", "match": {"value": locale}}] if locale else []),
                        *([{"key": "generation", "match": {"value": generation}}] if generation else []),
                        *([{"key": "status", "match": {"value": "published"}}] if generation else []),
                        *([{"key": "acl_scope", "match": {"any": sorted(acl_scopes)}}]
                          if acl_scopes else []),
                    ],
                    **({"should": [
                        {"key": "path", "match": {"value": path}}
                        for path in sorted(source_paths)
                    ]} if source_paths else {}),
                },
            },
            )
        except httpx.HTTPError:
            raise SemanticUnavailable() from None
        if response.status_code in (401, 403):
            raise SemanticAuthentication() from None
        if response.status_code == 429:
            raise SemanticRateLimited() from None
        if response.status_code >= 500:
            raise SemanticUnavailable() from None
        if response.status_code != 200:
            raise SemanticInvalidResponse() from None
        try:
            rows = response.json().get("result", [])
        except (ValueError, AttributeError):
            raise SemanticInvalidResponse() from None
        if not rows:
            return []
        ranked = rerank(config, query, [row.get("payload", {}).get("text", "") for row in rows],
                        top_n=min(top_k, len(rows)), client=client)
        results = []
        for item in ranked:
            row = rows[item["index"]]
            payload = row.get("payload", {})
            results.append({
                "score": item["score"],
                "path": payload.get("path"),
                "heading": payload.get("heading"),
                "start_line": payload.get("start_line"),
                "end_line": payload.get("end_line"),
                "text": payload.get("text", item["text"]),
                # Structured citation metadata, present for enterprise documents.
                "locator": payload.get("locator"),
                "block_type": payload.get("block_type"),
                "document_id": payload.get("document_id"),
                "document_version": payload.get("document_version"),
                "acl_scope": payload.get("acl_scope"),
            })
        return results
    finally:
        if owns:
            client.close()
