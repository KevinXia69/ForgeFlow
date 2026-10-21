"""API-created runs: ownership, idempotency and a single-active queue.

The API never talks to Odoo. It records who asked for which whitelisted
scenario, and (only when the service operator enables it) starts the existing
CLI runner in a detached process. Recorded runs created outside the API keep
no owner and stay visible to every signed-in reviewer.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import time
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


def default_command(run_id: str, scenario: str) -> list[str]:
    """The one product-mode command; scenario names are whitelisted upstream."""
    return [
        sys.executable, "-m", "forgeflow.cli", "run", scenario,
        "--agent", "pi",
        "--security-profile", "permission-split-v1",
        "--planner-profile", "cp-sat-recovery-v1",
        "--data-profile", "planning-metadata-v1",
        "--run-id", run_id,
    ]


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
        self._state = self._load()
        # A run left "running" by a previous process can no longer be trusted.
        for record in self._state["runs"].values():
            if record.get("state") == "running":
                record["state"] = "interrupted"
        self._save()

    def _load(self) -> dict[str, Any]:
        empty = {"schema_version": 1, "owners": {}, "idempotency": {}, "runs": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(payload, dict):
            return empty
        for key in empty:
            if key not in payload:
                payload[key] = empty[key]
        return payload

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
        with self._lock:
            value = self._state["owners"].get(run_id)
        return value if isinstance(value, str) else None

    def visible_to(self, run_id: str, uid: str) -> bool:
        owner = self.owner(run_id)
        return owner is None or owner == uid

    def list_for(self, uid: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"run_id": run_id, "state": record.get("state", "unknown"),
                 "scenario": record.get("scenario")}
                for run_id, record in sorted(self._state["runs"].items())
                if record.get("uid") == uid
            ]

    def state_of(self, run_id: str) -> str | None:
        with self._lock:
            record = self._state["runs"].get(run_id)
        return record.get("state") if isinstance(record, dict) else None

    def create(self, uid: str, scenario: str, idempotency_key: str) -> dict[str, Any]:
        if scenario not in TASKS:
            raise RunUnavailable("invalid_scenario")
        if not isinstance(idempotency_key, str) or IDEMPOTENCY.fullmatch(idempotency_key) is None:
            raise RunUnavailable("invalid_idempotency_key")
        if not self.enabled or self.spawn is None:
            raise RunUnavailable("runner_disabled")
        with self._lock:
            key = f"{uid}:{idempotency_key}"
            reused = self._state["idempotency"].get(key)
            if reused is not None:
                record = self._state["runs"].get(reused, {})
                return {"run_id": reused, "state": record.get("state", "unknown"), "reused": True}
            run_id = (
                f"api-{scenario.lower()}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
                f"-{secrets.token_hex(3)}"
            )
            busy = any(
                record.get("state") == "running" for record in self._state["runs"].values()
            )
            state = "queued" if busy else "running"
            self._state["owners"][run_id] = uid
            self._state["idempotency"][key] = run_id
            self._state["runs"][run_id] = {
                "uid": uid, "scenario": scenario, "state": state,
                "created_at": datetime.now(UTC).isoformat(),
            }
            self._save()
        if state == "running":
            threading.Thread(
                target=self._execute, args=(run_id, scenario), daemon=True,
                name=f"forgeflow-run-{run_id}",
            ).start()
        return {"run_id": run_id, "state": state, "reused": False}

    def _execute(self, run_id: str, scenario: str) -> None:
        try:
            process = self.spawn(default_command(run_id, scenario), run_id)  # type: ignore[misc]
            wait = getattr(process, "wait", None)
            if callable(wait):
                wait()
        except Exception:
            pass
        finally:
            with self._lock:
                record = self._state["runs"].get(run_id)
                if isinstance(record, dict) and record.get("state") == "running":
                    record["state"] = "finished"
                promoted = next(
                    (name for name, item in self._state["runs"].items()
                     if isinstance(item, dict) and item.get("state") == "queued"),
                    None,
                )
                if promoted is not None:
                    self._state["runs"][promoted]["state"] = "running"
                    scenario = self._state["runs"][promoted]["scenario"]
                self._save()
            if promoted is not None:
                threading.Thread(
                    target=self._execute, args=(promoted, scenario), daemon=True,
                    name=f"forgeflow-run-{promoted}",
                ).start()
