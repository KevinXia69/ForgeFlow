"""Qwen reranker adapter supporting the DashScope native request shape."""
from __future__ import annotations

import httpx

from .config import SemanticConfig


class RerankError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def rerank(config: SemanticConfig, query: str, documents: list[str], *, top_n: int = 5,
           client: httpx.Client | None = None) -> list[dict]:
    if not query.strip() or not documents:
        raise RerankError("empty_input")
    owns = client is None
    client = client or httpx.Client(timeout=60, trust_env=False)
    try:
        native = not config.reranker_url.rstrip("/").endswith("/rerank")
        body = (
            {
                "model": config.reranker_model,
                "input": {"query": query, "documents": documents},
                "parameters": {"top_n": min(top_n, len(documents)), "return_documents": True},
            }
            if native else {
                "model": config.reranker_model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
            }
        )
        response = client.post(
            config.reranker_url,
            headers={"Authorization": f"Bearer {config.reranker_api_key}"},
            json=body,
        )
    except httpx.HTTPError:
        raise RerankError("reranker_unavailable") from None
    finally:
        if owns:
            client.close()
    if response.status_code in (401, 403):
        raise RerankError("reranker_unauthorized")
    if response.status_code != 200:
        raise RerankError("reranker_error")
    try:
        payload = response.json()
        rows = payload.get("results")
        if not isinstance(rows, list):
            rows = payload["output"]["results"]
        return [
            {"index": int(row["index"]), "score": float(row["relevance_score"]),
             "text": (row.get("document") or {}).get("text", documents[int(row["index"])])}
            for row in rows
        ]
    except (KeyError, TypeError, ValueError, IndexError):
        raise RerankError("reranker_invalid_response") from None
