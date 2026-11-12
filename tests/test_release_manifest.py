"""Release manifest pins the commit, lock, addon, migrations and artifact."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.release_manifest import ReleaseError, build, verify


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "odoo-addons/forgeflow_general").mkdir(parents=True)
    (repo / "migrations/control").mkdir(parents=True)
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "odoo-addons/forgeflow_general/__manifest__.py").write_text(
        "{'name': 'ForgeFlow', 'version': '20.0.2.0.0'}\n", encoding="utf-8")
    (repo / "migrations/control/001_base.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (repo / "migrations/control/002_more.sql").write_text("SELECT 2;\n", encoding="utf-8")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_build_and_verify_a_clean_checkout(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manifest_path, artifact = build(repo, tmp_path / "dist")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["addon_version"] == "20.0.2.0.0"
    assert manifest["migration_versions"] == [1, 2]
    assert manifest["artifact"]["sha256"]
    assert verify(repo, manifest_path, artifact) == []


def test_a_tampered_artifact_or_source_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manifest_path, artifact = build(repo, tmp_path / "dist")
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    assert "artifact_sha256_mismatch" in verify(repo, manifest_path, artifact)

    manifest_path, artifact = build(repo, tmp_path / "dist2")
    (repo / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    assert "uv_lock_mismatch" in verify(repo, manifest_path, artifact)


def test_a_dirty_checkout_cannot_be_built(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="dirty_worktree"):
        build(repo, tmp_path / "dist")
