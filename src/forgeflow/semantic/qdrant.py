"""Minimal Qdrant read-side checks; no index is created without schema proof."""
from __future__ import annotations

import httpx

from .config import SemanticConfig


class QdrantError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _vector_params(info: dict) -> dict:
    params = info.get("config", {}).get("params", {}).get("vectors", {})
    if isinstance(params, dict) and "" in params:
        return params[""]
    return params if isinstance(params, dict) else {}


def check_collection(config: SemanticConfig, client: httpx.Client | None = None) -> dict:
    owns = client is None
    client = client or httpx.Client(timeout=20, trust_env=False)
    try:
        response = client.get(
            f"{config.qdrant_url}/collections/{config.collection}",
            headers={"api-key": config.qdrant_api_key},
        )
    except httpx.HTTPError:
        raise QdrantError("qdrant_unavailable") from None
    finally:
        if owns:
            client.close()
    if response.status_code == 404:
        raise QdrantError("collection_missing")
    if response.status_code in (401, 403):
        raise QdrantError("qdrant_unauthorized")
    if response.status_code != 200:
        raise QdrantError("qdrant_error")
    info = response.json().get("result", {})
    vector = _vector_params(info)
    if vector.get("size") != config.vector_size:
        raise QdrantError(f"vector_size_mismatch:{vector.get('size')}")
    if str(vector.get("distance", "")).lower() != "cosine":
        raise QdrantError("distance_mismatch")
    if vector.get("on_disk") is not True:
        raise QdrantError("vector_not_on_disk")
    if config.tenant_field not in (info.get("payload_schema") or {}):
        raise QdrantError("tenant_index_missing")
    return {
        "collection": config.collection,
        "vector_size": vector.get("size"),
        "distance": vector.get("distance"),
        "on_disk": vector.get("on_disk"),
        "tenant_field": config.tenant_field,
        "points_count": info.get("points_count"),
    }
