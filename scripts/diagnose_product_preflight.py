"""Read-only preflight diagnosis for an explicitly named acceptance plan.

Requires a managed revision-acceptance sandbox. Starts it through the existing
launcher, validates the original artifacts and recorded journal prefix, then calls
only the original scope guard through the existing read-only ERP connection.
Never calls apply, approval, solve, verifier, cancellation or journal append.
Only exception types, allowlisted reasons and project source locations escape.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import stat
import subprocess
import sys
import traceback
from pathlib import Path

from forgeflow.executor.main import ExecutorConfig, connection_factory
from forgeflow.product.reconciliation import ReadOnlyConnection, _journal, _readonly_adapter
from forgeflow.product.unstarted import _journal_proof
from forgeflow.recovery.journal import _history, _verify_binding_receipts, plan_digest
from forgeflow.recovery.product_versions import VersionError
from forgeflow.recovery.scope_guard import EXTRA_FIELDS, ScopeError, ScopeGuard
from forgeflow.recovery.service import ARTIFACTS, checked_id, verify_sources
from forgeflow.recovery.storage import digest, read_json, scope_lock

ROOT = Path(__file__).resolve().parents[1]


def legacy_apply_reasons(path):
    """Project only reviewed fixed reasons from the old sandbox-wide log.

    This log has no run/plan binding. Never present it as a per-plan receipt or
    disclose its exception prose, traceback or unknown strings.
    """
    allowed = {"mutation_outcome_unknown", "readback_failed", "preflight_failed",
               "postflight_failed", "unresolved_previous_dispatch", "precondition_conflict",
               "mutation_not_verified", "completed_step_drift", "executor_shutting_down"}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 1024 * 1024,
             "legacy_log_unavailable")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(1024 * 1024 + 1)
    finally:
        os.close(fd)
    need(len(raw) <= 1024 * 1024 and (not raw or raw.endswith(b"\n")), "legacy_log_unavailable")
    counts = {}
    for line in raw.splitlines():
        entry = json.loads(line)
        need(isinstance(entry, dict), "legacy_log_unavailable")
        if entry.get("op") != "apply":
            continue
        reason = entry.get("reason")
        reason = reason if isinstance(reason, str) and reason in allowed else "unclassified"
        counts[reason] = counts.get(reason, 0) + 1
    return {"scope": "sandbox_log_without_run_or_plan_binding", "reason_counts": counts}


def need(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--offline-records-only", action="store_true",
                        help="only fixed legacy error reasons; never start services or read ERP")
    args = parser.parse_args()
    checked_id(args.plan_id)
    need(not args.out.exists(), "evidence_already_exists")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    record = launcher["_instance"]()
    need(launcher["_revision_capability"](record), "revision_capability_required")
    sandbox = launcher["SANDBOX"]
    result = {"scope": "readonly_original_preflight_diagnosis", "plan_id": args.plan_id,
              "diagnostic_called_apply": False, "diagnostic_called_approval": False,
              "diagnostic_called_verifier": False, "status": "running"}
    if args.offline_records_only:
        result.pop("plan_id")
        result.update(scope="offline_sandbox_error_log_projection", plan_binding_verified=False)
        try:
            result["legacy_apply_errors"] = legacy_apply_reasons(sandbox / "executor-errors.jsonl")
            result.update(status="offline_records_projected", services_started=False)
        except Exception as error:
            result.update(status="diagnosis_unavailable", error_type=type(error).__name__)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "offline_records_projected" else 1
    try:
        launcher["resume"]()
        with scope_lock(sandbox / "store"):
            directory = sandbox / "store" / args.plan_id
            manifest = read_json(directory / "manifest.json")
            need(digest(manifest) == args.plan_id
                 and manifest["sources"] == verify_sources(sandbox / "planner"), "original_binding_changed")
            artifacts = {name: read_json(directory / f"{name}.json") for name in ARTIFACTS}
            need(manifest["artifacts"] == {name: digest(value) for name, value in artifacts.items()},
                 "original_artifacts_changed")
            guard = ScopeGuard(artifacts["snapshot"], artifacts["request"], artifacts["change-set"],
                               artifacts["baseline"]["payload"]["workorders"], artifacts["baseline"])
            need(guard.binding == artifacts["binding"], "original_guard_changed")
            journal = directory / "journal.jsonl"
            try:
                proof = _journal_proof(journal, guard)
                rows = [] if proof["rows"] == 0 else _journal(journal)[0]
            except VersionError as error:
                if str(error) != "product_dispatch_present":
                    raise
                rows, _sha = _journal(journal)
            started, completed = _history(rows, guard.steps, plan_digest(guard.steps))
            result["journal"] = {"rows": len(rows), "started_steps": len(started),
                                 "completed_steps": len(completed), "planned_steps": len(guard.steps),
                                 "dispatch_records": sum(row["event"] == "dispatch" for row in rows),
                                 "pending_dispatch": bool(started - completed),
                                 "started_kinds": [step.kind for step in guard.steps if step.key in started]}
            config = ExecutorConfig(socket_path=Path(launcher["SOCKET"]), store=sandbox / "store",
                                    bundle=sandbox / "planner", key_path=sandbox / "api_key",
                                    agent_user=os.environ.get("USER", "user"), port=launcher["ODOO_PORT"])
            reader = ReadOnlyConnection(connection_factory(config)())
            adapter = _readonly_adapter(reader, guard, directory)
            _verify_binding_receipts(rows, guard.steps, adapter)
            try:
                guard(adapter, guard.steps, frozenset(completed))
                result["status"] = "preflight_passed_now"
            except Exception as error:
                result.update(status="preflight_refused_now", error_type=type(error).__name__)
                reason = str(error) if isinstance(error, ScopeError) else "unclassified_preflight_refusal"
                fixed = {"manufacturing_projection_drift", "purchase_projection_drift",
                         "physical_stock_drift", "guard_inputs_changed", "untrusted_or_changed_steps",
                         "scope_projection_conflict", "incomplete_extended_read", "invalid_read_ids",
                         "missing_original_or_generated_record", "invalid_search_ids"}
                qualified = {prefix + ":" + model for prefix in (
                    "immutable_scope_drift", "extra_or_missing_document", "physical_execution") for model in EXTRA_FIELDS}
                result["reason"] = reason if reason in fixed | qualified else "unclassified_preflight_refusal"
                frames = []
                for frame in traceback.extract_tb(error.__traceback__):
                    path = Path(frame.filename)
                    if path.is_relative_to(ROOT) and path.relative_to(ROOT).parts[0] in {"src", "scripts"}:
                        frames.append({"file": str(path.relative_to(ROOT)), "line": frame.lineno,
                                       "function": frame.name})
                result["project_frames"] = frames
    except Exception as error:
        result.update(status="diagnosis_unavailable", error_type=type(error).__name__)
        if isinstance(error, RuntimeError) and re.fullmatch(r"[a-z_]+", str(error)):
            result["reason"] = str(error)
    finally:
        table_file = args.out.with_name(args.out.stem + "-tables.json")
        try:
            capture = subprocess.run([sys.executable, str(ROOT / "scripts/product_acceptance_snapshot.py"),
                                      "--container", launcher["CONTAINER"], "--label", "readonly-diagnostic",
                                      "--out", str(table_file)], cwd=ROOT, capture_output=True, timeout=120)
            result["table_snapshot_saved"] = capture.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            result["table_snapshot_saved"] = False
        try:
            launcher["stop"]()
            result["preserving_stop_completed"] = True
        except Exception as error:
            result.update(preserving_stop_completed=False, cleanup_error_type=type(error).__name__)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] != "diagnosis_unavailable" and result["preserving_stop_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
