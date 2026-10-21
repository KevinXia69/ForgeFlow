"""Opt-in controlled replacements for fake Harbor integration tests."""
from pathlib import Path

import pytest


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

