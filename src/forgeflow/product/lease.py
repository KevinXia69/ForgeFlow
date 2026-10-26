"""Persistent ownership of one hosted ERP generation.

There is deliberately no timeout or HTTP release operation. An operator must
stop the API, initialize a new ERP generation, then explicitly reset the lease.
Old cases remain readable but cannot be dispatched against the new baseline.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

GENERATION = re.compile(r"[0-9a-f]{32}")
OWNER = re.compile(r"[0-9a-f]{64}")
FILENAME = ".environment-lease.json"
MAX_BYTES = 32 * 1024
MAX_CASES = 256


class EnvironmentLeaseError(RuntimeError):
    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class EnvironmentLease:
    def __init__(self, root: Path, generation: str) -> None:
        if not isinstance(generation, str) or GENERATION.fullmatch(generation) is None:
            raise ValueError("invalid_environment_generation")
        self.root, self.generation = Path(root), generation
        self.path = self.root / FILENAME

    @staticmethod
    def _owner(uid: str) -> str:
        if not isinstance(uid, str) or not uid or len(uid) > 200:
            raise EnvironmentLeaseError("invalid_owner", 400)
        return hashlib.sha256(uid.encode()).hexdigest()

    @contextmanager
    def _locked(self):
        fd = None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise EnvironmentLeaseError("environment_lease_unavailable", 503)
            fd = os.open(self.root / ".environment-lease.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EnvironmentLeaseError("environment_lease_unavailable", 503)
            if os.geteuid() == 0:
                directory = self.root.stat()
                os.fchown(fd, directory.st_uid, directory.st_gid)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except OSError:
            raise EnvironmentLeaseError("environment_lease_unavailable", 503) from None
        finally:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read(self) -> dict | None:
        if self.root.is_symlink():
            raise EnvironmentLeaseError("environment_lease_unavailable", 503)
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise EnvironmentLeaseError("environment_lease_unavailable", 503) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise EnvironmentLeaseError("environment_lease_unavailable", 503)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError
            record = json.loads(raw)
            if (not isinstance(record, dict) or set(record) != {
                "version", "generation", "owner_sha256", "case_ids", "claimed_at",
            } or type(record["version"]) is not int or record["version"] != 1
                    or not isinstance(record["generation"], str)
                    or GENERATION.fullmatch(record["generation"]) is None):
                raise ValueError
            owner, cases, claimed = record["owner_sha256"], record["case_ids"], record["claimed_at"]
            if (not isinstance(cases, list) or len(cases) > MAX_CASES
                    or not all(isinstance(case, str) and GENERATION.fullmatch(case) for case in cases)
                    or len(set(cases)) != len(cases)):
                raise ValueError
            if owner is None:
                if cases or claimed is not None:
                    raise ValueError
            elif (not isinstance(owner, str) or OWNER.fullmatch(owner) is None
                  or not cases or not isinstance(claimed, str) or len(claimed) > 64):
                raise ValueError
            return record
        except (OSError, ValueError, TypeError, KeyError):
            raise EnvironmentLeaseError("environment_lease_unavailable", 503) from None
        finally:
            os.close(fd)

    def _empty(self) -> dict:
        return {"version": 1, "generation": self.generation, "owner_sha256": None,
                "case_ids": [], "claimed_at": None}

    def _write(self, record: dict) -> None:
        temporary = self.root / (".environment-lease-" + secrets.token_hex(8) + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            if os.geteuid() == 0:
                directory = self.root.stat()
                os.fchown(fd, directory.st_uid, directory.st_gid)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
                json.dump(record, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(fd)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.close(fd)
            temporary.unlink(missing_ok=True)

    def claim(self, uid: str, case_id: str) -> None:
        owner = self._owner(uid)
        if not isinstance(case_id, str) or GENERATION.fullmatch(case_id) is None:
            raise EnvironmentLeaseError("invalid_case_id", 400)
        with self._locked():
            record = self._read() or self._empty()
            if record["generation"] != self.generation:
                raise EnvironmentLeaseError("environment_generation_mismatch")
            if record["owner_sha256"] not in (None, owner):
                raise EnvironmentLeaseError("environment_busy")
            if case_id in record["case_ids"]:
                return
            if len(record["case_ids"]) >= MAX_CASES:
                raise EnvironmentLeaseError("environment_case_limit")
            record["owner_sha256"] = owner
            record["claimed_at"] = record["claimed_at"] or datetime.now(UTC).isoformat()
            record["case_ids"].append(case_id)
            self._write(record)

    def require(self, uid: str, case_id: str) -> None:
        record = self._read()
        if record is None or record["generation"] != self.generation:
            raise EnvironmentLeaseError("environment_generation_mismatch")
        if case_id not in record["case_ids"]:
            raise EnvironmentLeaseError("environment_generation_mismatch")
        if record["owner_sha256"] != self._owner(uid):
            raise EnvironmentLeaseError("environment_busy")

    def status(self, uid: str) -> dict:
        record = self._read() or self._empty()
        current = record["generation"] == self.generation
        owned = current and record["owner_sha256"] == self._owner(uid)
        available = current and (record["owner_sha256"] is None or owned)
        return {"enforced": True, "generation": self.generation,
                "available": available, "owned_by_me": owned,
                "requires_admin_reset": not available,
                "reason": None if available else "environment_generation_mismatch"
                if not current else "environment_busy"}

    def initialize_empty(self, *, previous_generation: str | None = None) -> None:
        """Offline operator operation; never called by the HTTP application.

        The root-only CLI is the supported reset interface. The API must be
        stopped and the new ERP baseline ready before invoking it. Files keep
        the case directory's ownership so the unprivileged API can use them.
        """
        with self._locked():
            record = self._read()
            if record is not None:
                if record["generation"] != previous_generation:
                    raise EnvironmentLeaseError("environment_generation_mismatch")
                if record["generation"] == self.generation:
                    raise EnvironmentLeaseError("new_environment_generation_required")
            elif previous_generation is not None:
                raise EnvironmentLeaseError("environment_generation_mismatch")
            self._write(self._empty())

    def stored_generation(self) -> str | None:
        """Operator recovery metadata, without revealing the owner or case IDs."""
        record = self._read()
        return None if record is None else record["generation"]

    def is_unclaimed(self) -> bool:
        record = self._read()
        return record is not None and record["owner_sha256"] is None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Existing API-owned product-cases directory")
    parser.add_argument("--generation", required=True, help="Already initialized new ERP generation")
    parser.add_argument("--previous-generation")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("lease reset requires root")
    if not args.root.is_dir() or args.root.is_symlink():
        parser.error("root must be an existing product-cases directory")
    try:
        EnvironmentLease(args.root, args.generation).initialize_empty(
            previous_generation=args.previous_generation,
        )
    except (EnvironmentLeaseError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"generation": args.generation, "initialized": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
