"""Filesystem preflight for the executor's credential boundary.

Only metadata is inspected here: no key bytes are read, and no sockets or
commands are involved. Failure details are fixed codes (never paths or key
material) so a result can be logged safely.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from forgeflow.executor.bootstrap import BootstrapError, open_store

_LEGACY_STORE = Path("/home/forgeflow/logs/recovery")


@dataclass(frozen=True)
class Check:
    """Result of one preflight invariant."""

    name: str
    ok: bool
    detail: str = ""


def _lstat(path: Path) -> tuple[os.stat_result | None, str]:
    try:
        return os.lstat(path), ""
    except FileNotFoundError:
        return None, "not_found"
    except OSError:
        return None, "unstatable"


def _check_private_path(name: str, path: Path, *, mode: int, directory: bool) -> Check:
    info, detail = _lstat(path)
    if info is None:
        return Check(name, False, detail)
    if stat.S_ISLNK(info.st_mode):
        return Check(name, False, "symlink")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            return Check(name, False, "not_directory")
    elif not stat.S_ISREG(info.st_mode):
        return Check(name, False, "not_regular")
    if info.st_uid != 0:
        return Check(name, False, "owner_mismatch")
    actual = stat.S_IMODE(info.st_mode)
    if actual != mode:
        return Check(name, False, f"mode {actual:04o}")
    return Check(name, True)


def _check_write_key(path: Path, agent_uid: int) -> Check:
    name = "write_key_unreadable"
    info, detail = _lstat(path)
    if info is None:
        return Check(name, False, detail)
    if stat.S_ISLNK(info.st_mode):
        return Check(name, False, "symlink")
    if not stat.S_ISREG(info.st_mode):
        return Check(name, False, "not_regular")
    if info.st_uid != 0:
        return Check(name, False, "owner_mismatch")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o400:
        return Check(name, False, f"mode {mode:04o}")
    # Do not call os.access: it answers for this root executor process, not for
    # the agent. Derive readability from (owner, mode) instead. The agent is not
    # uid 0, is not the owner, and the file grants no group or other read bit.
    if agent_uid == 0 or info.st_uid == agent_uid or mode & 0o044:
        return Check(name, False, "agent_can_read")
    return Check(name, True)


def _check_read_key(path: Path | None) -> Check:
    name = "read_key_usable"
    if path is None:
        return Check(name, False, "missing")
    # Group membership is a deployment invariant: stat exposes the group-read
    # bit (mode 0640), but not the agent's supplementary groups.
    return _check_private_path(name, path, mode=0o640, directory=False)


def _check_store(name: str, path: Path) -> Check:
    # open_store enforces directory, non-symlink, owner 0, mode 0700, and safe
    # ancestors; its stable codes are the right detail for every failure.
    try:
        open_store(path)
    except BootstrapError as exc:
        return Check(name, False, exc.code)
    except OSError:
        return Check(name, False, "unstatable")
    return Check(name, True)


def _check_approval_store(path: Path | None) -> Check:
    name = "approval_store_private"
    if path is None:
        return Check(name, True, "not_configured")
    return _check_store(name, path)


def _check_legacy_absent(path: Path) -> Check:
    name = "legacy_store_absent"
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return Check(name, True)
    except OSError:
        return Check(name, False, "unstatable")
    return Check(name, False, "present")


def run_preflight(
    *,
    agent_uid: int,
    write_key: Path,
    read_key: Path | None,
    odoo_conf: Path,
    entrypoint: Path,
    store: Path,
    run_dir: Path,
    approve_store: Path | None = None,
    legacy_store: Path | None = None,
) -> list[Check]:
    """Run the filesystem boundary checks in their contractual order."""
    legacy = _LEGACY_STORE if legacy_store is None else legacy_store
    return [
        _check_write_key(write_key, agent_uid),
        _check_read_key(read_key),
        _check_private_path("odoo_conf_private", odoo_conf, mode=0o600, directory=False),
        _check_private_path("entrypoint_private", entrypoint, mode=0o700, directory=False),
        _check_store("store_private", store),
        _check_approval_store(approve_store),
        _check_private_path("run_dir_restricted", run_dir, mode=0o711, directory=True),
        _check_legacy_absent(legacy),
    ]


def summarize(checks: list[Check]) -> dict[str, object]:
    """Return a JSON-serializable summary; failed names preserve check order."""
    return {
        "ok": all(check.ok for check in checks),
        "failed": [check.name for check in checks if not check.ok],
        "checks": [
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in checks
        ],
    }
