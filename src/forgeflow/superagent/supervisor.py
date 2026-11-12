"""In-process supervisor: run the background loops and restart a dead one.

The previous supervisor recorded a thread error and left the service dead.  That
is not "process monitoring": a source loop that crashed on a transient database
error silently stopped watching the tenant.  This version restarts a failed loop
with bounded backoff, reports how many times it restarted, and marks the service
as unhealthy once the restart budget is gone.

Restarting a *task* is a different decision and lives in the watchdog: a worker
lease is recovered from durable state, not by restarting a thread.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class ServiceState:
    name: str
    thread: threading.Thread | None = None
    error: str = ""
    restarts: int = 0
    finished: bool = False
    last_started_at: float | None = None
    last_error_at: float | None = None
    restartable: bool = True


class Supervisor:
    def __init__(self, *, max_restarts: int = 5, backoff_seconds: float = 1.0) -> None:
        if max_restarts < 0 or backoff_seconds < 0:
            raise ValueError("invalid_supervisor_policy")
        self.max_restarts = int(max_restarts)
        self.backoff_seconds = float(backoff_seconds)
        self._runnables: dict[str, object] = {}
        self._states: dict[str, ServiceState] = {}
        self._stop = threading.Event()

    def register(self, name: str, runnable, *, restartable: bool = True) -> None:
        if not name or name in self._runnables:
            raise ValueError("supervisor_service_invalid")
        self._runnables[name] = runnable
        self._states[name] = ServiceState(name, restartable=restartable)

    def start(self) -> None:
        for name, runnable in self._runnables.items():
            state = self._states[name]
            state.thread = threading.Thread(
                target=self._supervise, args=(name, runnable), name=name, daemon=True)
            state.last_started_at = time.time()
            state.thread.start()

    def _supervise(self, name: str, runnable) -> None:
        """Run one service until it returns or exhausts its restart budget."""
        state = self._states[name]
        while not self._stop.is_set():
            try:
                runnable.run(self._stop)
            except Exception as error:  # noqa: BLE001 - classified and reported
                state.error = f"{type(error).__name__}: {error}"[:200]
                state.last_error_at = time.time()
                if not state.restartable or state.restarts >= self.max_restarts:
                    state.finished = True
                    return
                state.restarts += 1
                # A stop request must interrupt the backoff immediately.
                if self._stop.wait(self.backoff_seconds * state.restarts):
                    state.finished = True
                    return
                state.last_started_at = time.time()
                continue
            state.finished = True
            return
        state.finished = True

    def stop(self) -> None:
        self._stop.set()
        for state in self._states.values():
            if state.thread is not None:
                state.thread.join(timeout=10)

    def status(self) -> dict[str, dict]:
        return {
            name: {
                "alive": bool(state.thread and state.thread.is_alive()
                              and not state.finished),
                "error": state.error,
                "restarts": state.restarts,
                "finished": state.finished,
                "healthy": not state.error or state.restarts < self.max_restarts,
            }
            for name, state in self._states.items()
        }

    def unhealthy(self) -> list[str]:
        return [name for name, value in self.status().items()
                if not value["alive"] or not value["healthy"]]

    def run(self) -> None:
        self.start()
        try:
            self._stop.wait()
        finally:
            self.stop()
