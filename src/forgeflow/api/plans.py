"""Read recorded recovery plans without trusting a caller-supplied path.

A plan lives either in the product publish directory (``run_dir/plans``) or
in the exported trial artifacts (``harbor/job/*/agent/recovery``). Both are
resolved by id only: the caller never supplies a path, and every component is
checked for symlinks before it is read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from forgeflow.recovery_evidence import execution_projection

PLAN_ID = re.compile(r"[0-9a-f]{64}")
MAX_ARTIFACT_BYTES = 512 * 1024


def _within(path: Path, boundary: Path) -> bool:
    """Reject symlinks in intermediate artifact directories as well as leaves."""
    try:
        relative = path.relative_to(boundary)
        cursor = boundary
        if cursor.is_symlink():
            return False
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                return False
        return True
    except (OSError, ValueError):
        return False


def _read(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _candidate_dirs(run_dir: Path, plan_id: str) -> list[Path]:
    if run_dir.is_symlink() or not run_dir.is_dir():
        return []
    candidates = [run_dir / "plans" / plan_id]
    job = run_dir / "harbor/job"
    if _within(job, run_dir) and job.is_dir():
        for trial in sorted(job.iterdir()):
            if trial.is_dir() and not trial.is_symlink():
                candidates.append(trial / "agent/recovery" / plan_id)
    return candidates


def find_plan_dir(run_dir: str | Path, plan_id: str) -> Path | None:
    """Return the one safe directory holding ``plan_id``, or ``None``."""
    run_dir = Path(run_dir)
    if not isinstance(plan_id, str) or PLAN_ID.fullmatch(plan_id) is None:
        return None
    found: list[Path] = []
    for candidate in _candidate_dirs(run_dir, plan_id):
        if not _within(candidate, run_dir):
            continue
        if candidate.is_dir() and (candidate / "prepared.json").is_file():
            found.append(candidate)
    return found[0] if len(found) == 1 else None


def read_plan(run_dir: str | Path, plan_id: str) -> dict[str, Any] | None:
    """Return a bounded, read-only projection of one plan."""
    directory = find_plan_dir(run_dir, plan_id)
    if directory is None:
        return None
    prepared = _read(directory / "prepared.json")
    if prepared is None:
        return None
    manifest = _read(directory / "manifest.json") or {}
    success = _read(directory / "success.json")
    binding = _read(directory / "binding.json") or {}
    execution = execution_projection(
        directory, Path(run_dir), prepared, success,
        success_present=(directory / "success.json").exists() or (directory / "success.json").is_symlink(),
    )
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    return {
        "plan_id": plan_id,
        "state": "prepared" if execution["status"] == "not_recorded" else execution["status"],
        "execution": execution,
        "policy_sha256": prepared.get("policy_sha256"),
        "snapshot_sha256": artifacts.get("snapshot"),
        "prepared": {
            "operation_counts": prepared.get("operation_counts"),
            "step_count": prepared.get("step_count"),
            "expected_new_spend": prepared.get("expected_new_spend"),
        },
        "success": success,
        "artifacts": artifacts,
        "binding": binding if isinstance(binding, dict) else {},
        "journal_present": execution["journal_status"] != "missing",
    }


def list_plans(run_dir: str | Path) -> list[dict[str, Any]]:
    """List every plan that can be resolved from this run, newest name first."""
    run_dir = Path(run_dir)
    ids: set[str] = set()
    candidate_roots = [run_dir / "plans"]
    job = run_dir / "harbor/job"
    if _within(job, run_dir) and job.is_dir():
        candidate_roots.extend(
            trial / "agent/recovery" for trial in sorted(job.iterdir())
            if _within(trial, run_dir) and trial.is_dir()
        )
    for candidate_root in candidate_roots:
        if not _within(candidate_root, run_dir) or not candidate_root.is_dir():
            continue
        for entry in candidate_root.iterdir():
            if entry.is_dir() and not entry.is_symlink() and PLAN_ID.fullmatch(entry.name):
                ids.add(entry.name)
    plans = [read_plan(run_dir, plan_id) for plan_id in sorted(ids, reverse=True)]
    return [plan for plan in plans if plan is not None]
