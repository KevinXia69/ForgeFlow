"""Local-only admission barrier for coordinated Compose stop/reconfiguration.

API ingress must be stopped first. Existing dispatcher and parse work continues
while this barrier pauses only source/outbox admission; their durable queues and
operator subscription settings are never rewritten.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class AdmissionError(ValueError):
    pass


def _paths(config: dict) -> tuple[Path, Path]:
    root = Path(config["env"]["FORGEFLOW_APPROVALS_ROOT"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise AdmissionError("local_admission_path_invalid")
    return root / "local-admission.lock", root / "local-admission-paused.json"


@contextmanager
def _lock(config: dict, *, exclusive: bool = False, timeout: float = 60):
    lock, _ = _paths(config)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(fd, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AdmissionError("local_admission_busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _paused(config: dict) -> bool:
    _, marker = _paths(config)
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = json.loads(handle.read(4097))
        if value != {"schema_version": 1, "installation_id": config["installation_id"]}:
            raise ValueError
    except (ValueError, OSError, TypeError):
        raise AdmissionError("local_admission_marker_invalid") from None
    return True


def paused(config: dict) -> bool:
    with _lock(config):
        return _paused(config)


def quiesce(config: dict, *, timeout: float = 60) -> dict:
    with _lock(config, exclusive=True, timeout=timeout):
        if not _paused(config):
            _, marker = _paths(config)
            fd, temporary = tempfile.mkstemp(prefix=".local-admission-", dir=marker.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"schema_version": 1, "installation_id": config["installation_id"]}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, marker)
            finally:
                Path(temporary).unlink(missing_ok=True)
    return {"ok": True, "admission_paused": True,
            "in_flight_admission_complete": True, "dispatcher_and_parser_drained": False}


def resume(config: dict) -> dict:
    with _lock(config, exclusive=True):
        if _paused(config):
            _paths(config)[1].unlink()
    return {"ok": True, "admission_paused": False}


class AdmissionGate:
    def __init__(self, config: dict, name: str, runnable):
        if name not in {"source", "outbox"} or not callable(getattr(runnable, "tick", None)):
            raise AdmissionError("local_admission_runnable_invalid")
        self.config, self.name, self.runnable = config, name, runnable

    def run(self, stop_event) -> int:
        ticks = 0
        while not stop_event.is_set():
            with _lock(self.config):
                if not _paused(self.config):
                    self.runnable.tick()
                    ticks += 1
                else:
                    from forgeflow.superagent.heartbeat import beat

                    beat(self.runnable.store,
                         service_id=f"{self.name}:{self.runnable.ctx.tenant_id}",
                         tenant_id=self.runnable.ctx.tenant_id, kind=self.name,
                         detail={"local_admission_paused": True})
            stop_event.wait(self.runnable.tick_seconds)
        return ticks
