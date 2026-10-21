"""In-process resumable SSE stream for one owner-scoped assistant turn.

A job owns one producer generator, a bounded ordered event buffer, and live
subscribers. Reconnecting with ``Last-Event-ID`` replays only events the client
missed. The buffer is process-local; it is not a durable transcript.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from collections.abc import Generator, Iterable
from typing import Any

MAX_EVENTS = 400
DEFAULT_MAX_JOBS = 64
DONE = object()


class TurnJournal:
    """Completed-turn journal. In-flight turns are deliberately not resumed."""

    def __init__(self, root, retention_seconds: float = 3600.0) -> None:
        self.root = root
        self.retention = retention_seconds
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str):
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def save(self, record: dict) -> None:
        path = self._path(str(record.get("key", "")))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def load(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) and value.get("key") == key else None

    def purge(self) -> None:
        cutoff = time.time() - self.retention
        for path in self.root.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


class StreamCapacityError(RuntimeError):
    """No finished job can be evicted and the active-job cap is reached."""


class StreamJob:
    def __init__(self, key: str, producer: Iterable[dict[str, Any]], *, request_hash=None,
                 on_cancel=None, on_finish=None, metadata=None) -> None:
        self.key = key
        self.request_hash = request_hash
        self._on_cancel = on_cancel
        self._on_finish = on_finish
        self.metadata = dict(metadata or {})
        self._producer = producer
        self._events: list[tuple[int, dict[str, Any]]] = []
        self._next_sequence = 1
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._done = False
        self._cancelled = False
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
            if self._on_finish is not None:
                self._on_finish(self.snapshot())
            with self._lock:
                self._done = True
                subscribers = list(self._subscribers)
            for sink in subscribers:
                sink.put(DONE)

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

    @staticmethod
    def _frame(seq: int, event: dict[str, Any]) -> str:
        return f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"key": self.key, "status": "cancelled" if self._cancelled else "complete",
                    "cursor": self._next_sequence - 1, "events": list(self._events),
                    "metadata": self.metadata, "request_hash": self.request_hash,
                    "updated_at": time.time(), "last_confirmed_at": time.time()}

    @classmethod
    def from_journal(cls, record: dict) -> "StreamJob":
        job = cls(str(record.get("key", "")), (), metadata=record.get("metadata"),
                  request_hash=record.get("request_hash"))
        job._events = [(int(seq), event) for seq, event in record.get("events", [])]
        job._next_sequence = max([seq for seq, _ in job._events], default=0) + 1
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


class AssistantStreamHub:
    """Keep one resumable job per conversation key for a short retention."""

    def __init__(self, retention_seconds: float = 300.0, max_jobs: int = DEFAULT_MAX_JOBS,
                 journal: TurnJournal | None = None) -> None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        self._jobs: dict[str, StreamJob] = {}
        self._finished_at: dict[str, float] = {}
        self._retention = retention_seconds
        self._max_jobs = max_jobs
        self._journal = journal
        self._lock = threading.Lock()

    def start(self, key: str, producer_factory, *, request_hash=None, on_cancel=None,
              reuse_completed=False, metadata=None) -> StreamJob:
        with self._lock:
            now = time.monotonic()
            active = self._jobs.get(key)
            if active is not None and (not active.done or reuse_completed):
                if active.request_hash != request_hash:
                    raise ValueError("turn_payload_mismatch")
                return active
            self._purge_locked()
            if self._journal is not None:
                self._journal.purge()
            if len(self._jobs) >= self._max_jobs:
                finished = [key for key, candidate in self._jobs.items() if candidate.done]
                if finished:
                    oldest = min(finished, key=lambda item: self._finished_at.get(item, now))
                    self._jobs.pop(oldest, None)
                    self._finished_at.pop(oldest, None)
                else:
                    raise StreamCapacityError("stream_capacity")
            def finished(snapshot):
                if self._journal is not None:
                    self._journal.save(snapshot)

            job = StreamJob(key, producer_factory(), request_hash=request_hash, on_cancel=on_cancel,
                            on_finish=finished, metadata=metadata)
            self._jobs[key] = job
            self._finished_at.pop(key, None)
            job.start()
            return job

    def get(self, key: str, last_event_id: int | None = None) -> StreamJob | None:
        with self._lock:
            self._purge_locked()
            job = self._jobs.get(key)
            if job is None and self._journal is not None:
                record = self._journal.load(key)
                if record is not None and record.get("status") in {"complete", "cancelled"}:
                    job = StreamJob.from_journal(record)
                    self._jobs[key] = job
                    self._finished_at[key] = time.monotonic()
            return job

    def _purge_locked(self) -> None:
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if not job.done:
                continue
            finished = self._finished_at.setdefault(key, now)
            if now - finished > self._retention:
                self._jobs.pop(key, None)
                self._finished_at.pop(key, None)

    def forget(self, key: str) -> None:
        with self._lock:
            job = self._jobs.pop(key, None)
            self._finished_at.pop(key, None)
        if job is not None:
            job.cancel()

    def cancel(self, key: str) -> bool:
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                # Remember an early Stop even if its start request is in flight.
                job = StreamJob(key, ())
                self._jobs[key] = job
        job.cancel()
        return True

    def forget_scope(self, prefix: str) -> None:
        with self._lock:
            keys = [key for key in self._jobs if key.startswith(prefix)]
        for key in keys:
            self.forget(key)
