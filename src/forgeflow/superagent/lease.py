"""Worker lease heartbeat; keeps a running effect from being recovered mid-task."""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from .store import ControlStore


class LeaseKeeper:
    def __init__(self, store: ControlStore, *, effect_id: str, tenant_id: str,
                 fencing_token: str, interval_seconds: float = 15.0,
                 lease_seconds: float = 60.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("invalid_heartbeat_interval")
        self.store = store
        self._worker_store: ControlStore | None = None
        self.effect_id = effect_id
        self.tenant_id = tenant_id
        self.fencing_token = fencing_token
        self.interval_seconds = float(interval_seconds)
        if lease_seconds <= 0:
            raise ValueError("invalid_lease_duration")
        self.extension_seconds = max(float(lease_seconds), self.interval_seconds * 2)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat_already_started")
        self._worker_store = ControlStore(self.store.database_url)
        self._thread = threading.Thread(target=self._run, name="effect-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if self._worker_store is None:
                    return
                with self._worker_store.transaction() as cursor:
                    row = cursor.execute(
                        "UPDATE control_effects SET lease_expires_at=%s, updated_at=now() "
                        "WHERE id=%s AND tenant_id=%s AND fencing_token=%s AND state='running' "
                        "RETURNING id",
                        (datetime.now(UTC) + timedelta(seconds=self.extension_seconds),
                         self.effect_id, self.tenant_id, self.fencing_token)).fetchone()
                if row is None:
                    self.lost.set()
                    return
            except Exception:
                # A transient DB failure must not silently stop the heartbeat.
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)
            self._thread = None
        if self._worker_store is not None:
            self._worker_store.close()
            self._worker_store = None
