"""Tests for the executor filesystem preflight checks."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgeflow.executor.preflight import Check, run_preflight, summarize

AGENT_UID = os.geteuid() + 1
NAMES = (
    "write_key_unreadable", "read_key_usable", "odoo_conf_private", "entrypoint_private",
    "store_private", "approval_store_private", "run_dir_restricted", "legacy_store_absent",
)


def _file(path: Path, mode: int, data: bytes = b"x") -> Path:
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _dir(path: Path, mode: int) -> Path:
    path.mkdir()
    path.chmod(mode)
    return path


def _tree(tmp_path: Path) -> dict[str, Path]:
    tmp_path.chmod(0o700)
    return {
        "write_key": _file(tmp_path / "write.key", 0o400, b"s" * 32),
        "read_key": _file(tmp_path / "read.key", 0o640),
        "odoo_conf": _file(tmp_path / "odoo.conf", 0o600),
        "entrypoint": _file(tmp_path / "entrypoint", 0o700),
        "store": _dir(tmp_path / "store", 0o700),
        "approve_store": _dir(tmp_path / "approve", 0o700),
        "run_dir": _dir(tmp_path / "run", 0o711),
        "legacy_store": tmp_path / "legacy" / "recovery",
    }


def _root_owned(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Report uid 0 under ``root`` and for its ancestors; delegate all else."""
    real_lstat = os.lstat
    ancestors = set(root.parents)

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        info = real_lstat(path)
        resolved = Path(path)
        if resolved.is_relative_to(root):
            fields = list(info)
            fields[4] = 0
            return os.stat_result(fields)
        if resolved in ancestors:
            return os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        return info

    monkeypatch.setattr(os, "lstat", lstat)


def _run(tree: dict[str, Path], **overrides: object) -> list[Check]:
    kwargs: dict[str, Any] = {
        "agent_uid": AGENT_UID, "write_key": tree["write_key"],
        "read_key": tree["read_key"], "odoo_conf": tree["odoo_conf"],
        "entrypoint": tree["entrypoint"], "store": tree["store"],
        "run_dir": tree["run_dir"], "approve_store": tree["approve_store"],
        "legacy_store": tree["legacy_store"],
    }
    kwargs.update(overrides)
    return run_preflight(**kwargs)


def _failed(checks: list[Check]) -> list[str]:
    return [check.name for check in checks if not check.ok]


def _assert_details_clean(checks: list[Check], tmp_path: Path) -> None:
    for check in checks:
        assert str(tmp_path) not in check.detail
        assert "/" not in check.detail


def _symlinked_key(tree: dict[str, Path]) -> None:
    target = _file(tree["write_key"].parent / "real.key", 0o400)
    tree["write_key"].unlink()
    tree["write_key"].symlink_to(target)


def _present_legacy_store(tree: dict[str, Path]) -> None:
    tree["legacy_store"].mkdir(parents=True)
    tree["legacy_store"].chmod(0o700)


@pytest.mark.parametrize(
    ("mutate", "expected", "overrides"),
    [
        (lambda t: t["write_key"].chmod(0o644), "write_key_unreadable", {}),
        (_symlinked_key, "write_key_unreadable", {}),
        (lambda t: t["store"].chmod(0o755), "store_private", {}),
        (lambda t: t["store"].rmdir(), "store_private", {}),
        (lambda t: t["run_dir"].chmod(0o777), "run_dir_restricted", {}),
        (_present_legacy_store, "legacy_store_absent", {}),
        (lambda _t: None, "read_key_usable", {"read_key": None}),
    ],
    ids=[
        "write-key-mode", "write-key-symlink", "store-mode", "store-missing",
        "run-dir-mode", "legacy-store-present", "read-key-missing",
    ],
)
def test_failure_flips_exactly_one_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, Path]], None],
    expected: str,
    overrides: dict[str, Any],
) -> None:
    tree = _tree(tmp_path)
    mutate(tree)
    _root_owned(monkeypatch, tmp_path)
    checks = _run(tree, **overrides)
    assert _failed(checks) == [expected]
    _assert_details_clean(checks, tmp_path)


def test_all_good_checks_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = _tree(tmp_path)
    _root_owned(monkeypatch, tmp_path)
    checks = _run(tree)
    assert [check.name for check in checks] == list(NAMES)
    assert all(check.ok for check in checks)
    expected = {
        "ok": True,
        "failed": [],
        "checks": [{"name": name, "ok": True, "detail": ""} for name in NAMES],
    }
    assert summarize(checks) == expected
    _assert_details_clean(checks, tmp_path)


def test_summarize_reports_failed_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(tmp_path)
    tree["legacy_store"].mkdir(parents=True)
    _root_owned(monkeypatch, tmp_path)
    summary = summarize(_run(tree, read_key=None))
    assert summary["ok"] is False
    assert summary["failed"] == ["read_key_usable", "legacy_store_absent"]
    entries = summary["checks"]
    assert [entry["name"] for entry in entries] == list(NAMES)


def test_details_never_echo_key_bytes_or_temp_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(tmp_path)
    sentinel = b"do-not-echo-this-secret-key-material"
    tree["write_key"].chmod(0o600)
    tree["write_key"].write_bytes(sentinel)
    tree["store"].chmod(0o777)
    tree["legacy_store"].mkdir(parents=True)
    _root_owned(monkeypatch, tmp_path)
    checks = _run(tree, read_key=None)
    _assert_details_clean(checks, tmp_path)
    assert all(sentinel.decode() not in check.detail for check in checks)


def test_missing_paths_never_raise(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    checks = run_preflight(
        agent_uid=AGENT_UID,
        write_key=missing / "write-key",
        read_key=None,
        odoo_conf=missing / "odoo.conf",
        entrypoint=missing / "entrypoint",
        store=missing / "store",
        run_dir=missing / "run",
        approve_store=None,
        legacy_store=tmp_path / "legacy",
    )
    assert [check.name for check in checks] == list(NAMES)
    assert _failed(checks) == [
        "write_key_unreadable",
        "read_key_usable",
        "odoo_conf_private",
        "entrypoint_private",
        "store_private",
        "run_dir_restricted",
    ]
    assert checks[1].detail == "missing"
    assert checks[5].ok and checks[5].detail == "not_configured"
    assert checks[7].ok
    _assert_details_clean(checks, tmp_path)
