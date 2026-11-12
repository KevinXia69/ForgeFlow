"""Submission packages contain tracked files, exclude secrets and verify hashes."""
from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.build_submission import SubmissionError, build, verify


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "README.md").write_text("ForgeFlow\n", encoding="utf-8")
    (repo / "docs/status.md").write_text("status\n", encoding="utf-8")
    (repo / ".env").write_text("DEEPSEEK_API_KEY=secret\n", encoding="utf-8")
    (repo / "server.pem").write_text("secret\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-f", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_submission_excludes_secrets_and_verifies(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = tmp_path / "submission.zip"
    result = build(repo, out)
    assert result["manifest"]["commit"]
    assert verify(out) == []
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert "README.md" in names and "docs/status.md" in names
        assert ".env" not in names and "server.pem" not in names
        manifest = json.loads(archive.read("SUBMISSION_MANIFEST.json"))
        assert {item["path"] for item in manifest["files"]} == {
            "README.md", "docs/status.md"}


def test_a_dirty_tree_cannot_be_packaged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(SubmissionError, match="dirty_worktree"):
        build(repo, tmp_path / "submission.zip")


def test_a_tampered_zip_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = tmp_path / "submission.zip"
    build(repo, out)
    with zipfile.ZipFile(out, "a") as archive:
        archive.writestr("README.md", "tampered\n")
    assert "sha256:README.md" in verify(out)


def test_release_manifest_path_is_relative_when_inside_the_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    release = repo / "release.json"
    release.write_text('{"commit": "x"}\n', encoding="utf-8")
    subprocess.run(["git", "add", "release.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "release"], cwd=repo, check=True)
    result = build(repo, tmp_path / "submission.zip", release_manifest=release)
    assert result["manifest"]["release_manifest"]["path"] == "release.json"


def test_submission_commit_is_read_from_the_manifest(tmp_path: Path) -> None:
    from scripts.build_submission import submission_commit

    repo = _repo(tmp_path)
    out = tmp_path / "submission.zip"
    result = build(repo, out)
    assert submission_commit(out) == result["manifest"]["commit"]


def _manifest_zip(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("SUBMISSION_MANIFEST.json", json.dumps(payload))
    return path


def test_an_empty_submission_is_not_accepted(tmp_path: Path) -> None:
    path = _manifest_zip(tmp_path, {"schema": 1, "commit": "", "files": []})
    errors = verify(path)
    assert "manifest_commit_missing" in errors
    assert "manifest_files_empty" in errors


def test_a_manifest_with_duplicate_paths_is_rejected(tmp_path: Path) -> None:
    item = {"path": "README.md", "sha256": "a" * 64, "size": 1}
    path = _manifest_zip(tmp_path, {
        "schema": 1, "commit": "b" * 40, "files": [item, dict(item)]})
    assert "manifest_path_duplicated:README.md" in verify(path)
