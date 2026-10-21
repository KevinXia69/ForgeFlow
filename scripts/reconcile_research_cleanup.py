"""Recheck only the recorded research residue after its host process has exited.

Stops proven-owned containers, never deletes them or reruns an agent/verifier.
Uses the project's normal runtime configuration without printing credentials.
"""
from __future__ import annotations

import json
from pathlib import Path

from forgeflow.research_containers import MECHANISM, quiesce_run, wait_host_group
from forgeflow.runner import _child_environment
from forgeflow.runtime_lifecycle import exclusive_lease, managed_process, read_record, write_record

ROOT = Path(__file__).resolve().parents[1]


def reconcile(root: Path) -> dict:
    state = root / ".runtime/lifecycle"
    with exclusive_lease(state / "heavy-operation.lock"):
        marker = state / "research-residue.json"
        residue = read_record(marker)
        if residue is None:
            return {"status": "no_residue", "quiescent": True}
        if residue.get("schema") != 1 or residue.get("mechanism") != MECHANISM:
            raise ValueError("Unsupported research residue record.")
        run_dir = Path(residue["run_dir"])
        if not run_dir.is_absolute() or run_dir.parent != root / "runs":
            raise ValueError("Research residue is outside this workspace's run directory.")
        if run_dir.is_symlink() or run_dir.parent.is_symlink():
            raise ValueError("Research residue must not traverse a symlink.")
        process, status = managed_process(run_dir / "host-harbor.json")
        if status in {"running", "pid_reused"}:
            raise ValueError("Recorded host identity has not safely exited; no container was changed.")
        if process is not None and not wait_host_group(process["identity"]["pid"]):
            raise ValueError("The recorded host group still has live members.")
        if process is None:
            # A launch may have been interrupted before its handshake record.
            # Do not turn missing process evidence into proof of exit.
            raise ValueError("Host launch ownership is missing; manual diagnosis is required.")
        report = quiesce_run(run_dir, _child_environment(root))
        write_record(run_dir / "host-cleanup-reconciliation.json", report)
        if report["quiescent"]:
            marker.unlink()
        return report


if __name__ == "__main__":
    try:
        result = reconcile(ROOT)
    except Exception as error:
        result = {"status": "refused", "quiescent": False, "error_type": type(error).__name__}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["quiescent"] else 1)
