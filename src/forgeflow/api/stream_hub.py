"""In-process resumable SSE stream for one owner-scoped assistant turn.

A job owns one producer generator, a bounded ordered event buffer, and live
subscribers. Reconnecting with ``Last-Event-ID`` replays only events the client
missed. The buffer is process-local; it is not a durable transcript.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import threading
import time
from collections.abc import Generator, Iterable
from typing import Any

MAX_EVENTS = 400
DEFAULT_MAX_JOBS = 64
DEFAULT_MAX_EARLY_CANCELLATIONS = 64
DEFAULT_MAX_EARLY_CANCELLATIONS_PER_OWNER = 16
DONE = object()


class TurnJournal:
    """Completed-turn journal. In-flight turns are deliberately not resumed."""

    def __init__(self, root, retention_seconds: float = 3600.0) -> None:
        self.root = root
        self.retention = retention_seconds
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str):
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def save(self, record: dict) -> None:
        with self._lock:
            path = self._path(str(record.get("key", "")))
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(path)

    def _load_path(self, path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            expires_at = value.get("expires_at")
            if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
                updated = value.get("updated_at")
                if not isinstance(updated, (int, float)) or not math.isfinite(updated):
                    updated = path.stat().st_mtime
                expires_at = updated + self.retention
            if expires_at <= time.time():
                path.unlink(missing_ok=True)
                return None
            # Loading or cancelling again must never extend the saved lifetime.
            return {**value, "expires_at": expires_at}
        except (OSError, ValueError):
            return None

    def load(self, key: str) -> dict | None:
        with self._lock:
            value = self._load_path(self._path(key))
            return value if value is not None and value.get("key") == key else None

    def purge(self) -> None:
        with self._lock:
            for path in self.root.glob("*.json"):
                self._load_path(path)

    def early_cancel_counts(self, owner: str | None) -> tuple[int, int]:
        """Count live early Stops on disk, including records from before restart."""
        total = owned = 0
        with self._lock:
            for path in self.root.glob("*.json"):
                record = self._load_path(path)
                if record is None or record.get("status") != "cancelled":
                    continue
                metadata = record.get("metadata") or {}
                # Older early Stops had no owner or metadata. They still consume
                # the global allowance until their original retention expires.
                early = metadata.get("early_cancel", not metadata.get("owner"))
                if early:
                    total += 1
                    owned += metadata.get("owner") == owner
        return total, owned


class StreamCapacityError(RuntimeError):
    """A stream cache or early-cancellation admission limit is reached."""


class StreamJob:
    def __init__(self, key: str, producer: Iterable[dict[str, Any]], *, request_hash=None,
                 on_cancel=None, on_finish=None, metadata=None, expires_at=None) -> None:
        self.key = key
        self.request_hash = request_hash
        self._on_cancel = on_cancel
        self._on_finish = on_finish
        self.metadata = dict(metadata or {})
        self.expires_at = expires_at
        self._producer = producer
        self._events: list[tuple[int, dict[str, Any]]] = []
        self._next_sequence = 1
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._done = False
        self._cancelled = False
        self._cancel_persisted = False
        self._error: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self._cancelled:
                return
            seq = self._next_sequence
            self._next_sequence += 1
            self._events.append((seq, event))
            if len(self._events) > MAX_EVENTS:
                self._events = self._events[-MAX_EVENTS:]
            subscribers = list(self._subscribers)
        for sink in subscribers:
            sink.put((seq, event))

    def _run(self) -> None:
        try:
            for event in self._producer:
                if self._cancelled:
                    break
                if isinstance(event, dict):
                    self._publish(event)
        except Exception:
            self._publish({"type": "error", "code": "stream_failed"})
        finally:
            try:
                # Breaking a generator after cancellation does not close it.
                # Close explicitly so runtime leases and budget cancellation
                # run even while the completed job remains cached for replay.
                try:
                    close = getattr(self._producer, "close", None)
                    if close is not None:
                        close()
                finally:
                    self._persist_snapshot()
            finally:
                with self._lock:
                    self._done = True
                    subscribers = list(self._subscribers)
                for sink in subscribers:
                    sink.put(DONE)

    def _persist_snapshot(self) -> None:
        if self._on_finish is not None:
            # Take the snapshot inside the persistence lock: a producer finishing
            # after Stop must not overwrite the cancellation with an older final.
            with self._persist_lock:
                snapshot = self.snapshot()
                self._on_finish(snapshot)
                if snapshot["status"] == "cancelled":
                    with self._lock:
                        self._cancel_persisted = True

    def subscribe(self, last_event_id: int | None) -> Generator[str, None, None]:
        sink: queue.Queue = queue.Queue()
        with self._lock:
            replay = [
                item for item in self._events
                if last_event_id is None or item[0] > last_event_id
            ]
            done = self._done
            if not done:
                self._subscribers.add(sink)
        try:
            for seq, event in replay:
                if self._cancelled:
                    return
                yield self._frame(seq, event)
            if done:
                return
            while True:
                try:
                    item = sink.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if item is DONE:
                    return
                if self._cancelled:
                    return
                seq, event = item
                yield self._frame(seq, event)
        finally:
            with self._lock:
                self._subscribers.discard(sink)
                orphaned = not self._subscribers and not self._done and not self._cancelled
            if orphaned:
                self._arm_orphan_timer()

    @staticmethod
    def _frame(seq: int, event: dict[str, Any]) -> str:
        return f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"key": self.key, "status": "cancelled" if self._cancelled else "complete",
                    "cursor": self._next_sequence - 1, "events": list(self._events),
                    "metadata": self.metadata, "request_hash": self.request_hash,
                    "expires_at": self.expires_at,
                    "updated_at": time.time(), "last_confirmed_at": time.time()}

    @classmethod
    def from_journal(cls, record: dict, *, on_finish=None) -> "StreamJob":
        job = cls(str(record.get("key", "")), (), metadata=record.get("metadata"),
                  request_hash=record.get("request_hash"), on_finish=on_finish,
                  expires_at=record.get("expires_at"))
        job._events = [(int(seq), event) for seq, event in record.get("events", [])]
        job._next_sequence = max([seq for seq, _ in job._events], default=0) + 1
        job._cancelled = record.get("status") == "cancelled"
        job._cancel_persisted = job._cancelled
        if job._cancelled:
            job._events.clear()
        job._done = True
        return job

    @property
    def done(self) -> bool:
        with self._lock:
            return self._done

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def cancellation_persisted(self) -> bool:
        with self._lock:
            return self._cancel_persisted

    ORPHAN_GRACE = 15.0

    def _arm_orphan_timer(self) -> None:
        """Nobody is listening any more: stop the turn after a short grace.

        A browser may drop the stream (navigation, network, client timeout) and
        come back with the same turn id; the grace period keeps that resume
        working while making sure an abandoned turn does not keep calling the
        model.
        """
        timer = getattr(self, "_orphan_timer", None)
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(self.ORPHAN_GRACE, self._cancel_if_still_orphaned)
        timer.daemon = True
        self._orphan_timer = timer
        timer.start()

    def _cancel_if_still_orphaned(self) -> None:
        with self._lock:
            if self._subscribers or self._done or self._cancelled:
                return
        self.cancel()

    def cancel(self) -> None:
        """Revoke cached and future output when an authorization scope changes."""
        with self._lock:
            self._cancelled = True
            self._done = True
            self._events.clear()
            subscribers = list(self._subscribers)
        for sink in subscribers:
            sink.put(DONE)
        if self._on_cancel is not None:
            self._on_cancel()
        self._persist_snapshot()


class AssistantStreamHub:
    """Keep one resumable job per conversation key for a short retention."""

    def __init__(self, retention_seconds: float = 300.0, max_jobs: int = DEFAULT_MAX_JOBS,
                 journal: TurnJournal | None = None,
                 max_early_cancellations: int = DEFAULT_MAX_EARLY_CANCELLATIONS,
                 max_early_cancellations_per_owner: int = DEFAULT_MAX_EARLY_CANCELLATIONS_PER_OWNER) -> None:
        if min(max_jobs, max_early_cancellations, max_early_cancellations_per_owner) < 1:
            raise ValueError("stream limits must be positive")
        self._jobs: dict[str, StreamJob] = {}
        self._finished_at: dict[str, float] = {}
        self._retention = retention_seconds
        self._max_jobs = max_jobs
        self._journal = journal
        self._max_early_cancellations = max_early_cancellations
        self._max_early_cancellations_per_owner = max_early_cancellations_per_owner
        self._lock = threading.Lock()

    def _save_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._journal is not None:
            self._journal.save(snapshot)

    def _expiry(self, expires_at=None) -> float:
        if expires_at is None:
            retention = self._journal.retention if self._journal is not None else self._retention
            expires_at = time.time() + retention
        if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
            raise ValueError("invalid_turn_expiry")
        if expires_at <= time.time():
            raise ValueError("turn_expired")
        return expires_at

    def _make_cache_room_locked(self) -> bool:
        if len(self._jobs) < self._max_jobs:
            return True
        # A cancelled turn can leave memory only when its durable tombstone is
        # still available. Without a journal it must remain until expiry.
        finished = [key for key, job in self._jobs.items()
                    if job.done and (not job.cancelled or
                                     self._journal is not None and job.cancellation_persisted)]
        if not finished:
            return False
        oldest = min(finished, key=lambda key: self._finished_at.get(key, 0))
        self._jobs.pop(oldest, None)
        self._finished_at.pop(oldest, None)
        return True

    def _lookup_locked(self, key: str) -> StreamJob | None:
        job = self._jobs.get(key)
        if job is None and self._journal is not None:
            record = self._journal.load(key)
            if record is not None and record.get("status") in {"complete", "cancelled"}:
                job = StreamJob.from_journal(record, on_finish=self._save_snapshot)
                # Completed replays do not need a producer or a cache slot.
                if self._make_cache_room_locked():
                    self._jobs[key] = job
                    self._finished_at[key] = time.monotonic()
        return job

    def start(self, key: str, producer_factory, *, request_hash=None, on_cancel=None,
              reuse_completed=False, metadata=None, expires_at=None) -> StreamJob:
        with self._lock:
            self._purge_locked()
            if self._journal is not None:
                self._journal.purge()
            active = self._lookup_locked(key)
            if active is not None and active.cancelled:
                return active
            if active is not None and (not active.done or reuse_completed):
                if active.request_hash != request_hash:
                    raise ValueError("turn_payload_mismatch")
                return active
            metadata = {**(metadata or {}), "early_cancel": False}
            expires_at = self._expiry(expires_at if expires_at is not None else metadata.get("expires_at"))
            if key not in self._jobs and not self._make_cache_room_locked():
                raise StreamCapacityError("stream_capacity")
            job = StreamJob(key, producer_factory(), request_hash=request_hash, on_cancel=on_cancel,
                            on_finish=self._save_snapshot, metadata=metadata, expires_at=expires_at)
            self._jobs[key] = job
            self._finished_at.pop(key, None)
            job.start()
            return job

    def get(self, key: str, last_event_id: int | None = None) -> StreamJob | None:
        with self._lock:
            self._purge_locked()
            return self._lookup_locked(key)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if not job.done:
                continue
            finished = self._finished_at.setdefault(key, now)
            expired = job.expires_at is not None and job.expires_at <= time.time()
            cache_expired = (now - finished > self._retention and
                             (not job.cancelled or
                              self._journal is not None and job.cancellation_persisted))
            if expired or cache_expired:
                self._jobs.pop(key, None)
                self._finished_at.pop(key, None)

    def forget(self, key: str) -> None:
        with self._lock:
            job = self._jobs.pop(key, None)
            self._finished_at.pop(key, None)
        if job is not None:
            job.cancel()

    def cancel(self, key: str, *, owner: str | None = None, expires_at=None) -> bool:
        with self._lock:
            self._purge_locked()
            if self._journal is not None:
                self._journal.purge()
            job = self._lookup_locked(key)
            if job is None:
                # Remember an early Stop even if its start request is in flight.
                # Admission must succeed before reporting cancellation, and
                # expiry is fixed rather than renewed by retries or restart.
                expiry = self._expiry(expires_at)
                if self._journal is not None:
                    total, owned = self._journal.early_cancel_counts(owner)
                else:
                    early = [candidate for candidate in self._jobs.values()
                             if candidate.cancelled and candidate.metadata.get("early_cancel")]
                    total = len(early)
                    owned = sum(candidate.metadata.get("owner") == owner for candidate in early)
                if total >= self._max_early_cancellations or owned >= self._max_early_cancellations_per_owner:
                    raise StreamCapacityError("cancel_capacity")
                cache_room = self._make_cache_room_locked()
                if not cache_room and self._journal is None:
                    raise StreamCapacityError("cancel_capacity")
                job = StreamJob(key, (), on_finish=self._save_snapshot, expires_at=expiry,
                                metadata={"owner": owner, "early_cancel": True})
                # Persist under the admission lock so concurrent early Stops
                # cannot all pass the same quota. No producer thread is started.
                job.cancel()
                if cache_room:
                    self._jobs[key] = job
                    self._finished_at[key] = time.monotonic()
                return True
        job.cancel()
        return True

    def forget_scope(self, prefix: str) -> None:
        with self._lock:
            keys = [key for key in self._jobs if key.startswith(prefix)]
        for key in keys:
            self.forget(key)
