"""Durable, reconciled execution for trusted planning-document operations.

This module has no RPC transport and is not a model-callable tool. A business
adapter must validate the whole change set and compare live pre/postconditions.
An interrupted or ambiguous mutation is reconciled by readback, never resent.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from forgeflow.recovery.failure import REASONS, STEP_KINDS, cause_summary, context_projection

KINDS = STEP_KINDS
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
_KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_:/.-]{0,199}\Z")


class ExecutionStopped(RuntimeError):
    """Fixed error codes only; adapter exceptions may contain credentials."""

    def __init__(self, reason, *, cause=None):
        super().__init__(reason)
        self.cause = cause_summary(cause)
        self.context = None
        self.failure = None


class _FailureTrace:
    def __init__(self):
        self.stage = "validate_plan"
        self.steps = self.journal = self.step_number = None
        self.history_valid = False

    def capture(self, error):
        reason = str(error) if str(error) in REASONS else "details_unavailable"
        counts = {"dispatch_count": None, "confirmed_steps": None, "pending_dispatch": None}
        # Do not infer durable counters after a failed journal append or parse.
        if (self.history_valid and self.journal is not None and self.steps is not None
                and not reason.startswith(("journal_", "invalid_journal_", "incomplete_journal_"))):
            try:
                started, completed = _history(self.journal.rows, self.steps, plan_digest(self.steps))
                counts = {"dispatch_count": sum(row["event"] == "dispatch" for row in self.journal.rows),
                          "confirmed_steps": len(completed), "pending_dispatch": bool(started - completed)}
            except ExecutionStopped:
                pass
        step = self.steps[self.step_number - 1] if self.step_number is not None else None
        return context_projection({
            "stage": self.stage, "reason": reason, "step_number": self.step_number,
            "step_kind": step.kind if step else None,
            "planned_steps": len(self.steps) if self.steps is not None else None,
            **counts, **error.cause,
        })


def _encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


@dataclass(frozen=True)
class Step:
    key: str
    kind: str
    document_token: str
    payload: dict
    depends_on: tuple[str, ...] = ()

    def record(self) -> dict:
        return {
            "key": self.key, "kind": self.kind, "document_token": self.document_token,
            "payload": self.payload, "depends_on": list(self.depends_on),
        }


class Adapter(Protocol):
    def binding_digest(self, step: Step) -> str:
        """For bind_workorder only: digest its durable, immutable binding artifact."""

    def preflight(self, steps: tuple[Step, ...], completed: frozenset[str]) -> None:
        """Reject invalid projections, stale scope or conflicting live state.

        Must be read-only. On resume, accept only the recorded execution prefix
        and explicitly reconcilable pending operation. Validate the current
        scope, not only the record IDs that will be changed.

        Called again before returning success, with every step completed. The
        adapter must validate the accumulated projection, including fields set
        by earlier steps on the same document. A later step's narrow 'after'
        condition alone cannot prove that those earlier fields remain valid.
        """

    def observe(self, step: Step) -> Literal["before", "after", "conflict"]:
        """Read-only exact business pre/postcondition comparison.

        For create: resolve a stable, unique identity set BEFORE dispatch. An
        absent record is 'before'; exactly one matching record is 'after'.
        Duplicate identities, partial writes and mismatches are 'conflict'.
        """

    def apply(self, step: Step) -> None:
        """Dispatch only a fixed, reviewed planning-document operation."""


def _validate_steps(steps: tuple[Step, ...]) -> str:
    if not isinstance(steps, tuple) or not steps or len(steps) > 2000:
        raise ExecutionStopped("invalid_step_count")
    seen = set()
    for step in steps:
        if (not isinstance(step, Step) or not isinstance(step.kind, str)
                or step.kind not in KINDS):
            raise ExecutionStopped("unsupported_operation")
        if not all(isinstance(s, str) and _KEY.fullmatch(s) for s in
                   (step.key, step.document_token)):
            raise ExecutionStopped("invalid_operation_identity")
        if step.key in seen or not isinstance(step.payload, dict):
            raise ExecutionStopped("invalid_operation_payload")
        if (not isinstance(step.depends_on, tuple)
                or not all(isinstance(key, str) and _KEY.fullmatch(key)
                           for key in step.depends_on)
                or not set(step.depends_on) <= seen):
            raise ExecutionStopped("invalid_dependency_order")
        seen.add(step.key)
    try:
        records = [step.record() for step in steps]
        encoded = _encode(records)
        # JSON silently coerces non-string object keys and tuples. Reject
        # those changes so the hash describes exactly what is dispatched.
        if json.loads(encoded) != records:
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise ExecutionStopped("non_json_payload") from None
    if len(encoded) > 4 * 1024 * 1024:
        raise ExecutionStopped("plan_too_large")
    return hashlib.sha256(encoded).hexdigest()


def _assert_plan_unchanged(steps: tuple[Step, ...], plan_hash: str) -> None:
    try:
        unchanged = _validate_steps(steps) == plan_hash
    except ExecutionStopped:
        unchanged = False
    if not unchanged:
        raise ExecutionStopped("adapter_mutated_plan")


def plan_digest(steps: tuple[Step, ...]) -> str:
    """Validate and identify the exact immutable plan for protected storage."""
    return _validate_steps(steps)


class Journal:
    """Exclusive process lock and fsynced, bounded, hash-linked JSONL records.

    The directory is owned by the executor, outside model-writable storage.
    Hash chaining detects accidental corruption; it is not authentication.
    A partial last line is refused, since the remote write outcome is unknown.
    """

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None
        self.rows: list[dict] = []

    def __enter__(self):
        if self.fd is not None:
            raise ExecutionStopped("journal_already_open")
        self.rows = []
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        self.fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(self.fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOURNAL_BYTES
                    or info.st_nlink != 1):
                raise ExecutionStopped("invalid_journal_file")
            # fsync(file) does not make a newly created directory entry durable.
            # Persist its name before any write-ahead record permits dispatch.
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            chunks = []
            length = 0
            while length <= MAX_JOURNAL_BYTES:
                chunk = os.read(self.fd, MAX_JOURNAL_BYTES + 1 - length)
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            if length > MAX_JOURNAL_BYTES:
                raise ExecutionStopped("invalid_journal_file")
            raw = b"".join(chunks)
            if raw and not raw.endswith(b"\n"):
                raise ExecutionStopped("incomplete_journal_record")
            previous = "0" * 64
            for index, line in enumerate(raw.splitlines()):
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) != {
                    "index", "previous", "event", "data", "sha256",
                }:
                    raise ExecutionStopped("invalid_journal_record")
                unsigned = {key: row[key] for key in ("index", "previous", "event", "data")}
                if (type(row["index"]) is not int or row["index"] != index
                        or row["previous"] != previous
                        or row["sha256"] != _digest(unsigned)):
                    raise ExecutionStopped("journal_integrity_failure")
                previous = row["sha256"]
                self.rows.append(row)
        except BaseException as error:
            os.close(self.fd)
            self.fd = None
            if isinstance(error, BlockingIOError):
                raise ExecutionStopped("execution_already_running") from None
            if isinstance(error, (ValueError, TypeError, UnicodeError)):
                raise ExecutionStopped("invalid_journal_record") from None
            raise
        return self

    def append(self, event: str, data: dict) -> None:
        if self.fd is None:
            raise ExecutionStopped("journal_not_open")
        row = {
            "index": len(self.rows),
            "previous": self.rows[-1]["sha256"] if self.rows else "0" * 64,
            "event": event, "data": data,
        }
        row["sha256"] = _digest(row)
        encoded = _encode(row) + b"\n"
        if os.fstat(self.fd).st_size + len(encoded) > MAX_JOURNAL_BYTES:
            raise ExecutionStopped("journal_size_limit")
        # Write-ahead record must be on disk before the adapter may mutate ERP.
        offset = 0
        while offset < len(encoded):
            written = os.write(self.fd, encoded[offset:])
            if written <= 0:
                raise ExecutionStopped("journal_write_failed")
            offset += written
        os.fsync(self.fd)
        self.rows.append(row)

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _history(rows: list[dict], steps: tuple[Step, ...], plan_hash: str) -> tuple[set, set]:
    if not rows:
        return set(), set()
    if rows[0]["event"] != "plan" or rows[0]["data"] != {
        "schema_version": 1, "plan_sha256": plan_hash, "step_count": len(steps),
    }:
        raise ExecutionStopped("journal_plan_mismatch")
    started, completed = set(), set()
    next_step = 0
    pending = None
    for row in rows[1:]:
        data = row["data"]
        if (not isinstance(data, dict) or not {"key", "step_sha256"} <= set(data)
                or not isinstance(data["key"], str)
                or not isinstance(row["event"], str)):
            raise ExecutionStopped("invalid_journal_event")
        key = data["key"]
        # execute dispatches in sequence, even when no explicit dependency was
        # necessary. Only a contiguous completed prefix and one pending step
        # can therefore be a legitimate crash/recovery history.
        if next_step >= len(steps) or key != steps[next_step].key:
            raise ExecutionStopped("invalid_journal_order")
        step = steps[next_step]
        required = {"key", "step_sha256"}
        if step.kind == "bind_workorder" and row["event"] != "dispatch":
            required.add("binding_sha256")
            if (not isinstance(data.get("binding_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", data["binding_sha256"])):
                raise ExecutionStopped("invalid_binding_receipt")
        if set(data) != required:
            raise ExecutionStopped("invalid_journal_event")
        if data["step_sha256"] != _digest(step.record()):
            raise ExecutionStopped("journal_step_mismatch")
        if row["event"] == "dispatch" and pending is None:
            started.add(key)
            pending = key
        elif row["event"] in {"verified", "reconciled"} and pending == key:
            completed.add(key)
            pending = None
            next_step += 1
        elif row["event"] == "already_applied" and pending is None:
            completed.add(key)
            next_step += 1
        else:
            raise ExecutionStopped("invalid_journal_transition")
    return started, completed


def _completion_data(step: Step, adapter: Adapter) -> dict:
    data = {"key": step.key, "step_sha256": _digest(step.record())}
    if step.kind == "bind_workorder":
        try:
            receipt = adapter.binding_digest(step)
        except Exception:
            raise ExecutionStopped("binding_receipt_unavailable") from None
        if not isinstance(receipt, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt):
            raise ExecutionStopped("invalid_binding_receipt")
        data["binding_sha256"] = receipt
    return data


def _verify_binding_receipts(rows: list[dict], steps: tuple[Step, ...], adapter: Adapter) -> None:
    receipts = {row["data"]["key"]: row["data"]["binding_sha256"] for row in rows[1:]
                if "binding_sha256" in row["data"]}
    for step in steps:
        if step.kind == "bind_workorder" and step.key in receipts:
            if _completion_data(step, adapter)["binding_sha256"] != receipts[step.key]:
                raise ExecutionStopped("binding_receipt_changed")


def execute(
    steps: tuple[Step, ...],
    adapter: Adapter,
    journal_path: Path,
    *,
    authorize_first_dispatch=None,
) -> dict:
    """Execute a validated plan once; replays read back and reconcile only.

    There is no blind network retry and no rollback that invents physical
    events. On any unresolved result, stop with an inspectable journal prefix.
    Original exception text is never exposed, since it may include credentials.
    """
    trace = _FailureTrace()
    try:
        return _execute(steps, adapter, journal_path, authorize_first_dispatch, trace)
    except ExecutionStopped as error:
        error.context = trace.capture(error)
        raise


def _execute(steps, adapter, journal_path, authorize_first_dispatch, trace):
    plan_hash = _validate_steps(steps)
    # Detach nested payloads from the caller. The adapter must not mutate this
    # execution snapshot; check after each callback before permitting progress.
    steps = tuple(Step(step.key, step.kind, step.document_token,
                       json.loads(_encode(step.payload)), step.depends_on) for step in steps)
    trace.steps = steps
    trace.stage = "journal_open"
    with Journal(journal_path) as journal:
        trace.journal = journal
        trace.stage = "journal_history"
        dispatched = any(row["event"] == "dispatch" for row in journal.rows)
        started, completed = _history(journal.rows, steps, plan_hash)
        trace.history_valid = True
        # A completed binding cannot be recreated from whatever state happens
        # to exist now. Its original immutable bytes must still match the log.
        trace.stage = "binding_readback"
        _verify_binding_receipts(journal.rows, steps, adapter)
        _assert_plan_unchanged(steps, plan_hash)
        try:
            trace.stage = "preflight"
            adapter.preflight(steps, frozenset(completed))
        except Exception as error:
            raise ExecutionStopped("preflight_failed", cause=error) from None
        _assert_plan_unchanged(steps, plan_hash)
        if not journal.rows:
            trace.stage = "completion_record"
            journal.append("plan", {
                "schema_version": 1, "plan_sha256": plan_hash, "step_count": len(steps),
            })
        latest_observation = {
            step.document_token: step.key for step in steps
            if step.key in completed or step.key in started
        }
        for number, step in enumerate(steps, 1):
            trace.step_number = number
            if (step.key in completed
                    and latest_observation[step.document_token] != step.key):
                # A later completed or in-flight operation legitimately
                # supersedes this intermediate state. Validate its frontier;
                # preflight is responsible for the full accumulated projection.
                continue
            try:
                trace.stage = "observe_before"
                observed = adapter.observe(step)
            except Exception as error:
                raise ExecutionStopped("readback_failed", cause=error) from None
            _assert_plan_unchanged(steps, plan_hash)
            if step.key in completed:
                if observed != "after":
                    raise ExecutionStopped("completed_step_drift")
                continue
            event_data = {"key": step.key, "step_sha256": _digest(step.record())}
            if observed == "after":
                trace.stage = "completion_record"
                journal.append("reconciled" if step.key in started else "already_applied",
                               _completion_data(step, adapter))
                _assert_plan_unchanged(steps, plan_hash)
                completed.add(step.key)
                continue
            if observed != "before":
                raise ExecutionStopped("precondition_conflict")
            if step.key in started:
                raise ExecutionStopped("unresolved_previous_dispatch")
            if not dispatched:
                # The approval gate guards the first real write only. Any
                # journal that already dispatched a step skips it, so a replay
                # or reconciliation never needs the token again.
                if authorize_first_dispatch is not None:
                    trace.stage = "authorize"
                    authorize_first_dispatch()
                dispatched = True
            trace.stage = "dispatch_record"
            journal.append("dispatch", event_data)
            try:
                trace.stage = "apply"
                adapter.apply(step)
            except Exception:
                # A transport error can follow a committed remote mutation.
                # Always read before deciding; never repeat the mutation here.
                pass
            _assert_plan_unchanged(steps, plan_hash)
            try:
                trace.stage = "observe_after"
                observed = adapter.observe(step)
            except Exception as error:
                raise ExecutionStopped("mutation_outcome_unknown", cause=error) from None
            _assert_plan_unchanged(steps, plan_hash)
            if observed != "after":
                raise ExecutionStopped("mutation_not_verified")
            trace.stage = "completion_record"
            journal.append("verified", _completion_data(step, adapter))
            _assert_plan_unchanged(steps, plan_hash)
            completed.add(step.key)
        trace.stage, trace.step_number = "binding_readback", None
        _verify_binding_receipts(journal.rows, steps, adapter)
        try:
            trace.stage = "postflight"
            adapter.preflight(steps, frozenset(completed))
        except Exception as error:
            raise ExecutionStopped("postflight_failed", cause=error) from None
        _assert_plan_unchanged(steps, plan_hash)
        return {"schema_version": 1, "plan_sha256": plan_hash,
                "verified_steps": len(completed), "step_count": len(steps)}
