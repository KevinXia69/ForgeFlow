"""Host-owned per-turn limits for evidence MCP processes.

Only counters are writable by the isolated MCP process. No tool can create,
extend or reopen a lease. A file lock serializes dispatches sharing a snapshot,
including a cancelled SDK request which has not actually finished yet.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path

MAX_TOOL_CALLS = 12
MAX_TOOL_BYTES = 128 * 1024
MAX_RESULT_BYTES = 32 * 1024


def _locked_file(path: Path):
    import fcntl

    if path.is_symlink():
        raise ValueError("evidence_budget_unavailable")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _write(handle, value) -> None:
    handle.seek(0)
    json.dump(value, handle, separators=(",", ":"))
    handle.truncate()
    handle.flush()


class EvidenceTurnBudget:
    def __init__(self, evidence: Path, *, timeout: float):
        self.evidence = evidence
        self.path = evidence.with_suffix(".budget.json")
        self.timeout = timeout
        self.nonce = uuid.uuid4().hex
        self._dispatch = None

    def start(self, *, check_cancelled=lambda: None, queue_timeout: float = 60.0) -> None:
        import fcntl

        lock = self.evidence.with_suffix(".turn.lock")
        if lock.is_symlink():
            raise ValueError("evidence_budget_unavailable")
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(descriptor, "r+")
        deadline = time.monotonic() + queue_timeout
        try:
            while True:
                check_cancelled()
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("turn_queue_timeout") from None
                    time.sleep(0.05)
            check_cancelled()
            self._dispatch = handle
            with _locked_file(self.path) as budget:
                # A setgid shared evidence directory supplies the MCP group.
                os.fchmod(budget.fileno(), 0o660 if os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE_ROOT") else 0o600)
                _write(budget, {"nonce": self.nonce, "active": True,
                                "expires_at": time.time() + self.timeout,
                                "calls": 0, "bytes": 0,
                                "max_calls": MAX_TOOL_CALLS, "max_bytes": MAX_TOOL_BYTES})
        except BaseException:
            self._dispatch = None
            handle.close()
            raise

    def cancel(self) -> None:
        """Close permission to query without releasing an in-flight dispatch."""
        if self._dispatch is None:
            return
        try:
            with _locked_file(self.path) as handle:
                value = json.load(handle)
                if value.get("nonce") == self.nonce:
                    value["active"] = False
                    _write(handle, value)
        except (OSError, ValueError):
            # A missing/bad lease already makes MCP fail closed.
            pass

    def close(self) -> None:
        self.cancel()
        if self._dispatch is not None:
            self._dispatch.close()
            self._dispatch = None


@contextlib.contextmanager
def turn_budget(evidence: Path | None, *, timeout: float, check_cancelled=lambda: None,
                queue_timeout: float = 60.0):
    lease = EvidenceTurnBudget(evidence, timeout=timeout) if evidence is not None else None
    try:
        if lease is not None:
            lease.start(check_cancelled=check_cancelled, queue_timeout=queue_timeout)
        yield lease
    finally:
        if lease is not None:
            lease.close()
