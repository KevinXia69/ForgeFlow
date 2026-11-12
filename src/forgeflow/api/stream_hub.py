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

    def __init__(self, root, retention_seconds: float = 3600.0,
                 max_records: int | None = None) -> None:
        self.root = root
        self.retention = retention_seconds
        self.max_records = max_records
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str):
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def save(self, record: dict) -> None:
        path = self._path(str(record.get("key", "")))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        self._trim(path)

    def _trim(self, keep=None) -> None:
        if self.max_records is None:
            return
        paths = []
        for path in self.root.glob("*.json"):
            try:
                paths.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for _mtime, path in sorted(paths):
            if len(paths) <= self.max_records:
                break
            if path == keep:
                continue
            path.unlink(missing_ok=True)
            paths.remove((_mtime, path))

    def load(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            info = path.stat()
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict) or value.get("key") != key:
            return None
        updated = value.get("updated_at")
        if isinstance(updated, (int, float)) and not isinstance(updated, bool):
            age = time.time() - float(updated)
        else:
            age = time.time() - info.st_mtime
        if age > self.retention:
            path.unlink(missing_ok=True)
            return None
        return value

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
        self._persist_lock = threading.Lock()
        self._done = False
        self._cancelled = False
        self._finished_monotonic: float | None = None
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
        producer = self._producer
        try:
            try:
                for event in producer:
                    if self._cancelled:
                        break
                    if isinstance(event, dict):
                        self._publish(event)
            finally:
                # A break leaves the generator suspended. Close it on the
                # consuming thread so SDK cleanup (and its provider slot) is
                # completed before this job reports itself done.
                close = getattr(producer, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except Exception:
            self._publish({"type": "error", "code": "stream_failed"})
        finally:
            try:
                self._persist_snapshot()
            finally:
                with self._lock:
                    self._done = True
                    self._finished_monotonic = time.monotonic()
                    subscribers = list(self._subscribers)
                for sink in subscribers:
                    sink.put(DONE)

    def _persist_snapshot(self) -> None:
        if self._on_finish is not None:
            # Take the snapshot inside the persistence lock: a producer finishing
            # after Stop must not overwrite the cancellation with an older final.
            with self._persist_lock:
                self._on_finish(self.snapshot())

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
                    "updated_at": time.time(), "last_confirmed_at": time.time()}

    @classmethod
    def from_journal(cls, record: dict, *, on_finish=None) -> "StreamJob":
        job = cls(str(record.get("key", "")), (), metadata=record.get("metadata"),
                  request_hash=record.get("request_hash"), on_finish=on_finish)
        job._events = [(int(seq), event) for seq, event in record.get("events", [])]
        job._next_sequence = max([seq for seq, _ in job._events], default=0) + 1
        job._cancelled = record.get("status") == "cancelled"
        if job._cancelled:
            job._events.clear()
        job._done = True
        updated = record.get("updated_at")
        if isinstance(updated, (int, float)) and not isinstance(updated, bool):
            job._finished_monotonic = time.monotonic() - max(0.0, time.time() - float(updated))
        else:
            job._finished_monotonic = time.monotonic()
        return job

    @property
    def done(self) -> bool:
        with self._lock:
            return self._done

    @property
    def finished_at(self) -> float | None:
        with self._lock:
            return self._finished_monotonic

    @property
    def busy(self) -> bool:
        """True while this turn still owns a producer execution slot."""
        with self._lock:
            done = self._done
        return not done or self._thread.is_alive()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

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
            self._finished_monotonic = self._finished_monotonic or time.monotonic()
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
                 journal: TurnJournal | None = None) -> None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be positive")
        self._jobs: dict[str, StreamJob] = {}
        self._finished_at: dict[str, float] = {}
        self._retention = retention_seconds
        self._max_jobs = max_jobs
        self._journal = journal
        if journal is not None and journal.max_records is None:
            journal.max_records = max_jobs
        self._lock = threading.Lock()

    def _save_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._journal is not None:
            self._journal.save(snapshot)

    def start(self, key: str, producer_factory, *, request_hash=None, on_cancel=None,
              reuse_completed=False, metadata=None, on_create=None) -> StreamJob:
        with self._lock:
            now = time.monotonic()
            active = self._jobs.get(key)
            if active is not None and active.cancelled:
                if active.request_hash != request_hash:
                    raise ValueError("turn_payload_mismatch")
                return active
            if active is not None and (not active.done or reuse_completed):
                if active.request_hash != request_hash:
                    raise ValueError("turn_payload_mismatch")
                return active
            self._purge_locked()
            if self._journal is not None:
                record = self._journal.load(key)
                if record is not None and (
                        record.get("status") == "cancelled"
                        or record.get("status") == "complete" and reuse_completed
                ):
                    if record.get("request_hash") not in (None, request_hash):
                        raise ValueError("turn_payload_mismatch")
                    # An explicit turn id is allowed to recover a completed
                    # turn. A legacy no-turn-id repeat must recompute.
                    return StreamJob.from_journal(record, on_finish=self._save_snapshot)
            self._ensure_capacity_locked(now)
            job = StreamJob(key, producer_factory(), request_hash=request_hash, on_cancel=on_cancel,
                            on_finish=self._save_snapshot, metadata=metadata)
            self._jobs[key] = job
            self._finished_at.pop(key, None)
            if on_create is not None:
                try:
                    # The creation callback commits turn-scoped state before a
                    # competing start can observe this job.
                    on_create()
                except BaseException:
                    self._jobs.pop(key, None)
                    raise
            job.start()
            return job

    def _evict_finished_locked(self, now: float) -> bool:
        finished = [
            (key, job) for key, job in self._jobs.items()
            if job.done and not job.busy
        ]
        if not finished:
            return False
        key, _job = min(
            finished,
            key=lambda item: item[1].finished_at
            if item[1].finished_at is not None else self._finished_at.get(item[0], now))
        self._jobs.pop(key, None)
        self._finished_at.pop(key, None)
        return True

    def _ensure_capacity_locked(self, now: float) -> None:
        # A cancelled job still owns its producer thread until the provider
        # request actually ends; it must continue to consume an execution slot.
        if sum(1 for job in self._jobs.values() if job.busy) >= self._max_jobs:
            raise StreamCapacityError("stream_capacity")
        if len(self._jobs) >= self._max_jobs and not self._evict_finished_locked(now):
            raise StreamCapacityError("stream_capacity")

    def get(self, key: str, last_event_id: int | None = None) -> StreamJob | None:
        with self._lock:
            self._purge_locked()
            job = self._jobs.get(key)
            if job is None and self._journal is not None:
                record = self._journal.load(key)
                if record is not None and record.get("status") in {"complete", "cancelled"}:
                    try:
                        self._ensure_capacity_locked(time.monotonic())
                    except StreamCapacityError:
                        return None
                    job = StreamJob.from_journal(record, on_finish=self._save_snapshot)
                    self._jobs[key] = job
                    updated = record.get("updated_at")
                    if isinstance(updated, (int, float)) and not isinstance(updated, bool):
                        age = max(0.0, time.time() - float(updated))
                    else:
                        age = 0.0
                    self._finished_at[key] = time.monotonic() - age
            return job

    def _purge_locked(self) -> None:
        now = time.monotonic()
        if self._journal is not None:
            self._journal.purge()
        for key, job in list(self._jobs.items()):
            if not job.done or job.busy:
                continue
            finished = job.finished_at
            if finished is None:
                finished = self._finished_at.setdefault(key, now)
            else:
                self._finished_at[key] = finished
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
            if job is not None:
                job.cancel()
                return True
            if self._journal is not None:
                # Journal-only early Stop: this does not consume an in-memory
                # execution slot and is still bounded by the journal's capacity.
                job = StreamJob(key, (), on_finish=self._save_snapshot)
                job.cancel()
                return True
            try:
                self._ensure_capacity_locked(time.monotonic())
            except StreamCapacityError:
                return False
            # Remember an early Stop even if its start request is in flight.
            job = StreamJob(key, (), on_finish=self._save_snapshot)
            self._jobs[key] = job
            self._finished_at.pop(key, None)
        job.cancel()
        return True

    def forget_scope(self, prefix: str) -> None:
        with self._lock:
            keys = [key for key in self._jobs if key.startswith(prefix)]
        for key in keys:
            self.forget(key)
