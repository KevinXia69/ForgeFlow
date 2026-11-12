"""Scheduled backups: retention, manifests, encryption and readability."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from scripts.backup_control_db import BackupError, backup, prune_backups, verify

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not DATABASE_URL,
                                 reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _named(path: Path, when: float, suffix: str = ".dump") -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when))
    return path / f"control-{stamp}{suffix}"


def test_prune_removes_only_old_control_backups(tmp_path: Path) -> None:
    now = time.time()
    old = _named(tmp_path, now - 20 * 86400)
    old.write_text("old", encoding="utf-8")
    (tmp_path / (old.name[:-5] + ".manifest.json")).write_text("{}", encoding="utf-8")
    recent = _named(tmp_path, now - 1 * 86400)
    recent.write_text("new", encoding="utf-8")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")
    removed = prune_backups(tmp_path, retain_days=7, now=now)
    assert old.name in removed
    assert not old.exists()
    assert recent.exists() and unrelated.exists()


def test_a_negative_retention_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        prune_backups(tmp_path, retain_days=-1)


@requires_db
def test_a_backup_is_sha256_checked_and_readable(tmp_path: Path) -> None:
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        pytest.skip("PostgreSQL client tools not installed")
    result = backup(out_dir=tmp_path, retain_days=7, database_url=DATABASE_URL)
    file = Path(result["backup"])
    assert file.is_file() and result["bytes"] > 0
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["sha256"] == result["sha256"]
    checked = verify(file=file)
    assert checked["ok"] is True and checked["sha256"] == manifest["sha256"]


@requires_db
def test_an_encrypted_backup_needs_the_key(tmp_path: Path) -> None:
    if shutil.which("pg_dump") is None or shutil.which("openssl") is None:
        pytest.skip("PostgreSQL client tools or openssl not installed")
    key = tmp_path / "backup.key"
    key.write_text("test-only-backup-key", encoding="utf-8")
    key.chmod(0o600)
    result = backup(out_dir=tmp_path / "backups", retain_days=7,
                    database_url=DATABASE_URL, encrypt_key=key)
    assert result["encrypted"] is True and result["backup"].endswith(".enc")
    with pytest.raises(BackupError) as missing:
        verify(file=Path(result["backup"]))
    assert missing.value.code == "decryption_key_required"
    assert verify(file=Path(result["backup"]), encrypt_key=key)["ok"] is True


def test_an_insecure_key_file_is_refused(tmp_path: Path) -> None:
    from scripts.backup_control_db import _encrypt

    key = tmp_path / "backup.key"
    key.write_text("k", encoding="utf-8")
    key.chmod(0o644)
    with pytest.raises(BackupError) as insecure:
        _encrypt(tmp_path / "missing.dump", tmp_path / "out.enc", key)
    assert insecure.value.code == "encryption_key_missing" or (
        insecure.value.code == "encryption_key_permissions")
