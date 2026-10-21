"""Product-only recovery of a missing original-verifier receipt.

This module is deliberately absent from benchmark bundles. The host enables it
explicitly, with a fixed verifier runner; callers supply only the original run
and plan IDs. ERP access is read-only and the executor's scope lock spans both
fresh scope checks and the verifier process. No prepare, approval or apply path
is called here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.approval import NONCE, Approval, used_digest
from forgeflow.contract import check_changes, check_request, contract_from_payload
from forgeflow.planning.objectives import bound_objective
from forgeflow.recovery.change_set import validate_projection
from forgeflow.recovery.journal import (
    MAX_JOURNAL_BYTES,
    _digest,
    _history,
    _verify_binding_receipts,
    plan_digest,
)
from forgeflow.recovery.odoo import PlanDocuments
from forgeflow.recovery.product_constraints import load_constraints
from forgeflow.recovery.readback import ReadbackAdapter, matches
from forgeflow.recovery.scope_guard import ScopeGuard, _read_scope
from forgeflow.recovery.service import ARTIFACTS, ExecutionGrant, checked_id, verify_sources
from forgeflow.recovery.storage import atomic_json, digest, read_json, scope_lock


class ReconciliationError(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise ReconciliationError(code)


class ReadOnlyConnection:
    def __init__(self, connection):
        self.connection = connection

    def get_model(self, model):
        proxy = self.connection.get_model(model)

        class Reader:
            def __getattr__(self, method):
                if method not in {"read", "search", "fields_get"}:
                    raise ReconciliationError("reconciliation_write_forbidden")
                return getattr(proxy, method)

        return Reader()


def _journal(path):
    """Validate the original hash-linked journal without creating/appending it."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
             and info.st_size <= MAX_JOURNAL_BYTES, "invalid_journal_file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_JOURNAL_BYTES + 1)
    finally:
        os.close(fd)
    need(raw and len(raw) <= MAX_JOURNAL_BYTES and raw.endswith(b"\n"),
         "incomplete_journal_record")
    rows, previous = [], "0" * 64
    for index, line in enumerate(raw.splitlines()):
        row = json.loads(line)
        need(isinstance(row, dict) and set(row) == {
            "index", "previous", "event", "data", "sha256",
        }, "invalid_journal_record")
        unsigned = {key: row[key] for key in ("index", "previous", "event", "data")}
        need(type(row["index"]) is int and row["index"] == index
             and row["previous"] == previous and row["sha256"] == _digest(unsigned),
             "journal_integrity_failure")
        previous = row["sha256"]
        rows.append(row)
    return rows, hashlib.sha256(raw).hexdigest()


def _used_approval(approvals, run_id, plan_id, policy, snapshot):
    need(approvals.is_dir() and not approvals.is_symlink(), "approval_not_found")
    candidates = []
    for path in approvals.glob("approval-*.json"):
        payload = read_json(path, 64 * 1024)
        if not isinstance(payload, dict) or payload.get("run_id") != run_id or payload.get("plan_id") != plan_id:
            continue
        nonce = payload.get("nonce")
        need(isinstance(nonce, str) and NONCE.fullmatch(nonce) is not None
             and path.name == f"approval-{nonce}.json" and payload.get("schema_version") == 1,
             "invalid_execution_grant")
        approval = Approval(**{k: v for k, v in payload.items() if k != "schema_version"})
        need(payload == approval.payload(), "invalid_execution_grant")
        if used_digest(approvals, approval) is None:
            continue
        need(approval.policy_sha256 == policy and approval.snapshot_sha256 == snapshot
             and used_digest(approvals, approval) == approval.digest(), "approval_token_tampered")
        candidates.append(approval)
    need(len(candidates) == 1, "consumed_product_approval_required")
    return candidates[0]


def _readonly_adapter(connection, guard, directory):
    # ReadbackAdapter.__init__ creates binding directories. Reconciliation may
    # read only existing bindings, so construct the same reader without mkdir.
    adapter = ReadbackAdapter.__new__(ReadbackAdapter)
    adapter.connection = connection
    adapter.documents = PlanDocuments(connection)
    adapter.scope_guard = guard
    adapter.plan_sha256 = plan_digest(guard.steps)
    parent = directory / "bindings"
    adapter.binding_directory = parent / adapter.plan_sha256
    need(parent.is_dir() and not parent.is_symlink()
         and adapter.binding_directory.is_dir() and not adapter.binding_directory.is_symlink(),
         "binding_receipt_unavailable")
    return adapter


