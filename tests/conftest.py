"""Opt-in controlled replacements for fake Harbor integration tests."""
import os
from pathlib import Path

import pytest

#: Environment keys for the live semantic stack.  The suite is hermetic by
#: default: a developer machine that happens to have Qdrant configured must not
#: change what the ordinary tests assert.  Live tests opt back in explicitly.
SEMANTIC_ENV_KEYS = (
    "QDRANT_URL", "QDRANT_API_KEY", "SEMANTIC_API_KEY", "EMBEDDING_API_URL",
    "EMBEDDING_MODEL_ID", "RERANKER_API_URL", "RERANKER_MODEL_ID",
)
SEMANTIC_ENV = {key: os.environ.get(key, "") for key in SEMANTIC_ENV_KEYS}


@pytest.fixture(autouse=True)
def _hermetic_semantic_env(monkeypatch):
    """No live semantic configuration unless a test asks for it."""
    for key in SEMANTIC_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def live_semantic_env(monkeypatch):
    """Restore the ambient semantic configuration for a live integration test."""
    restored = False
    for key, value in SEMANTIC_ENV.items():
        if value:
            monkeypatch.setenv(key, value)
            restored = True
    if not restored:
        pytest.skip("semantic/control credentials not set")
    return True


@pytest.fixture
def synthetic_host_runtime(monkeypatch):
    """Runner fixtures use their fake Harbor; real runtime copying has separate checks."""
    from forgeflow import host_runtime

    def prepare(source, cache, host_python):
        packages = cache / "synthetic-packages"
        packages.mkdir(parents=True, exist_ok=True)
        return {"python": str(host_python), "packages": str(packages),
                "root": str(cache), "host_dependencies_frozen": True}

    monkeypatch.setattr(host_runtime, "ensure_host_runtime", prepare)
    monkeypatch.setattr(host_runtime, "verify_host_runtime", lambda record: {"ok": True})
    monkeypatch.setattr(host_runtime, "harbor_command", lambda record, source: [
        str(Path(record["python"]).with_name("harbor"))])



@pytest.fixture(autouse=True)
def _explicit_knowledge_test_adapter(monkeypatch):
    """Tests opt into a deterministic adapter; production never falls back."""
    import importlib

    module = importlib.import_module("forgeflow.knowledge.answer")
    from support.legacy_answer import _legacy_answer, _legacy_market_answer
    monkeypatch.setattr(module, "_TEST_ADAPTER", _legacy_answer)
    monkeypatch.setattr(module, "_TEST_MARKET_ADAPTER", _legacy_market_answer)
