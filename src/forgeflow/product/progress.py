"""Thread-safe, in-memory phase progress for the local product executor.

This is intentionally ephemeral: it only feeds the live Activity stream while a
local approval is executing. Durable evidence remains the case file, audit log,
approval store and verifier reward. No secret or ERP payload is published here.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class ProgressHub:
    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def reset(self, case_id: str) -> None:
        with self._lock:
            self._events[case_id] = []

    def emit(self, case_id: str, phase: str, status: str, detail: str = "") -> None:
        event = {
            "phase": phase,
            "status": status,
            "detail": detail,
            "ts": round(time.time(), 3),
        }
        with self._lock:
            self._events.setdefault(case_id, []).append(event)

    def snapshot(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events.get(case_id, []))


HUB = ProgressHub()
