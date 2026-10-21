"""Strict filesystem bootstrap checks for the root-side executor.

These checks fail closed with fixed error codes. Key material is never stored
on the exception, formatted into its text, or attached as a cause.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


class BootstrapError(RuntimeError):
    """A bootstrap invariant was violated; the executor must not continue."""

    code: str

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _has_whitespace(value: bytes) -> bool:
    return any(chr(byte).isspace() for byte in value)


def read_write_key(
    path: str | os.PathLike[str],
    *,
    expected_uid: int = 0,
    max_bytes: int = 256,
) -> bytes:
    """Read the Odoo write key after validating its file identity and shape."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        code = "key_symlinked" if exc.errno == errno.ELOOP else "key_not_found"
        raise BootstrapError(code) from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BootstrapError("key_not_regular")
        if info.st_uid != expected_uid:
            raise BootstrapError("key_owner_mismatch")
        if stat.S_IMODE(info.st_mode) != 0o400:
            raise BootstrapError("key_mode_insecure")
        if not 16 <= info.st_size <= max_bytes:
            raise BootstrapError("key_size_invalid")
        raw = os.read(fd, info.st_size + 1)
    except OSError:
        raise BootstrapError("key_not_found") from None
    finally:
        os.close(fd)

    if len(raw) != info.st_size:
        raise BootstrapError("key_size_invalid")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if _has_whitespace(raw):
        raise BootstrapError("key_format_invalid")
    if not 16 <= len(raw) <= max_bytes:
        raise BootstrapError("key_size_invalid")
    return raw


def open_store(
    path: str | os.PathLike[str],
    *,
    expected_uid: int = 0,
) -> None:
    """Validate an existing private state directory and all of its ancestors."""
    target = Path(path)
    if not target.is_absolute():
        raise BootstrapError("store_not_found")
    try:
        info = os.lstat(target)
    except OSError:
        raise BootstrapError("store_not_found") from None
    if stat.S_ISLNK(info.st_mode):
        raise BootstrapError("store_symlinked")
    if not stat.S_ISDIR(info.st_mode):
        raise BootstrapError("store_not_directory")
    if info.st_uid != expected_uid:
        raise BootstrapError("store_owner_mismatch")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise BootstrapError("store_mode_insecure")
    for ancestor in target.parents:
        try:
            ancestor_info = os.lstat(ancestor)
        except OSError:
            raise BootstrapError("store_ancestor_insecure") from None
        if ancestor_info.st_uid != expected_uid:
            raise BootstrapError("store_ancestor_insecure")
        if stat.S_IMODE(ancestor_info.st_mode) & 0o022:
            raise BootstrapError("store_ancestor_insecure")


def ensure_private_dir(
    path: str | os.PathLike[str],
    mode: int = 0o700,
    expected_uid: int = 0,
) -> None:
    """Create the directory if needed, then require the full open_store checks."""
    target = Path(path)
    if not target.is_absolute():
        raise BootstrapError("store_not_found")
    try:
        os.mkdir(target, mode)
    except FileExistsError:
        pass
    except OSError:
        raise BootstrapError("store_not_found") from None
    open_store(target, expected_uid=expected_uid)
