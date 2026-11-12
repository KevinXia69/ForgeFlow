"""API-created runs: ownership, idempotency and a single-active queue.

The API never talks to Odoo. It records who asked for which whitelisted
scenario, and (only when the service operator enables it) starts the existing
CLI runner in a detached process. Recorded runs created outside the API keep
no owner and stay visible to every signed-in reviewer.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..runner import TASKS

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
IDEMPOTENCY = re.compile(r"[A-Za-z0-9._:-]{8,128}")


class RunUnavailable(Exception):
    """The request is invalid, disabled, or conflicts with current state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def default_command(run_id: str, scenario: str, data_root: str | Path | None = None) -> list[str]:
    """The one product-mode command; scenario names are whitelisted upstream."""
    command = [
        sys.executable, "-m", "forgeflow.cli", "run", scenario,
        "--agent", "pi",
        "--security-profile", "permission-split-v1",
        "--planner-profile", "cp-sat-recovery-v1",
        "--data-profile", "planning-metadata-v1",
        "--run-id", run_id,
    ]
    if data_root is not None:
        command.extend(["--data-root", str(data_root)])
    return command


class RunService:
    """Thread-safe registry; the worker thread runs at most one trial."""

    def __init__(
        self,
        data_root: str | Path,
        state_path: str | Path,
        *,
        spawn: Callable[[list[str], str], Any] | None = None,
        enabled: bool = False,
    ) -> None:
        self.data_root = Path(data_root)
        self.state_path = Path(state_path)
        self.spawn = spawn
        self.enabled = enabled
        self._lock = threading.Lock()
        self._host = socket.gethostname()
        self._state_error: RunUnavailable | None = None
        promoted = None
        with self._file_lock():
            try:
                self._state = self._load()
            except RunUnavailable as error:
                # Preserve the unreadable file; no empty registry is written.
                self._state = None
                self._state_error = error
            else:
                # Only a confirmed-dead local worker is interrupted. An
                # unconfirmed worker keeps the slot busy.
                for record in self._state["runs"].values():
                    if record.get("state") == "running" and not self._worker_alive(record):
                        record["state"] = "interrupted"
                if self.enabled and self.spawn is not None:
                    promoted = self._promote_locked()
                self._save()
        if promoted is not None:
            self._start(*promoted)

    def _record_age(self, record: dict[str, Any]) -> float:
        stamp = (record.get("worker_started_at") or record.get("dispatch_started_at")
                 or record.get("created_at"))
        if not isinstance(stamp, str):
            return float("inf")
        try:
            then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return (datetime.now(UTC) - then).total_seconds()
        except (TypeError, ValueError):
            return float("inf")

    def _worker_alive(self, record: dict[str, Any]) -> bool:
        host = record.get("worker_host")
        if isinstance(host, str) and host and host != self._host:
            return True  # unknown across hosts: never release the slot blindly
        pid = record.get("worker_pid")
        if type(pid) is not int or pid <= 0:
            # A very recent record may still be between create() and spawn().
            return self._record_age(record) < 300
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @contextmanager
    def _file_lock(self):
        path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load(self) -> dict[str, Any]:
        empty = {"schema_version": 1, "owners": {}, "idempotency": {}, "runs": {}, "sequence": 0}
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty
        except (OSError, UnicodeError) as error:
            # Invalid bytes are a damaged registry, not a missing one.
            raise RunUnavailable("state_unavailable") from error
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise RunUnavailable("state_unavailable") from error
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                or not all(isinstance(payload.get(key), dict)
                           for key in ("owners", "idempotency", "runs"))):
            raise RunUnavailable("state_unavailable")
        return payload

    def _reload(self) -> dict[str, Any]:
        if self._state_error is not None:
            raise self._state_error
        try:
            self._state = self._load()
        except RunUnavailable as error:
            self._state_error = error
            raise
        return self._state

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.state_path.with_name(f".{self.state_path.name}.{secrets.token_hex(4)}.tmp")
        try:
            temp.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n")
            temp.chmod(0o600)
            temp.replace(self.state_path)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    def owner(self, run_id: str) -> str | None:
        with self._lock, self._file_lock():
            self._state = self._reload()
            value = self._state["owners"].get(run_id)
        return value if isinstance(value, str) else None

    def visible_to(self, run_id: str, uid: str) -> bool:
        owner = self.owner(run_id)
        return owner is None or owner == uid

    def list_for(self, uid: str) -> list[dict[str, Any]]:
        with self._lock, self._file_lock():
            self._state = self._reload()
            return [
                {"run_id": run_id, "state": record.get("state", "unknown"),
                 "scenario": record.get("scenario")}
                for run_id, record in sorted(self._state["runs"].items())
                if record.get("uid") == uid
            ]

    def state_of(self, run_id: str) -> str | None:
        with self._lock, self._file_lock():
            self._state = self._reload()
            record = self._state["runs"].get(run_id)
        return record.get("state") if isinstance(record, dict) else None

    def create(self, uid: str, scenario: str, idempotency_key: str) -> dict[str, Any]:
        if scenario not in TASKS:
            raise RunUnavailable("invalid_scenario")
        if not isinstance(idempotency_key, str) or IDEMPOTENCY.fullmatch(idempotency_key) is None:
            raise RunUnavailable("invalid_idempotency_key")
        if not self.enabled or self.spawn is None:
            raise RunUnavailable("runner_disabled")
        with self._lock, self._file_lock():
            self._state = self._reload()
            key = f"{uid}:{idempotency_key}"
            reused = self._state["idempotency"].get(key)
            if reused is not None:
                if isinstance(reused, dict):
                    if reused.get("scenario") != scenario:
                        raise RunUnavailable("idempotency_conflict")
                    run_id = reused.get("run_id")
                else:
                    run_id = reused
                    record = self._state["runs"].get(run_id, {})
                    if record.get("scenario") != scenario:
                        raise RunUnavailable("idempotency_conflict")
                record = self._state["runs"].get(run_id, {})
                return {"run_id": run_id, "state": record.get("state", "unknown"), "reused": True}
            for record in self._state["runs"].values():
                if record.get("state") == "running" and not self._worker_alive(record):
                    record["state"] = "interrupted"
            run_id = (
                f"api-{scenario.lower()}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
                f"-{secrets.token_hex(3)}"
            )
            stored_sequence = self._state.get("sequence", 0)
            if type(stored_sequence) is not int or stored_sequence < 0:
                stored_sequence = 0
            prior_sequences = [
                item.get("queue_seq") for item in self._state["runs"].values()
                if isinstance(item, dict) and type(item.get("queue_seq")) is int
                and item["queue_seq"] >= 0
            ]
            queue_seq = max([stored_sequence, *prior_sequences], default=0) + 1
            self._state["sequence"] = queue_seq
            busy = any(
                record.get("state") == "running" for record in self._state["runs"].values()
            )
            state = "queued" if busy else "running"
            self._state["owners"][run_id] = uid
            self._state["idempotency"][key] = {"run_id": run_id, "scenario": scenario}
            self._state["runs"][run_id] = {
                "uid": uid, "scenario": scenario, "state": state,
                "queue_seq": queue_seq,
                "created_at": datetime.now(UTC).isoformat(),
                **({"dispatch_started_at": datetime.now(UTC).isoformat()}
                   if state == "running" else {}),
            }
            self._save()
        if state == "running":
            self._start(run_id, scenario)
        return {"run_id": run_id, "state": state, "reused": False}

    def _start(self, run_id: str, scenario: str) -> None:
        try:
            process = self.spawn(default_command(run_id, scenario, self.data_root), run_id)  # type: ignore[misc]
        except Exception:
            with self._lock, self._file_lock():
                self._state = self._reload()
                record = self._state["runs"].get(run_id)
                if isinstance(record, dict) and record.get("state") == "running":
                    record["state"] = "failed"
                promoted = self._promote_locked()
                self._save()
            if promoted is not None:
                self._start(*promoted)
            return
        with self._lock, self._file_lock():
            self._state = self._reload()
            record = self._state["runs"].get(run_id)
            if isinstance(record, dict):
                record["worker_pid"] = getattr(process, "pid", None)
                record["worker_host"] = self._host
                record["worker_started_at"] = datetime.now(UTC).isoformat()
                self._save()
        threading.Thread(
            target=self._wait, args=(run_id, process), daemon=True,
            name=f"forgeflow-run-{run_id}",
        ).start()

    def _promote_locked(self) -> tuple[str, str] | None:
        # Promotion is a slot allocation, not just a queue pop: another
        # instance (or an earlier wait thread) may already have started C.
        if any(
            isinstance(item, dict) and item.get("state") == "running"
            for item in self._state["runs"].values()
        ):
            return None
        queued = [
            (name, item) for name, item in self._state["runs"].items()
            if isinstance(item, dict) and item.get("state") == "queued"
        ]
        if not queued:
            return None
        def sequence(item):
            value = item.get("queue_seq")
            return value if type(value) is int and value >= 0 else 0
        promoted = min(queued, key=lambda row: (sequence(row[1]), row[0]))[0]
        record = self._state["runs"][promoted]
        record["state"] = "running"
        record["dispatch_started_at"] = datetime.now(UTC).isoformat()
        for stale in ("worker_pid", "worker_host", "worker_started_at"):
            record.pop(stale, None)
        return promoted, record["scenario"]

    def _wait(self, run_id: str, process) -> None:
        try:
            wait = getattr(process, "wait", None)
            if callable(wait):
                wait()
        except Exception:
            pass
        finally:
            with self._lock, self._file_lock():
                self._state = self._reload()
                record = self._state["runs"].get(run_id)
                if isinstance(record, dict) and record.get("state") == "running":
                    record["state"] = "finished"
                promoted = self._promote_locked()
                self._save()
            if promoted is not None:
                self._start(*promoted)

