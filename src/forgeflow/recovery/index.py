"""Stable prepare keys and the durable key-to-plan index.

Wall-clock collection timestamps are excluded so retrying the same live state
returns the original plan instead of creating a duplicate.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 64 * 1024
_TIMESTAMP = "collected_at_utc"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_SHA256 = re.compile(r"[a-f0-9]{64}")


class IndexConflict(RuntimeError):
    """A prepare key is already bound to a different plan."""


def _canonical(value: object) -> bytes:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return (encoded + "\n").encode("utf-8")


def _without_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_timestamps(v) for k, v in value.items() if k != _TIMESTAMP}
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def _sorted_models(snapshot: dict) -> dict:
    if "models" not in snapshot:
        return snapshot
    models = snapshot["models"]
    if not isinstance(models, dict):
        raise ValueError("snapshot models must be an object")
    for rows in models.values():
        if not isinstance(rows, list):
            raise ValueError("snapshot model rows must be a list")
        for row in rows:
            row_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(row_id, bool) or not isinstance(row_id, int):
                raise ValueError("snapshot row requires an integer id")
        rows.sort(key=lambda row: row["id"])
    return snapshot


def business_snapshot_digest(snapshot: dict) -> str:
    """Return the canonical digest of business state, ignoring collection time."""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    cleaned = _without_timestamps(snapshot)
    return hashlib.sha256(_canonical(_sorted_models(cleaned))).hexdigest()


def prepare_key(
    *,
    run_id: str,
    request: dict,
    snapshot: dict,
    sources_sha256: str,
    policy_sha256: str,
) -> str:
    """Return the stable key for one preparation attempt."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run_id")
    digests = {"sources_sha256": sources_sha256, "policy_sha256": policy_sha256}
    for name, value in digests.items():
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"invalid {name}")
    binding = {
        "run_id": run_id,
        "request": request,
        "snapshot": business_snapshot_digest(snapshot),
        "sources": sources_sha256,
        "policy": policy_sha256,
    }
    return hashlib.sha256(_canonical(binding)).hexdigest()


class PrepareIndex:
    """A crash-safe, process-safe mapping from prepare key to plan id."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        created = not self.root.exists()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(self.root.lstat().st_mode):
            raise ValueError("index root must be a directory")
        if created:
            os.chmod(self.root, 0o700)

    def _path(self, key: str) -> Path:
        unsafe = (not isinstance(key, str) or not key or key in {".", ".."}
                  or "/" in key or "\\" in key or "\x00" in key)
        if unsafe:
            raise ValueError("invalid index key")
        return self.root / f"{key}.json"

    @contextmanager
    def _locked(self) -> Iterator[int]:
        # Locking the directory descriptor serializes processes and threads
        # without leaving a lock file that could be mistaken for an entry.
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)

    def get(self, key: str) -> str | None:
        """Return the indexed plan id, or None for any unusable entry."""
        try:
            fd = os.open(self._path(key), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (OSError, ValueError):
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INDEX_BYTES:
                    return None
                raw = handle.read(MAX_INDEX_BYTES + 1)
            if len(raw) > MAX_INDEX_BYTES:
                return None
            payload = json.loads(raw)
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            return None
        if payload.get("key") != key:
            return None
        plan_id = payload.get("plan_id")
        return plan_id if isinstance(plan_id, str) else None

    def put(self, key: str, plan_id: str) -> None:
        """Atomically bind key to plan_id, or raise IndexConflict."""
        path = self._path(key)
        if not isinstance(plan_id, str):
            raise ValueError("plan_id must be a string")
        with self._locked() as directory_fd:
            existing = self.get(key)
            if existing == plan_id:
                return
            if existing is not None:
                raise IndexConflict("index key already maps to a different plan")
            payload = {"schema_version": SCHEMA_VERSION, "key": key, "plan_id": plan_id}
            temporary = self.root / f".{key}.{uuid.uuid4().hex}.tmp"
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    handle.write(_canonical(payload))
                    handle.flush()
                    os.fsync(fd)
                os.replace(temporary, path)
                os.fsync(directory_fd)
            finally:
                os.close(fd)
                temporary.unlink(missing_ok=True)
