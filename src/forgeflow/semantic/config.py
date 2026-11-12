"""Validated environment contract for the target semantic stack."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

VECTOR_SIZE = 4096
DEFAULT_COLLECTION = "forgeflow_knowledge_v1"
DEFAULT_TENANT_FIELD = "tenant_id"
DEFAULT_KNOWLEDGE_TENANT = "forgeflow-project"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TENANT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SemanticConfigError(ValueError):
    """A client-safe configuration error; no secret values are included."""


def _url(name: str, value: str) -> str:
    if not value:
        raise SemanticConfigError(f"{name} is required")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise SemanticConfigError(f"{name} must be an absolute HTTPS URL") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SemanticConfigError(f"{name} must be an absolute HTTPS URL")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class SemanticConfig:
    qdrant_url: str
    qdrant_api_key: str = field(repr=False)
    collection: str = DEFAULT_COLLECTION
    tenant_field: str = DEFAULT_TENANT_FIELD
    tenant_id: str = DEFAULT_KNOWLEDGE_TENANT
    embedding_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = field(default="", repr=False)
    reranker_url: str = ""
    reranker_model: str = ""
    reranker_api_key: str = field(default="", repr=False)
    vector_size: int = VECTOR_SIZE

    def __post_init__(self) -> None:
        object.__setattr__(self, "qdrant_url", _url("QDRANT_URL", self.qdrant_url))
        object.__setattr__(self, "embedding_url", _url("EMBEDDING_API_URL", self.embedding_url))
        object.__setattr__(self, "reranker_url", _url("RERANKER_API_URL", self.reranker_url))
        if not self.qdrant_api_key:
            raise SemanticConfigError("QDRANT_API_KEY is required")
        if self.collection != DEFAULT_COLLECTION:
            raise SemanticConfigError(f"QDRANT_COLLECTION must be {DEFAULT_COLLECTION}")
        if self.tenant_field != DEFAULT_TENANT_FIELD:
            raise SemanticConfigError(f"QDRANT_TENANT_FIELD must be {DEFAULT_TENANT_FIELD}")
        if _TENANT_IDENTIFIER.fullmatch(self.tenant_id) is None:
            raise SemanticConfigError("FORGEFLOW_KNOWLEDGE_TENANT is invalid")
        if not self.embedding_model:
            raise SemanticConfigError("EMBEDDING_MODEL_ID is required")
        if not self.reranker_model:
            raise SemanticConfigError("RERANKER_MODEL_ID is required")
        if not self.embedding_api_key or not self.reranker_api_key:
            raise SemanticConfigError("SEMANTIC_API_KEY or SILICONFLOW_API_KEY is required for embedding and reranking")
        if self.vector_size != VECTOR_SIZE:
            raise SemanticConfigError(f"embedding dimension must be {VECTOR_SIZE}")
        if _IDENTIFIER.fullmatch(self.collection) is None:
            raise SemanticConfigError("QDRANT_COLLECTION is invalid")

    @classmethod
    def from_env(cls, source: Mapping[str, str] | None = None) -> "SemanticConfig":
        env = os.environ if source is None else source
        key = (
            env.get("SEMANTIC_API_KEY")
            or env.get("SILICONFLOW_API_KEY")
            or env.get("DASHSCOPE_API_KEY")
            or ""
        ).strip()
        return cls(
            qdrant_url=(env.get("QDRANT_URL") or "").strip(),
            qdrant_api_key=(env.get("QDRANT_API_KEY") or "").strip(),
            collection=(env.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION).strip(),
            tenant_field=(env.get("QDRANT_TENANT_FIELD") or DEFAULT_TENANT_FIELD).strip(),
            tenant_id=(env.get("FORGEFLOW_KNOWLEDGE_TENANT") or DEFAULT_KNOWLEDGE_TENANT).strip(),
            embedding_url=(env.get("EMBEDDING_API_URL") or "").strip(),
            embedding_model=(env.get("EMBEDDING_MODEL_ID") or "").strip(),
            embedding_api_key=key,
            reranker_url=(env.get("RERANKER_API_URL") or "").strip(),
            reranker_model=(env.get("RERANKER_MODEL_ID") or "").strip(),
            reranker_api_key=key,
        )
