"""OpenAI-compatible embedding adapter with exact-dimension enforcement."""
from __future__ import annotations

import httpx

from .config import VECTOR_SIZE, SemanticConfig


class EmbeddingError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def embed_texts(config: SemanticConfig, texts: list[str], client: httpx.Client | None = None) -> list[list[float]]:
    if not texts or any(not text.strip() for text in texts):
        raise EmbeddingError("empty_input")
    owns = client is None
    client = client or httpx.Client(timeout=60, trust_env=False)
    try:
        response = client.post(
            config.embedding_url,
            headers={"Authorization": f"Bearer {config.embedding_api_key}"},
            json={
                "model": config.embedding_model,
                "input": texts,
                "dimensions": VECTOR_SIZE,
                "encoding_format": "float",
            },
        )
    except httpx.HTTPError:
        raise EmbeddingError("embedding_unavailable") from None
    finally:
        if owns:
            client.close()
    if response.status_code in (401, 403):
        raise EmbeddingError("embedding_unauthorized")
    if response.status_code != 200:
        raise EmbeddingError("embedding_error")
    try:
        rows = response.json().get("data", [])
        vectors = [row["embedding"] for row in rows]
    except (KeyError, TypeError, ValueError):
        raise EmbeddingError("embedding_invalid_response") from None
    if len(vectors) != len(texts):
        raise EmbeddingError("embedding_count_mismatch")
    if any(not isinstance(vector, list) or len(vector) != VECTOR_SIZE for vector in vectors):
        raise EmbeddingError(f"embedding_dimension_mismatch:{VECTOR_SIZE}")
    return vectors
