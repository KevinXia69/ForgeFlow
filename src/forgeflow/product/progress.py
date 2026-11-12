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
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()

    def reset(self, case_id: str) -> None:
        with self._lock:
            self._events[case_id] = []

    def emit(self, case_id: str, phase: str, status: str, detail: str = "") -> None:
        with self._lock:
            sequence = self._seq.get(case_id, 0) + 1
            self._seq[case_id] = sequence
            self._events.setdefault(case_id, []).append({
                "seq": sequence,
                "phase": phase,
                "status": status,
                "detail": detail,
                "ts": round(time.time(), 3),
            })

    def snapshot(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events.get(case_id, []))


HUB = ProgressHub()
