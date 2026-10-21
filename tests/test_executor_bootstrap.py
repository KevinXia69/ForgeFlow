"""Tests for the executor's strict write-key and store bootstrap checks."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from forgeflow.executor.bootstrap import (
    BootstrapError,
    ensure_private_dir,
    open_store,
    read_write_key,
)

UID = os.geteuid()
KEY = b"0123456789abcdef0123456789abcdef"
SENTINEL = b"do-not-echo-this-secret-material-123456"


def _write_key(path: Path, data: bytes = KEY, mode: int = 0o400) -> Path:
    path.unlink(missing_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _error(code: str, operation: Callable[[], object]) -> BootstrapError:
    with pytest.raises(BootstrapError) as caught:
        operation()
    assert caught.value.code == code
    assert str(caught.value) == code
    return caught.value


def _allow_safe_ancestors(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    boundary: Path,
    expected_uid: int,
) -> None:
    """Fake only the non-root-owned ancestors above ``tmp_path``.

    ``/private/tmp`` is root-owned and world-writable on this macOS host;
    ancestors inside ``tmp_path`` stay real so nested failures are exercised.
    """
    real_lstat = os.lstat
    outside = {
        ancestor for ancestor in target.parents if not ancestor.is_relative_to(boundary)
    }

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        if Path(path) not in outside:
            return real_lstat(path)
        return os.stat_result(
            (stat.S_IFDIR | 0o755, 0, 0, 1, expected_uid, 0, 0, 0, 0, 0)
        )

    monkeypatch.setattr(os, "lstat", lstat)


def test_key_accepts_private_32_byte_file_and_strips_one_newline(tmp_path: Path) -> None:
    path = _write_key(tmp_path / "write-key")
    assert read_write_key(path, expected_uid=UID) == KEY
    _write_key(path, KEY + b"\n")
    assert read_write_key(path, expected_uid=UID) == KEY


def test_key_rejects_extra_newline_and_embedded_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "write-key"
    for data in (KEY + b"\n\n", b"x" * 16 + b" " + b"x" * 15):
        _write_key(path, data)
        _error("key_format_invalid", lambda: read_write_key(path, expected_uid=UID))


@pytest.mark.parametrize("length", [15, 257])
def test_key_rejects_out_of_range_sizes(tmp_path: Path, length: int) -> None:
    path = _write_key(tmp_path / f"write-key-{length}", b"k" * length)
    _error("key_size_invalid", lambda: read_write_key(path, expected_uid=UID))


def test_key_rejects_directory_bad_mode_symlink_and_owner(tmp_path: Path) -> None:
    directory = tmp_path / "key-directory"
    directory.mkdir(mode=0o700)
    _error("key_not_regular", lambda: read_write_key(directory, expected_uid=UID))
    insecure = _write_key(tmp_path / "insecure-key", mode=0o644)
    _error("key_mode_insecure", lambda: read_write_key(insecure, expected_uid=UID))
    target = _write_key(tmp_path / "real-key")
    link = tmp_path / "link-key"
    link.symlink_to(target)
    _error("key_symlinked", lambda: read_write_key(link, expected_uid=UID))
    _error("key_owner_mismatch", lambda: read_write_key(target, expected_uid=UID + 1))


def test_key_errors_never_contain_key_material(tmp_path: Path) -> None:
    path = _write_key(tmp_path / "write-key", SENTINEL + b"\n\n")
    error = _error("key_format_invalid", lambda: read_write_key(path, expected_uid=UID))
    assert SENTINEL.decode() not in str(error)


def test_store_accepts_private_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path.chmod(0o700)
    store = tmp_path / "store"
    store.mkdir(mode=0o700)
    _allow_safe_ancestors(monkeypatch, store, tmp_path, UID)
    assert open_store(store, expected_uid=UID) is None


def test_store_rejects_insecure_mode(tmp_path: Path) -> None:
    store = tmp_path / "store"
    store.mkdir()
    store.chmod(0o755)
    _error("store_mode_insecure", lambda: open_store(store, expected_uid=UID))


def test_store_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "store"
    link.symlink_to(target)
    _error("store_symlinked", lambda: open_store(link, expected_uid=UID))


def test_store_rejects_file_and_relative_path(tmp_path: Path) -> None:
    path = tmp_path / "store"
    path.write_text("not a directory")
    _error("store_not_directory", lambda: open_store(path, expected_uid=UID))
    _error("store_not_found", lambda: open_store(Path("store"), expected_uid=UID))


def test_store_rejects_world_writable_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The offending parent is a nested directory we control, not /private/tmp.
    tmp_path.chmod(0o700)
    parent = tmp_path / "world-writable"
    parent.mkdir()
    parent.chmod(0o777)
    store = parent / "store"
    store.mkdir(mode=0o700)
    _allow_safe_ancestors(monkeypatch, store, tmp_path, UID)
    _error("store_ancestor_insecure", lambda: open_store(store, expected_uid=UID))


def test_ensure_private_dir_creates_0700_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    target = tmp_path / "state"
    _allow_safe_ancestors(monkeypatch, target, tmp_path, UID)
    ensure_private_dir(target, expected_uid=UID)
    info = target.stat()
    assert stat.S_ISDIR(info.st_mode)
    assert info.st_uid == UID
    assert stat.S_IMODE(info.st_mode) == 0o700
    ensure_private_dir(target, expected_uid=UID)
