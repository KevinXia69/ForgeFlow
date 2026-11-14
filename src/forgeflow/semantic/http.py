"""Verified HTTPS for cloud services and an installation's private Qdrant CA."""
from __future__ import annotations

import ssl
from pathlib import Path

import httpx

from .config import SemanticConfig, SemanticConfigError


def semantic_client(config: SemanticConfig, *, timeout: float) -> httpx.Client:
    # Do not inherit proxies or arbitrary trust environment from a model process.
    # Local installations explicitly supply a bundle containing the normal public
    # roots and their own Qdrant CA. Hostname verification remains enabled.
    verification: ssl.SSLContext | bool = True
    if config.ca_file:
        path = Path(config.ca_file)
        if not path.is_absolute() or not path.is_file():
            raise SemanticConfigError("semantic_ca_file_invalid")
        try:
            verification = ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError):
            raise SemanticConfigError("semantic_ca_file_invalid") from None
    return httpx.Client(timeout=timeout, trust_env=False, verify=verification)