def _start_verification(directory, pending_path, bindings):
    """Resume only a durably proved pre-verifier failure, never an unknown child.

    The fixed host runner returns this exact not-run code before launching the
    checker. Preserve its prior attempt and pending record across an executor
    configuration/code repair. The actual plan, approval, journal and fresh ERP
    bindings must still match; an old completed receipt never takes this path.
    """
    pending = {"bindings": bindings, "started_at": datetime.now(UTC).isoformat()}
    if pending_path.exists():
        previous = read_json(pending_path)
        old_bindings = previous.get("bindings", {})
        # A host-only reconciliation repair may change this module's digest.
        # Every approved/executed business binding remains exact.
        def comparable(value):
            return {k: v for k, v in value.items() if k != "reconciliation_source_sha256"}
        need(isinstance(old_bindings, dict)
             and comparable(old_bindings) == comparable(bindings)
             and isinstance(old_bindings.get("reconciliation_source_sha256"), str)
             and re.fullmatch(r"[0-9a-f]{64}", old_bindings["reconciliation_source_sha256"])
             and isinstance(previous.get("started_at"), str), "product_verifier_outcome_unknown")
        attempts = []
        for path in directory.glob("product-verification-attempt-*.json"):
            attempt = read_json(path)
            need(path.name == "product-verification-attempt-" + digest(attempt) + ".json",
                 "product_verifier_outcome_unknown")
            if (attempt.get("bindings") == old_bindings
                    and isinstance(attempt.get("attempted_at"), str)
                    and attempt["attempted_at"] >= previous["started_at"]):
                attempts.append(attempt)
        need(len(attempts) == 1, "product_verifier_outcome_unknown")
        proof = attempts[0].get("verifier", {})
        need(proof.get("status") == "not_run"
             and proof.get("error") == "verifier_output_not_isolated"
             and proof.get("evaluation_valid") is None
             and proof.get("strict_success") is None, "product_verifier_outcome_unknown")
        prior_sha = digest(previous)
        archive = directory / ("product-verification-start-" + prior_sha + ".json")
        if archive.exists():
            need(read_json(archive) == previous, "product_verifier_outcome_unknown")
        else:
            atomic_json(archive, previous)
        pending.update(prior_pending_sha256=prior_sha,
                       retry_of_preflight_attempt_sha256=digest(attempts[0]))
        atomic_json(pending_path, pending, replace=True)
    else:
        atomic_json(pending_path, pending)


