"""Configured semantic retrieval for the Odoo 20 target architecture.

The semantic path never falls back to BM25. A missing or mismatched provider is
reported as an explicit availability error.
"""
from .config import SemanticConfig, SemanticConfigError
from .embedding import EmbeddingError, embed_texts
from .errors import (
    SemanticAuthentication,
    SemanticInvalidResponse,
    SemanticRateLimited,
    SemanticServiceError,
    SemanticUnavailable,
)
from .qdrant import QdrantError, check_collection
from .rerank import RerankError, rerank

__all__ = [
    "EmbeddingError", "QdrantError", "RerankError", "SemanticAuthentication",
    "SemanticConfig", "SemanticInvalidResponse", "SemanticRateLimited",
    "SemanticServiceError", "SemanticUnavailable",
    "SemanticConfigError", "check_collection", "embed_texts", "rerank",
]
