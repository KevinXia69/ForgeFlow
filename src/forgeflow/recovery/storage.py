"""Durable local recovery artifacts and one execution lock per ERP sandbox.

These detect corruption and concurrent tool invocations; they are not an
authentication boundary against a process with the same filesystem privileges.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path


class StorageError(RuntimeError):
    pass


def encoded(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n").encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path: Path, limit=16 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise StorageError("invalid_artifact")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise StorageError("artifact_too_large")
        return json.loads(raw)
    finally:
        os.close(fd)


def atomic_json(path: Path, value, *, replace=False):
    temporary = path.with_name(".temporary-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(encoded(value))
            handle.flush()
            os.fsync(fd)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)
    sync_directory(path.parent)


@contextmanager
def scope_lock(root: Path):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise StorageError("invalid_store_directory")
    sync_directory(root.parent)
    fd = os.open(root / "scope.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StorageError("invalid_scope_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageError("another_recovery_operation_running") from None
        yield
    finally:
        os.close(fd)