def reconcile_original(
    connection, run_id, plan_id, *, store, bundle, approvals, verifier,
):
    """Recover one original product result under the same sandbox scope lock."""
    need(isinstance(run_id, str) and re.fullmatch(r"local-[0-9a-f]{32}", run_id),
         "product_run_required")
    checked_id(plan_id)
    store, bundle, approvals = Path(store), Path(bundle), Path(approvals)
    need(callable(verifier), "product_verifier_unavailable")
    with scope_lock(store):
        directory = store / plan_id
        need(directory.is_dir() and not directory.is_symlink(), "missing_original_plan")
        active = store / "active-plan.json"
        if active.exists():
            need(read_json(active).get("plan_id") == plan_id, "different_active_plan")
        sources = verify_sources(bundle)
        manifest = read_json(directory / "manifest.json")
        need(digest(manifest) == plan_id and manifest["sources"] == sources,
             "original_source_binding_changed")
        artifacts = {name: read_json(directory / f"{name}.json") for name in ARTIFACTS}
        need(manifest["artifacts"] == {name: digest(value) for name, value in artifacts.items()},
             "original_plan_artifact_changed")
        request, snapshot, blueprint, plan, changes, baseline, binding = (artifacts[n] for n in ARTIFACTS)
        from forgeflow.recovery.product_costs import load_cost_report

        load_cost_report(directory, manifest, inputs=(request, snapshot, blueprint, changes))
        constraints = load_constraints(directory, manifest, request)
        objective = bound_objective(manifest, plan)
        validate_projection(changes, request, snapshot, blueprint, plan,
                            product_constraints=constraints, product_objective=objective)
        prepared = read_json(directory / "prepared.json")
        policy_path = directory / "product-policy.json" if "product" in manifest else store.parent / "policy.json"
        policy = contract_from_payload(read_json(policy_path))
        if "product" in manifest:
            need(manifest.get("policy_sha256") == policy.digest(), "approval_policy_mismatch")
        need(prepared.get("plan_id") == plan_id and prepared.get("policy_sha256") == policy.digest(),
             "approval_policy_mismatch")
        need(check_request(request, policy)["ok"] and check_changes(changes, policy)["ok"],
             "plan_contract_violation")
        approval = _used_approval(approvals, run_id, plan_id, policy.digest(), manifest["artifacts"]["snapshot"])
        if "product" in manifest:
            from forgeflow.recovery.product_versions import ProductVersions

            ProductVersions(store).assert_executable_locked(
                manifest, plan_id, ExecutionGrant(approval, run_id, approval.policy_sha256,
                                                 approval.snapshot_sha256, approval.approver),
            )
        guard = ScopeGuard(snapshot, request, changes, baseline["payload"]["workorders"], baseline)
        need(guard.binding == binding, "original_execution_binding_changed")
        rows, journal_sha = _journal(directory / "journal.jsonl")
        started, completed = _history(rows, guard.steps, plan_digest(guard.steps))
        need(started and completed == {s.key for s in guard.steps}, "original_execution_incomplete")
        reader = ReadOnlyConnection(connection)
        adapter = _readonly_adapter(reader, guard, directory)
        _verify_binding_receipts(rows, guard.steps, adapter)

        def fresh_readback():
            need(verify_sources(bundle) == sources, "source_bytes_changed")
            guard(adapter, guard.steps, frozenset(completed))
            observed = {}
            for token, expected in guard.compiled["final_expectations"].items():
                current = adapter.resolve(expected["identity"])
                need(current is not None and matches(current, expected["fields"]),
                     "final_document_readback_failed")
                observed[token] = current
            # Guard allows reservation movement; the verifier must nevertheless
            # evaluate one unchanged observed ERP snapshot while this lock is held.
            scope = _read_scope(reader, snapshot, request)
            return digest({"scope": scope, "documents": observed})

        observed_sha = fresh_readback()
        bindings = {
            "run_id": run_id, "case_id": run_id.removeprefix("local-"), "plan_id": plan_id,
            "policy_sha256": policy.digest(), "snapshot_sha256": manifest["artifacts"]["snapshot"],
            "journal_sha256": journal_sha, "approval_sha256": approval.digest(),
            "readback_sha256": observed_sha, "sources_sha256": digest(sources),
            "reconciliation_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        receipt_path = directory / "product-verification.json"
        pending_path = directory / "product-verification-pending.json"
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            need(isinstance(receipt, dict) and set(receipt) == {"payload", "sha256"}
                 and digest(receipt["payload"]) == receipt["sha256"]
                 and receipt["payload"].get("bindings") == bindings,
                 "product_verification_binding_changed")
        else:
            # A lost Docker CLI response does not prove the container process
            # stopped. Never start another verifier while an attempt's outcome
            # is unknown, even if the host API or executor has restarted.
            _start_verification(directory, pending_path, bindings)
            verified = verifier()
            need(isinstance(verified, dict), "invalid_product_verifier_result")
            verified = copy.deepcopy(verified)
            original_reward = verified.pop("_original_reward_json", None)
            need(fresh_readback() == observed_sha, "erp_changed_during_verification")
            need(_journal(directory / "journal.jsonl")[1] == journal_sha,
                 "journal_integrity_failure")
            if verified.get("status") in ("not_run", "unknown") and verified.get("evaluation_valid") is not True:
                attempt = {"bindings": bindings, "verifier": verified,
                           "attempted_at": datetime.now(UTC).isoformat()}
                atomic_json(directory / ("product-verification-attempt-" + digest(attempt) + ".json"), attempt)
                raise ReconciliationError("product_verifier_outcome_unknown")
            # Cache failures as well as passes: reconciliation must never become
            # an automatic rerun that erases a real original-verifier failure.
            payload = {"schema_version": 1, "bindings": bindings, "verifier": verified,
                       "verified_steps": len(completed), "verified_at": datetime.now(UTC).isoformat()}
            if original_reward is not None:
                need(isinstance(original_reward, str)
                     and hashlib.sha256(original_reward.encode()).hexdigest() == verified.get("reward_sha256"),
                     "invalid_product_verifier_result")
                payload["original_reward_json"] = original_reward
            receipt = {"payload": payload, "sha256": digest(payload)}
            atomic_json(receipt_path, receipt)
        payload = receipt["payload"]
        return {
            **{k: bindings[k] for k in ("run_id", "case_id", "plan_id", "policy_sha256", "snapshot_sha256", "readback_sha256")},
            "execution_validated": True, "verified_steps": len(completed),
            "verifier": copy.deepcopy(payload["verifier"]),
            "receipt_sha256": receipt["sha256"], "verified_at": payload["verified_at"],
        }
