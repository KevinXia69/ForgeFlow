"""Typed semantic-service failures; never collapsed into a silent fallback."""
from __future__ import annotations


class SemanticServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class SemanticUnavailable(SemanticServiceError):
    def __init__(self) -> None:
        super().__init__("semantic_unavailable", 503)


class SemanticAuthentication(SemanticServiceError):
    def __init__(self) -> None:
        super().__init__("semantic_authentication_failed", 503)


class SemanticRateLimited(SemanticServiceError):
    def __init__(self) -> None:
        super().__init__("semantic_rate_limited", 429)


class SemanticInvalidResponse(SemanticServiceError):
    def __init__(self) -> None:
        super().__init__("semantic_invalid_response", 502)
