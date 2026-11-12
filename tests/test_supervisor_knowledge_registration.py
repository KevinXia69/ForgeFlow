"""The formal supervisor registers knowledge tools even before first retrieval."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _load_supervisor():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_supervisor.py"
    spec = importlib.util.spec_from_file_location("run_supervisor_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_knowledge_tools_are_registered_for_a_published_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEFLOW_CONTROL_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("FORGEFLOW_KNOWLEDGE_ROOT", str(tmp_path))
    # The supervisor only needs a valid semantic configuration to construct the
    # scoped box; the published generation is resolved on each tool call.
    monkeypatch.setattr(
        "forgeflow.semantic.SemanticConfig.from_env",
        classmethod(lambda cls, source=None: object()))
    module = _load_supervisor()
    box = module._knowledge_toolbox()
    assert box is not None
    assert box.names == frozenset({"knowledge_search", "knowledge_read_source"})
