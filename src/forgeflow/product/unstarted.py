"""Explicitly retire an approved product plan only if it never dispatched.

This is a separate product control action, not read-only reconciliation. It
never opens an ERP connection, issues approval, runs a verifier or calls apply.
The same root scope lock prevents late apply requests from crossing retirement.
"""
from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.approval import NONCE, Approval
from forgeflow.contract import contract_from_payload
from forgeflow.planning.objectives import bound_objective
from forgeflow.product.reconciliation import _journal
from forgeflow.recovery.journal import plan_digest
from forgeflow.recovery.product_constraints import load_constraints
from forgeflow.recovery.product_versions import ProductVersions, VersionError, owner_scope
from forgeflow.recovery.scope_guard import ScopeGuard
from forgeflow.recovery.service import ARTIFACTS, checked_id, verify_sources
from forgeflow.recovery.storage import digest, read_json, scope_lock, sync_directory


def need(value, code="product_cancellation_unconfirmed"):
    if not value:
        raise VersionError(code)


def _exists(path):
    # Broken links are evidence of corruption, never absence.
    return os.path.lexists(path)


def _journal_proof(path, guard):
    if not _exists(path):
        return {"sha256": None, "rows": 0}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        if info.st_size == 0:
            return {"sha256": hashlib.sha256(b"").hexdigest(), "rows": 0}
    finally:
        os.close(fd)
    rows, sha = _journal(path)
    need(len(rows) == 1 and rows[0]["event"] == "plan", "product_dispatch_present")
    need(rows[0]["data"] == {"schema_version": 1, "plan_sha256": plan_digest(guard.steps),
                             "step_count": len(guard.steps)})
    return {"sha256": sha, "rows": 1}


def _clear_own_marker(store, plan_id):
    marker = store / "active-plan.json"
    if _exists(marker):
        value = read_json(marker)
        need(isinstance(value, dict) and set(value) == {"plan_id"})
        checked_id(value["plan_id"])
        if value["plan_id"] == plan_id:
            marker.unlink()
            sync_directory(store)


def cancel_unstarted(*, run_id, owner_sha256, plan_id, approval, store, bundle, approvals):
    """Return an immutable cancellation receipt or refuse without dispatching."""
    checked_id(plan_id)
    fields = {"nonce", "run_id", "plan_id", "policy_sha256", "snapshot_sha256",
              "approver", "issued_at_utc", "expires_at_utc"}
    need(isinstance(approval, dict) and set(approval) == fields, "product_approval_mismatch")
    token = Approval(**approval)
    need(isinstance(token.nonce, str) and NONCE.fullmatch(token.nonce), "product_approval_mismatch")
    # Expiry cannot prevent invalidating a never-dispatched grant. Its exact
    # original bytes and actor are still required, including expired timestamps.
    need(token.run_id == run_id and token.plan_id == plan_id
         and owner_scope(token.approver) == owner_sha256, "product_approval_mismatch")
    store, bundle, approvals = Path(store), Path(bundle), Path(approvals)
    registry = ProductVersions(store)
    with scope_lock(store):
        value = registry._read(run_id, owner_sha256)
        need(value is not None, "product_decision_missing")
        row = value["plans"].get(plan_id)
        need(isinstance(row, dict), "product_binding_changed")
        prior = row.get("unstarted_cancellation")
        if prior is None:
            need(value["selected_plan_id"] == plan_id and row["state"] == "selected",
                 "product_plan_not_selected")
            need(value["state"] in {"approved", "executing"}, "product_dispatch_present")
            need((value.get("approval") or {}).get("token") == token.payload(), "product_approval_mismatch")
        directory = store / plan_id
        need(directory.is_dir() and not directory.is_symlink())
        manifest = read_json(directory / "manifest.json")
        need(digest(manifest) == plan_id and manifest.get("product") == row["product"]
             and manifest.get("sources") == verify_sources(bundle), "product_binding_changed")
        artifacts = {name: read_json(directory / f"{name}.json") for name in ARTIFACTS}
        need(manifest["artifacts"] == {name: digest(data) for name, data in artifacts.items()},
             "product_binding_changed")
        load_constraints(directory, manifest, artifacts["request"])
        bound_objective(manifest, artifacts["plan"])
        from forgeflow.recovery.product_costs import load_cost_report

        load_cost_report(directory, manifest, inputs=tuple(
            artifacts[name] for name in ("request", "snapshot", "blueprint", "change-set")))
        policy = contract_from_payload(read_json(directory / "product-policy.json")).digest()
        need(policy == manifest.get("policy_sha256") == row["policy_sha256"] == token.policy_sha256
             and manifest["artifacts"]["snapshot"] == token.snapshot_sha256, "product_binding_changed")
        need(not _exists(directory / "success.json"), "product_dispatch_present")
        guard = ScopeGuard(artifacts["snapshot"], artifacts["request"], artifacts["change-set"],
                           artifacts["baseline"]["payload"]["workorders"], artifacts["baseline"])
        need(guard.binding == artifacts["binding"], "product_binding_changed")
        journal = _journal_proof(directory / "journal.jsonl", guard)
        binding = {"run_id": run_id, "plan_id": plan_id, "policy_sha256": policy,
                   "snapshot_sha256": token.snapshot_sha256, "approval_sha256": token.digest(),
                   "journal_sha256": journal["sha256"]}
        if prior is not None:
            need(isinstance(prior, dict) and prior.get("sha256") == digest(prior.get("payload"))
                 and prior["payload"].get("binding") == binding
                 and row["state"] == "retired" and row.get("retired_reason") == "unstarted_cancelled")
            receipt = prior
        else:
            need(approvals.is_dir() and not approvals.is_symlink())
            need(read_json(approvals / f"approval-{token.nonce}.json") == token.payload(), "product_approval_mismatch")
            used = approvals / f"approval-{token.nonce}.used"
            if _exists(used):
                # consume occurs after the durable plan row. A missing/empty
                # journal with a spent token is corruption, not zero dispatch.
                need(journal["rows"] == 1)
                fd = os.open(used, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 200)
                    need(os.read(fd, 201).decode("ascii").strip() == token.digest())
                finally:
                    os.close(fd)
            else:
                need(value["state"] == "approved")
            # Validate before committing; remove only after retirement is
            # durably visible to every later apply request.
            active = store / "active-plan.json"
            if _exists(active):
                marker = read_json(active)
                need(isinstance(marker, dict) and set(marker) == {"plan_id"})
                checked_id(marker["plan_id"])
            proof = {"schema_version": 1, "binding": binding, "outcome": "not_started",
                     "approval_invalidated": True, "plan_retired": True,
                     "observed_at": datetime.now(UTC).isoformat()}
            receipt = {"payload": proof, "sha256": digest(proof)}
            registry._retire(value, "unstarted_cancelled")
            row["unstarted_cancellation"] = receipt
            value.update(state="retired", revision=value["revision"] + 1)
            registry._save(value)
        _clear_own_marker(store, plan_id)
        return {key: receipt["payload"][key] for key in (
            "outcome", "approval_invalidated", "plan_retired", "observed_at")} | {
                "run_id": run_id, "plan_id": plan_id, "policy_sha256": policy,
                "snapshot_sha256": token.snapshot_sha256, "receipt_sha256": receipt["sha256"],
            }
