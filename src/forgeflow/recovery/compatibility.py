"""Explicit operator review of legacy plans without rewriting their evidence.

This local operator interface is not exposed to model tools. New writes require
the corrected margin contract. A legacy dispatch can resume only after current
independent validation, identical compiled operations and a separate, expiring
authorization bound to the exact journal prefix and current source bundle.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from forgeflow.approval import Approval, consume, issue
from forgeflow.planning.objectives import bound_objective
from forgeflow.recovery.change_set import validate_projection
from forgeflow.recovery.journal import Journal, _history, plan_digest
from forgeflow.recovery.margin import MARGIN_BASIS
from forgeflow.recovery.product_constraints import load_constraints
from forgeflow.recovery.scope_guard import ScopeGuard
from forgeflow.recovery.storage import atomic_json, digest, read_json, scope_lock, sync_directory


def _need(condition, reason):
    if not condition:
        from forgeflow.recovery.service import RecoveryError

        raise RecoveryError(reason)


def _review_locked(plan_id, *, store, bundle):
    from forgeflow.recovery.service import ARTIFACTS, checked_id, verify_sources

    checked_id(plan_id)
    directory = Path(store) / plan_id
    _need(directory.is_dir() and not directory.is_symlink(), "missing_original_plan")
    manifest = read_json(directory / "manifest.json")
    _need(digest(manifest) == plan_id, "original_plan_artifact_changed")
    artifacts = {name: read_json(directory / f"{name}.json") for name in ARTIFACTS}
    _need(manifest["artifacts"] == {name: digest(data) for name, data in artifacts.items()},
          "original_plan_artifact_changed")
    sources = verify_sources(Path(bundle))
    report = {"schema_version": 1, "plan_id": plan_id, "margin_basis": MARGIN_BASIS,
              "original_sources_sha256": digest(manifest["sources"]),
              "current_sources_sha256": digest(sources),
              "snapshot_sha256": manifest["artifacts"]["snapshot"]}
    if (directory / "success.json").exists():
        success = read_json(directory / "success.json")
        _need(success.get("execution_validated") is True and success.get("plan_id") == plan_id,
              "prior_execution_summary_changed")
        return {**report, "state": "completed_history"}, manifest, None
    if manifest.get("margin_basis") == MARGIN_BASIS:
        return {**report, "state": "current_plan"}, manifest, None
    request, snapshot, changes = (artifacts[key] for key in ("request", "snapshot", "change-set"))
    baseline = artifacts["baseline"]
    guard = ScopeGuard(snapshot, request, changes, baseline["payload"]["workorders"], baseline)
    _need(guard.binding == artifacts["binding"], "original_execution_binding_changed")
    rows = []
    journal = directory / "journal.jsonl"
    if journal.exists() or journal.is_symlink():
        with Journal(journal) as history:
            rows = list(history.rows)
    started, completed = _history(rows, guard.steps, plan_digest(guard.steps))
    report.update(journal_sha256=digest(rows), steps_sha256=plan_digest(guard.steps),
                  remaining_steps_sha256=digest([step.record() for step in guard.steps
                                                  if step.key not in completed]),
                  completed_steps=len(completed), pending_dispatch=bool(started - completed))
    if not started and not completed:
        return {**report, "state": "requires_reprepare"}, manifest, guard
    try:
        constraints = load_constraints(directory, manifest, request)
        objective = bound_objective(manifest, artifacts["plan"])
        checked = validate_projection(changes, request, snapshot, artifacts["blueprint"],
            artifacts["plan"], product_constraints=constraints, product_objective=objective)
    except (KeyError, ValueError):
        return {**report, "state": "recovery_blocked", "reason": "corrected_projection_rejected"}, manifest, guard
    report.update(state="requires_recovery_authorization", checks=checked)
    return report, manifest, guard


def review_legacy_plan(plan_id, *, store, bundle):
    """Save an additive review; no RPC, original artifact or journal mutation."""
    store = Path(store)
    with scope_lock(store):
        report, _, _ = _review_locked(plan_id, store=store, bundle=bundle)
        review_id = digest(report)
        directory = store / plan_id / "compatibility-reviews"
        _need(not directory.is_symlink(), "original_plan_artifact_changed")
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{review_id}.json"
        if path.exists():
            _need(read_json(path) == report, "original_plan_artifact_changed")
        else:
            atomic_json(path, report)
        return {"review_id": review_id, **report}


def authorize_legacy_resume(plan_id, review_id, *, approver, store, bundle, now=None, ttl_seconds=900):
    """Operator approval of a concrete current review, never an old approval replay."""
    from forgeflow.recovery.product_versions import owner_scope
    from forgeflow.recovery.service import checked_id

    checked_id(review_id)
    store = Path(store)
    with scope_lock(store):
        report, manifest, _ = _review_locked(plan_id, store=store, bundle=bundle)
        _need(report["state"] == "requires_recovery_authorization", "legacy_recovery_not_resumable")
        _need(digest(report) == review_id and read_json(
            store / plan_id / "compatibility-reviews" / f"{review_id}.json") == report,
            "legacy_recovery_review_changed")
        if "product" in manifest:
            _need(owner_scope(approver) == manifest["product"]["owner_sha256"], "approval_approver_mismatch")
        return issue(store / plan_id / "compatibility-approvals", run_id=f"legacy-{plan_id}",
            plan_id=plan_id, policy_sha256=review_id, snapshot_sha256=report["snapshot_sha256"],
            approver=approver, now=now, ttl_seconds=ttl_seconds)


def consume_resume_authorization(plan_id, authorization, *, store, bundle, grant=None, now=None):
    """Called with the execution scope locked immediately before any resume."""
    _need(isinstance(authorization, Approval), "legacy_recovery_authorization_required")
    report, manifest, _ = _review_locked(plan_id, store=store, bundle=bundle)
    _need(report["state"] == "requires_recovery_authorization", "legacy_recovery_not_resumable")
    _need(authorization.policy_sha256 == digest(report), "legacy_recovery_review_changed")
    saved = read_json(Path(store) / plan_id / "compatibility-reviews" / f"{digest(report)}.json")
    _need(saved == report, "legacy_recovery_review_changed")
    if "product" in manifest:
        _need(grant is not None and grant.approver == authorization.approver, "approval_approver_mismatch")
    consume(Path(store) / plan_id / "compatibility-approvals", authorization,
        run_id=f"legacy-{plan_id}", plan_id=plan_id, policy_sha256=digest(report),
        snapshot_sha256=manifest["artifacts"]["snapshot"], approver=authorization.approver, now=now)


def retire_legacy_unstarted(plan_id, *, approver, store, bundle):
    """Retire a never-dispatched old product version and preserve its approval."""
    from forgeflow.recovery.product_versions import ProductVersions, owner_scope

    store = Path(store)
    with scope_lock(store):
        report, manifest, _ = _review_locked(plan_id, store=store, bundle=bundle)
        _need(report["state"] == "requires_reprepare", "legacy_recovery_not_unstarted")
        context = manifest.get("product")
        _need(isinstance(context, dict), "product_version_required")
        owner = owner_scope(approver)
        _need(owner == context["owner_sha256"], "approval_approver_mismatch")
        versions = ProductVersions(store)
        value = versions._read(context["run_id"], owner)
        _need(value is not None and plan_id in value["plans"], "product_binding_changed")
        row = value["plans"][plan_id]
        _need(row["state"] in {"selected", "candidate"}
              and row["version"] == value["version"] and row["product"] == context,
              "product_plan_retired")
        # A historical old plan must never retire a newer comparison, and an
        # undispatched sibling must not retire another candidate mid-execution.
        for candidate_id, candidate in value["plans"].items():
            if candidate["state"] == "retired":
                continue
            candidate_report, candidate_manifest, _ = _review_locked(
                candidate_id, store=store, bundle=bundle)
            _need(candidate["version"] == value["version"]
                  and candidate_manifest.get("product") == context
                  and candidate_report["state"] == "requires_reprepare",
                  "legacy_recovery_not_unstarted")
        receipt = {"schema_version": 1, "plan_id": plan_id, "review": report,
                   "decision_sha256": digest(value), "previous_approval": value.get("approval"),
                   "reason": "margin_basis_requires_reprepare"}
        path = store / plan_id / f"retirement-{digest(receipt)}.json"
        if not path.exists():
            atomic_json(path, receipt)
        # The whole old comparison is retired; every candidate used old semantics.
        versions._retire(value, "margin_basis_upgrade")
        value.update(state="retired", approval=None, revision=value["revision"] + 1)
        versions._save(value)
        marker = store / "active-plan.json"
        if marker.exists() and read_json(marker).get("plan_id") == plan_id:
            marker.unlink()
            sync_directory(store)
        return {"plan_id": plan_id, "status": "retired", "revision": value["revision"],
                "receipt_sha256": digest(receipt)}


def main(argv=None):
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("review", "authorize", "retire"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--review-id")
    parser.add_argument("--approver")
    args = parser.parse_args(argv)
    options = {"store": args.store, "bundle": args.bundle}
    if args.action == "review":
        result = review_legacy_plan(args.plan_id, **options)
    elif args.action == "retire":
        if not args.approver:
            parser.error("retire requires --approver")
        result = retire_legacy_unstarted(args.plan_id, approver=args.approver, **options)
    else:
        if not args.approver or not args.review_id:
            parser.error("authorize requires --approver and --review-id")
        result = authorize_legacy_resume(args.plan_id, args.review_id,
                                        approver=args.approver, **options).payload()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
