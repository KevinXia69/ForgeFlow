"""Production preflight blocks while any production-blocking gap is open."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_preflight import evaluate, load_gaps


def _gaps(tmp_path: Path, items: list[dict]) -> Path:
    path = tmp_path / "gaps.json"
    path.write_text(json.dumps({"schema": 1, "items": items}), encoding="utf-8")
    return path


def test_an_open_blocking_gap_blocks_production_but_not_local(tmp_path: Path) -> None:
    items = load_gaps(_gaps(tmp_path, [
        {"id": "https", "title": "HTTPS", "status": "open", "blocks_production": True},
        {"id": "docs", "title": "Docs", "status": "done", "blocks_production": False},
    ]))
    production = evaluate("production", items)
    assert production["ok"] is False
    assert production["blocking_gaps"] == ["https"]
    assert evaluate("local", items)["ok"] is True


def test_done_gaps_and_clean_artifacts_pass_production(tmp_path: Path) -> None:
    items = load_gaps(_gaps(tmp_path, [
        {"id": "https", "title": "HTTPS", "status": "done", "blocks_production": True}]))
    result = evaluate("production", items, manifest_errors=[], submission_errors=[])
    assert result["ok"] is True and result["blocking_gaps"] == []


def test_artifact_or_worktree_errors_fail_local_too(tmp_path: Path) -> None:
    items = load_gaps(_gaps(tmp_path, [
        {"id": "x", "title": "X", "status": "open", "blocks_production": True}]))
    result = evaluate("local", items, manifest_errors=["artifact_sha256_mismatch"],
                      dirty=True)
    assert result["ok"] is False
    assert set(result["errors"]) == {"artifact_sha256_mismatch", "dirty_worktree"}


def test_a_broken_gap_register_is_named(tmp_path: Path) -> None:
    path = tmp_path / "gaps.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="gap_register_unreadable"):
        load_gaps(path)


def test_production_requires_a_release_manifest_and_artifact(tmp_path: Path) -> None:
    items = load_gaps(_gaps(tmp_path, [
        {"id": "x", "title": "X", "status": "done", "blocks_production": True}]))
    result = evaluate("production", items, artifacts_present=False)
    assert result["ok"] is False
    assert "release_artifacts_required" in result["errors"]
    assert evaluate("local", items, artifacts_present=False)["ok"] is True


def test_safety_gap_cannot_be_waived_by_a_feature_exemption() -> None:
    items = [{"id": "native_rpc", "title": "Native RPC authorization",
              "status": "waived", "blocks_production": False, "safety_critical": True}]
    assert evaluate("production", items)["blocking_gaps"] == ["native_rpc"]
    assert evaluate("local", items)["ok"] is True


def test_dirty_override_is_rejected_for_production():
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts/release_preflight.py"
    result = subprocess.run([sys.executable, str(script), "--environment", "production",
                             "--allow-dirty"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "allow_dirty_is_local_only" in result.stdout


def test_a_manifest_without_its_artifact_is_rejected(tmp_path: Path) -> None:
    import subprocess
    import sys

    from forgeflow.api.settings import create_settings  # noqa: F401  (import side effect only)

    script = Path(__file__).resolve().parents[1] / "scripts/release_preflight.py"
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"schema": 1}', encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), "--environment", "local",
                           "--manifest", str(manifest), "--allow-dirty"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    assert "manifest_artifact_pair_required" in proc.stdout


def test_a_submission_without_a_version_is_rejected() -> None:
    from scripts.release_preflight import commit_consistency_errors

    assert commit_consistency_errors("a" * 40, "a" * 40) == []
    assert commit_consistency_errors(None, "a" * 40) == [
        "release_manifest_commit_missing"]
    assert commit_consistency_errors("a" * 40, None) == ["submission_commit_missing"]
    assert commit_consistency_errors("a" * 40, "b" * 40) == [
        "submission_commit_mismatch"]
