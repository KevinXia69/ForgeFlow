"""Read-only execution evidence for recorded plans; never replay a journal.

Journal dispatch is a write-ahead record, not proof that an ERP write occurred.
Its hash chain detects corruption, not authenticity or current ERP state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

MAX_JOURNAL_BYTES = 8 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_:/.-]{0,199}")


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _journal(path: Path, boundary: Path, planned_steps: object) -> dict:
    result = {"journal_status": "unreadable", "dispatch_count": None,
              "verified_count": None, "pending_dispatch": None}
    try:
        relative = path.relative_to(boundary)
        cursor = boundary
        if cursor.is_symlink():
            return result
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                return result
        if not path.exists():
            return {**result, "journal_status": "missing"}
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOURNAL_BYTES:
                return result
            raw = handle.read(MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            return result
    except (OSError, ValueError):
        return result
    result["journal_status"] = "invalid"
    try:
        if raw and not raw.endswith(b"\n"):
            return result
        rows = [json.loads(line) for line in raw.splitlines()]
        previous = "0" * 64
        pending = None
        completed: set[str] = set()
        dispatches = 0
        steps = None
        for index, row in enumerate(rows):
            if type(row) is not dict or set(row) != {"index", "previous", "event", "data", "sha256"}:
                return result
            unsigned = {key: row[key] for key in ("index", "previous", "event", "data")}
            if (type(row["index"]) is not int or row["index"] != index
                    or row["previous"] != previous or row["sha256"] != _digest(unsigned)):
                return result
            previous = row["sha256"]
            data, event = row["data"], row["event"]
            if type(data) is not dict:
                return result
            if index == 0:
                if (event != "plan" or set(data) != {"schema_version", "plan_sha256", "step_count"}
                        or type(data["schema_version"]) is not int or data["schema_version"] != 1
                        or not isinstance(data["plan_sha256"], str)
                        or not SHA256.fullmatch(data["plan_sha256"])
                        or not _count(data["step_count"])):
                    return result
                steps = data["step_count"]
                if _count(planned_steps) and steps != planned_steps:
                    return result
                continue
            if (event not in {"dispatch", "verified", "reconciled", "already_applied"}
                    or set(data) not in ({"key", "step_sha256"},
                                         {"key", "step_sha256", "binding_sha256"})
                    or not isinstance(data["key"], str) or not KEY.fullmatch(data["key"])
                    or not isinstance(data["step_sha256"], str)
                    or not SHA256.fullmatch(data["step_sha256"])
                    or ("binding_sha256" in data and (
                        event == "dispatch" or not isinstance(data["binding_sha256"], str)
                        or not SHA256.fullmatch(data["binding_sha256"])))):
                return result
            identity = (data["key"], data["step_sha256"])
            if data["key"] in completed or len(completed) >= steps:
                return result
            if event == "dispatch" and pending is None:
                pending = identity
                dispatches += 1
            elif event in {"verified", "reconciled"} and pending == identity:
                completed.add(data["key"])
                pending = None
            elif event == "already_applied" and pending is None:
                completed.add(data["key"])
            else:
                return result
        status = "empty" if not rows else "plan_only" if len(rows) == 1 else "recorded"
        return {"journal_status": status, "dispatch_count": dispatches,
                "verified_count": len(completed), "pending_dispatch": pending is not None}
    except (ValueError, TypeError, KeyError, RecursionError):
        return result


def execution_projection(directory: Path, boundary: Path, prepared: dict | None,
                         success: dict | None, *, success_present: bool) -> dict:
    """Summarize recorded evidence, not execution authority or business success."""
    planned_steps = prepared.get("step_count") if isinstance(prepared, dict) else None
    journal = _journal(directory / "journal.jsonl", boundary, planned_steps)
    result = {"status": "unknown", "reason": "execution_result_unconfirmed",
              "execution_validated": None, **journal}
    if isinstance(prepared, dict) and prepared.get("plan_id") not in (None, directory.name):
        return {**result, "reason": "prepared_plan_mismatch"}
    if success is not None:
        if success.get("plan_id") != directory.name:
            return {**result, "reason": "success_plan_mismatch"}
        validated = success.get("execution_validated")
        if (success.get("ok") is True and validated is True
                and _count(success.get("verified_steps"))
                and (not _count(planned_steps) or success["verified_steps"] == planned_steps)):
            if journal["journal_status"] in {"invalid", "unreadable"}:
                return {**result, "reason": "success_journal_unreadable"}
            if journal["journal_status"] != "missing" and (
                    journal["pending_dispatch"] is True
                    or journal["verified_count"] != success["verified_steps"]):
                return {**result, "reason": "success_journal_conflict"}
            return {**result, "status": "succeeded", "reason": "recorded_execution_success",
                    "execution_validated": True}
        if validated is False:
            return {**result, "status": "stopped", "reason": "recorded_execution_stopped",
                    "execution_validated": False}
        return {**result, "reason": "success_record_incomplete"}
    if success_present:
        return {**result, "reason": "success_record_unreadable"}
    if journal["journal_status"] in {"invalid", "unreadable"}:
        return {**result, "reason": "journal_unreadable"}
    if journal["journal_status"] == "recorded":
        return {**result, "reason": "journal_without_success"}
    return {**result, "status": "not_recorded", "reason": "no_execution_result_recorded"}
