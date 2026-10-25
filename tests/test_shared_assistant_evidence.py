import json
import stat
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl")

from forgeflow.api.dsh_harness import DshHarness  # noqa: E402
from forgeflow.api.evidence import ReadOnlyEvidenceProjection  # noqa: E402


def projection(tmp_path):
    result = object.__new__(ReadOnlyEvidenceProjection)
    result.settings = SimpleNamespace(approvals_root=tmp_path / "private-cases")
    result.snapshot = lambda _uid: {"scope_id": "a" * 64, "cases": [], "runs": []}
    return result


def test_shared_evidence_and_patch_are_group_readable_without_sharing_cases(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("FORGEFLOW_ASSISTANT_EVIDENCE_ROOT", str(shared))
    snapshot = projection(tmp_path).publish("owner")
    assert snapshot.parent == shared
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o640
    assert not (tmp_path / "private-cases").exists()
    base = tmp_path / "base.json"
    base.write_text(json.dumps([{"insert": [{"id": "mcp-forgeflow",
        "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "forgeflow", "transport": "stdio"}}]}]))
    harness = DshHarness(cli=tmp_path, patch=base, home=tmp_path, workspace=tmp_path)
    scoped = harness.with_evidence(snapshot)
    assert stat.S_IMODE(scoped.patch.stat().st_mode) == 0o640


def test_shared_evidence_root_rejects_symlink(tmp_path, monkeypatch):
    actual, link = tmp_path / "actual", tmp_path / "link"
    actual.mkdir()
    link.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("FORGEFLOW_ASSISTANT_EVIDENCE_ROOT", str(link))
    with pytest.raises(ValueError, match="invalid_evidence_store"):
        projection(tmp_path).publish("owner")
    assert not list(actual.iterdir())


def test_shared_evidence_root_must_be_explicit_existing_absolute_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEFLOW_ASSISTANT_EVIDENCE_ROOT", "relative-scope")
    with pytest.raises(ValueError, match="invalid_evidence_store"):
        projection(tmp_path).publish("owner")
