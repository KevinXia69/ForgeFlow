"""Prepare a fresh recovery change set, then execute its original durable plan.

The fixed CLI runs inside an isolated ERP task, using the task's Odoo credential.
No reference answer, task initializer, or benchmark verifier is packaged here.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import socket
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.approval import Approval, ApprovalError, used_digest
from forgeflow.contract import check_changes, check_request, contract_from_payload
from forgeflow.odoo_credentials import read_key
from forgeflow.planning.live import collect_snapshot, validate_request
from forgeflow.planning.objectives import bound_objective, canonical_objectives
from forgeflow.planning.tool import SolverFailure, solve_blueprint
from forgeflow.recovery.change_set import ChangeSetError, build_change_set, validate_projection
from forgeflow.recovery.failure import failure_projection
from forgeflow.recovery.journal import ExecutionStopped, Journal, execute, plan_digest
from forgeflow.recovery.margin import MARGIN_BASIS
from forgeflow.recovery.product_constraints import (
    ConstraintError,
    canonical_constraints,
    constrained_blueprint,
    load_constraints,
)
from forgeflow.recovery.product_costs import build_cost_report, load_cost_report
from forgeflow.recovery.readback import ReadbackAdapter, matches
from forgeflow.recovery.scope_guard import ScopeGuard, capture_execution_baseline
from forgeflow.recovery.storage import (
    StorageError,
    atomic_json,
    digest,
    read_json,
    scope_lock,
    sync_directory,
)

STORE = Path("/home/forgeflow/logs/recovery")
BUNDLE = Path(__file__).resolve().parents[2]
PLAN_ID = re.compile(r"[a-f0-9]{64}")
ARTIFACTS = ("request", "snapshot", "blueprint", "plan", "change-set", "baseline", "binding")


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionGrant:
    """An approval plus the bindings the executor must re-check itself.

    The product worker builds this from its own approval record, never from
    anything the agent sends.
    """

    approval: Approval
    run_id: str
    policy_sha256: str
    snapshot_sha256: str
    approver: str


def verify_sources(bundle: Path):
    provenance = read_json(bundle / "provenance.json")
    files = provenance.get("files_sha256", {})
    if provenance.get("recovery_enabled") is not True or not files:
        raise RecoveryError("recovery_bundle_required")
    for name, expected in files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RecoveryError("invalid_source_manifest")
        path = bundle / relative
        if path.is_symlink() or not path.is_file():
            raise RecoveryError("source_file_missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RecoveryError("source_bytes_changed")
    required = {"recover.py", "forgeflow/recovery/" + "service.py"}
    required |= {"forgeflow/recovery/" + n + ".py" for n in (
        "compiler", "readback", "scope_guard", "change_set", "journal", "odoo", "storage", "margin", "compatibility")}
    required |= {"forgeflow/approval.py", "forgeflow/contract.py",
                 "forgeflow/odoo_credentials.py"}
    required |= {"forgeflow/recovery/" + n + ".py" for n in ("client", "index")}
    required |= {"forgeflow/executor/" + n + ".py" for n in (
        "__init__", "bootstrap", "daemon", "handlers", "main", "peer", "protocol")}
    if not required <= files.keys():
        raise RecoveryError("incomplete_source_manifest")
    return provenance


def checked_id(value):
    if not isinstance(value, str) or not PLAN_ID.fullmatch(value):
        raise RecoveryError("invalid_plan_id")
    return value


def _pending_plan(store):
    marker = store / "active-plan.json"
    if not marker.exists():
        return None
    plan_id = checked_id(read_json(marker)["plan_id"])
    status = store / plan_id / "success.json"
    if status.exists() and read_json(status).get("execution_validated") is True:
        return None
    return plan_id


def _retention_profile(value):
    if value is not None:
        from forgeflow.planning.retention import RETENTION_PROFILE

        if value != RETENTION_PROFILE:
            raise RecoveryError("invalid_retention_profile")
    return value


def _refine(request, snapshot, blueprint, plan, bundle, profile, constraints=None, objective=None):
    if profile is None:
        return plan
    from forgeflow.planning.retention import refine_retention

    return refine_retention(request, snapshot, blueprint, plan, bundle / "upstream",
                            product_constraints=constraints, product_objective=objective)


def prepare(connection, request, *, store=STORE, bundle=BUNDLE, contract=None, retention_profile=None):
    request = validate_request(request)
    retention_profile = _retention_profile(retention_profile)
    if contract is not None:
        report = check_request(request, contract)
        if not report["ok"]:
            raise RecoveryError("task_contract_mismatch")
    with scope_lock(store):
        return _prepare_locked(connection, request, store=store, bundle=bundle, contract=contract,
                               retention_profile=retention_profile)


def _prepare_locked(connection, request, *, store, bundle, contract, product=None,
                    product_constraints=None, retention_profile=None):
    if _pending_plan(store):
        raise RecoveryError("unfinished_execution_requires_original_plan")
    sources = verify_sources(bundle)
    if product is not None and not {
        "forgeflow/recovery/product_versions.py", "forgeflow/recovery/product_constraints.py",
        "forgeflow/recovery/product_costs.py",
    } <= sources["files_sha256"].keys():
        raise RecoveryError("incomplete_source_manifest")
    snapshot = collect_snapshot(connection, request)
    blueprint = constrained_blueprint(snapshot, request, product_constraints)
    plan = solve_blueprint(blueprint, bundle / "upstream")
    plan = _refine(request, snapshot, blueprint, plan, bundle, retention_profile, product_constraints)
    changes = build_change_set(request, snapshot, blueprint, plan, product_constraints=product_constraints)
    validate_projection(changes, request, snapshot, blueprint, plan, product_constraints=product_constraints)
    if contract is not None:
        if not check_changes(changes, contract)["ok"]:
            raise RecoveryError("plan_contract_violation")
    baseline = capture_execution_baseline(connection, snapshot, request)
    return _persist_prepared(connection, request, snapshot, blueprint, plan, changes, baseline,
                             sources=sources, store=store, contract=contract, product=product,
                             product_constraints=product_constraints, retention_profile=retention_profile)


def _persist_prepared(connection, request, snapshot, blueprint, plan, changes, baseline, *,
                      sources, store, contract, product=None, product_constraints=None,
                      product_objectives=None, product_objective=None, retention_profile=None):
    _check_retention_binding(retention_profile, plan)
    guard = ScopeGuard(snapshot, request, changes, baseline["payload"]["workorders"], baseline)
    artifacts = dict(zip(ARTIFACTS, (
        request, snapshot, blueprint, plan, changes, baseline, guard.binding), strict=True))
    manifest = {"schema_version": 1, "sources": sources, "margin_basis": MARGIN_BASIS,
                "artifacts": {name: digest(value) for name, value in artifacts.items()}}
    if retention_profile is not None:
        manifest["retention_profile"] = retention_profile
    if product is not None:
        costs = build_cost_report(request, snapshot, blueprint, changes)
        manifest["product"] = product
        manifest["policy_sha256"] = contract.digest()
        manifest["product_constraints_sha256"] = digest(product_constraints)
        manifest["product_costs_sha256"] = digest(costs)
        if product_objectives is not None:
            manifest["product_objectives"] = product_objectives
            manifest["product_objective"] = product_objective
    plan_id = digest(manifest)
    directory = store / plan_id
    directory.mkdir(mode=0o700)
    sync_directory(store)
    for name, value in artifacts.items():
        atomic_json(directory / (name + ".json"), value)
    if product is not None:
        atomic_json(directory / "product-policy.json", contract.payload())
        atomic_json(directory / "product-constraints.json", product_constraints)
        atomic_json(directory / "product-costs.json", costs)
    atomic_json(directory / "manifest.json", manifest)
    (directory / "bindings").mkdir(mode=0o700)
    sync_directory(directory)
    adapter = ReadbackAdapter(connection, guard, directory / "bindings",
                              plan_sha256=plan_digest(guard.steps))
    adapter.preflight(guard.steps, frozenset())
    counts = Counter(op["action"] for op in changes["operations"])
    summary = {"ok": True, "odoo_writes": False, "plan_id": plan_id,
               "operation_counts": {name: counts[name] for name in ("keep", "update", "cancel", "create")},
               "step_count": len(guard.steps),
               "expected_new_spend": plan["plan"]["expected_new_spend"]}
    if contract is not None:
        summary["policy_sha256"] = contract.digest()
    if product_objectives is not None:
        summary["prepared_at"] = datetime.now(UTC).isoformat()
    atomic_json(directory / "prepared.json", summary)
    if contract is not None and product is None:
        atomic_json(Path(store).parent / "policy.json", contract.payload(), replace=True)
    return summary


def _prepare_candidates_locked(connection, request, *, store, bundle, contract, product,
                               product_constraints, product_objectives, retention_profile=None):
    from forgeflow.recovery.product_candidates import business_fingerprint

    if _pending_plan(store):
        raise RecoveryError("unfinished_execution_requires_original_plan")
    sources = verify_sources(bundle)
    required = {"forgeflow/recovery/" + name + ".py" for name in (
        "product_versions", "product_constraints", "product_costs", "product_candidates")}
    required.add("forgeflow/planning/objectives.py")
    if not required <= sources["files_sha256"].keys():
        raise RecoveryError("incomplete_source_manifest")
    snapshot = collect_snapshot(connection, request)
    blueprint = constrained_blueprint(snapshot, request, product_constraints)
    baseline = capture_execution_baseline(connection, snapshot, request)
    # Each solve sees a copy of exactly these facts. Later preflight reads can
    # invalidate the set; they never feed fresher facts into another candidate.
    audit = {"schema_version": 1, "product": product, "state": "preparing",
             "snapshot_sha256": digest(snapshot), "baseline_sha256": digest(baseline),
             "blueprint_sha256": digest(blueprint), "policy_sha256": contract.digest(),
             "constraints_sha256": digest(product_constraints),
             "product_objectives": product_objectives, "attempts": []}
    directory = store / "product-comparisons"
    if directory.is_symlink():
        raise RecoveryError("product_binding_changed")
    directory.mkdir(mode=0o700, exist_ok=True)
    sync_directory(store)
    path = directory / (product["comparison_id"] + ".json")
    atomic_json(path, audit)
    candidates, seen, guards = [], {}, []
    try:
        for objective in product_objectives:
            attempt = {"product_objective": objective, "state": "solving"}
            audit["attempts"].append(attempt)
            atomic_json(path, audit, replace=True)
            try:
                plan = solve_blueprint(copy.deepcopy(blueprint), bundle / "upstream",
                                       product_objective=objective)
                plan = _refine(request, snapshot, blueprint, plan, bundle, retention_profile,
                               product_constraints, objective)
            except SolverFailure as error:
                if str(error) != "planning_model_infeasible":
                    raise
                attempt.update(state="infeasible", reason="planning_model_infeasible", trace=error.trace)
                atomic_json(path, audit, replace=True)
                continue
            attempt.update(state="validating", plan=plan)
            atomic_json(path, audit, replace=True)
            try:
                changes = build_change_set(request, snapshot, blueprint, plan,
                    product_constraints=product_constraints, product_objective=objective)
                validate_projection(changes, request, snapshot, blueprint, plan,
                    product_constraints=product_constraints, product_objective=objective)
                if not check_changes(changes, contract)["ok"]:
                    raise RecoveryError("plan_contract_violation")
            except (ChangeSetError, ConstraintError) as error:
                if isinstance(error, ConstraintError) and str(error) != "product_constraint_not_satisfied":
                    raise
                reason = error.code if isinstance(error, ChangeSetError) else "product_constraint_not_satisfied"
                attempt.update(state="rejected", reason=reason)
                atomic_json(path, audit, replace=True)
                continue
            fingerprint = business_fingerprint(changes)
            attempt.update(business_sha256=fingerprint, changes=changes)
            if fingerprint in seen:
                attempt.update(state="duplicate", equivalent_plan_id=seen[fingerprint])
            else:
                prepared = _persist_prepared(connection, request, snapshot, blueprint, plan, changes,
                    baseline, sources=sources, store=store, contract=contract, product=product,
                    product_constraints=product_constraints, product_objectives=product_objectives,
                    product_objective=objective, retention_profile=retention_profile)
                plan_id = prepared["plan_id"]
                candidates.append(plan_id)
                seen[fingerprint] = plan_id
                guard = ScopeGuard(snapshot, request, changes, baseline["payload"]["workorders"], baseline)
                guards.append((plan_id, guard))
                attempt.update(state="prepared", plan_id=plan_id)
            atomic_json(path, audit, replace=True)
        if not candidates:
            # Failure of these two attempts is not a proof that no ERP repair exists.
            raise RecoveryError("product_candidates_unavailable")
        for plan_id, guard in guards:
            ReadbackAdapter(connection, guard, store / plan_id / "bindings",
                plan_sha256=plan_digest(guard.steps)).preflight(guard.steps, frozenset())
    except BaseException:
        audit["state"] = "failed"
        atomic_json(path, audit, replace=True)
        raise
    audit.update(state="prepared", candidate_plan_ids=candidates)
    atomic_json(path, audit, replace=True)
    return candidates


def prepare_product(connection, request, *, run_id, owner_sha256, expected_revision,
                    operation_id, constraints=None, store=STORE, bundle=BUNDLE, contract=None,
                    connection_factory=None, product_objectives=None, retention_profile=None):
    from forgeflow.recovery.product_versions import ProductVersions

    request = validate_request(request)
    if contract is None or not check_request(request, contract)["ok"]:
        raise RecoveryError("task_contract_mismatch")
    constraints = canonical_constraints(constraints, request)
    product_objectives = canonical_objectives(product_objectives)
    retention_profile = _retention_profile(retention_profile)
    inputs = {"request": request, "policy_sha256": contract.digest(), "constraints": constraints}
    if retention_profile is not None:
        inputs["retention_profile"] = retention_profile
    if product_objectives is not None:
        inputs["product_objectives"] = product_objectives

    def produce(context):
        # Replay of a completed root operation needs neither live ERP nor its
        # credentials. Only a genuinely new preparation opens the connection.
        active_connection = connection_factory() if connection_factory is not None else connection
        if product_objectives is not None:
            return _prepare_candidates_locked(active_connection, request, store=store, bundle=bundle,
                contract=contract, product=context, product_constraints=constraints,
                product_objectives=product_objectives, retention_profile=retention_profile)
        result = _prepare_locked(active_connection, request, store=store, bundle=bundle,
                                 contract=contract, product=context, product_constraints=constraints,
                                 retention_profile=retention_profile)
        return [result["plan_id"]]

    return ProductVersions(store).prepare(
        run_id, owner_sha256, expected_revision=expected_revision, operation_id=operation_id,
        inputs=inputs, produce=produce,
    )


def authorize_product(*, run_id, owner_sha256, expected_revision, plan_id, intent,
                      store=STORE, approval_store=None, now=None):
    from forgeflow.approval import APPROVER, MAX_TTL_SECONDS, NONCE, issue
    from forgeflow.recovery.product_versions import ProductVersions, VersionError, owner_scope

    required = {"nonce", "approver", "issued_at_utc", "expires_at_utc"}
    if not isinstance(intent, dict) or set(intent) != required:
        raise VersionError("product_invalid_request")
    if (not isinstance(intent["nonce"], str) or not NONCE.fullmatch(intent["nonce"])
            or not isinstance(intent["approver"], str) or not APPROVER.fullmatch(intent["approver"])
            or owner_scope(intent["approver"]) != owner_sha256):
        raise VersionError("product_owner_mismatch")
    try:
        issued = datetime.fromisoformat(intent["issued_at_utc"])
        expires = datetime.fromisoformat(intent["expires_at_utc"])
        moment = now or datetime.now(UTC)
        if issued.tzinfo is None or expires.tzinfo is None or moment.tzinfo is None:
            raise ValueError
        ttl = (expires - issued).total_seconds()
        if ttl != int(ttl) or not 0 < ttl <= MAX_TTL_SECONDS or issued > moment:
            raise ValueError
    except (TypeError, ValueError):
        raise VersionError("product_invalid_request") from None
    if moment >= expires:
        raise RecoveryError("approval_expired")

    manifest = read_json(Path(store) / checked_id(plan_id) / "manifest.json")
    if digest(manifest) != plan_id or manifest.get("margin_basis") != MARGIN_BASIS:
        # Fail before reserving an approval intent: an old unstarted comparison
        # must remain available for retirement and a fresh preparation.
        raise RecoveryError("margin_basis_requires_reprepare")

    def issue_once(binding, data):
        manifest = read_json(Path(store) / checked_id(plan_id) / "manifest.json")
        if digest(manifest) != plan_id or manifest.get("margin_basis") != MARGIN_BASIS:
            raise RecoveryError("margin_basis_requires_reprepare")
        return issue(
            Path(approval_store) if approval_store is not None else Path(store).parent / "approvals",
            run_id=run_id, plan_id=plan_id, policy_sha256=binding["policy_sha256"],
            snapshot_sha256=binding["snapshot_sha256"], approver=data["approver"],
            ttl_seconds=int(ttl), now=issued, nonce=data["nonce"],
        )

    return ProductVersions(store).authorize(
        run_id, owner_sha256, expected_revision=expected_revision, plan_id=plan_id,
        intent=intent, issue=issue_once,
    )


def _check_retention_binding(profile, plan):
    _retention_profile(profile)
    audit = plan.get("retention_refinement")
    if (profile is None and audit is not None) or (
        profile is not None and (not isinstance(audit, dict) or audit.get("profile") != profile)
    ):
        raise RecoveryError("retention_profile_binding_changed")
    if profile is not None:
        from forgeflow.planning.retention import validate_refinement_audit

        try:
            validate_refinement_audit(plan)
        except (KeyError, TypeError, ValueError):
            raise RecoveryError("retention_profile_binding_changed") from None


def apply(
    connection,
    plan_id,
    *,
    store=STORE,
    bundle=BUNDLE,
    grant: ExecutionGrant | None = None,
    require_grant: bool = False,
    require_product_version: bool = False,
    approval_store: Path | None = None,
    now: datetime | None = None,
    legacy_authorization: Approval | None = None,
):
    plan_id = checked_id(plan_id)
    with scope_lock(store):
        pending = _pending_plan(store)
        if pending and pending != plan_id:
            raise RecoveryError("unfinished_execution_requires_original_plan")
        directory = store / plan_id
        if directory.is_symlink() or not directory.is_dir():
            raise RecoveryError("missing_original_plan")
        sources = verify_sources(bundle)
        manifest = read_json(directory / "manifest.json")
        if digest(manifest) != plan_id:
            raise RecoveryError("original_source_binding_changed")
        legacy = manifest.get("margin_basis") != MARGIN_BASIS
        if legacy and legacy_authorization is None:
            raise RecoveryError("margin_basis_requires_reprepare")
        if not legacy and (manifest["sources"] != sources or legacy_authorization is not None):
            raise RecoveryError("original_source_binding_changed")
        if require_product_version and "product" not in manifest:
            raise RecoveryError("product_version_required")
        product_versions = product_decision = None
        if "product" in manifest:
            from forgeflow.recovery.product_versions import ProductVersions

            product_versions = ProductVersions(store)
            product_decision = product_versions.assert_executable_locked(manifest, plan_id, grant)
        if not (directory / "prepared.json").is_file():
            raise RecoveryError("plan_preparation_incomplete")
        artifacts = {name: read_json(directory / (name + ".json")) for name in ARTIFACTS}
        if manifest["artifacts"] != {name: digest(value) for name, value in artifacts.items()}:
            raise RecoveryError("original_plan_artifact_changed")
        request, snapshot, blueprint, plan, changes, baseline, binding = (
            artifacts[name] for name in ARTIFACTS)
        constraints = load_constraints(directory, manifest, request)
        objective = bound_objective(manifest, plan)
        _check_retention_binding(manifest.get("retention_profile"), plan)
        validate_projection(changes, request, snapshot, blueprint, plan,
                            product_constraints=constraints, product_objective=objective)
        load_cost_report(directory, manifest, inputs=(request, snapshot, blueprint, changes))
        policy_file = (directory / "product-policy.json" if product_versions is not None
                       else Path(store).parent / "policy.json")
        if product_versions is not None and not policy_file.exists():
            raise RecoveryError("approval_policy_mismatch")
        if policy_file.exists():
            if not _local_policy(policy_file):
                raise RecoveryError("policy_file_untrusted")
            contract = contract_from_payload(read_json(policy_file))
            policy_digest = contract.digest()
            if product_versions is not None and manifest.get("policy_sha256") != policy_digest:
                raise RecoveryError("approval_policy_mismatch")
            if not (directory / "prepared.json").is_file():
                raise RecoveryError("plan_preparation_incomplete")
            recorded = read_json(directory / "prepared.json").get("policy_sha256")
            if recorded != policy_digest:
                raise RecoveryError("approval_policy_mismatch")
            if not check_request(request, contract)["ok"] or not check_changes(changes, contract)["ok"]:
                raise RecoveryError("plan_contract_violation")
        guard = ScopeGuard(snapshot, request, changes, baseline["payload"]["workorders"], baseline)
        if guard.binding != binding:
            raise RecoveryError("original_execution_binding_changed")

        if legacy:
            from forgeflow.recovery.compatibility import consume_resume_authorization

            consume_resume_authorization(plan_id, legacy_authorization, store=store,
                bundle=bundle, grant=grant, now=now)

        if grant is None and require_grant:
            # The executor sets this so a bare plan_id can never dispatch.
            raise RecoveryError("approval_required")

        authorize_first_dispatch = None
        if grant is not None:
            if not isinstance(grant, ExecutionGrant):
                raise RecoveryError("invalid_execution_grant")
            if grant.approval.plan_id != plan_id:
                raise RecoveryError("approval_plan_mismatch")
            if grant.run_id != grant.approval.run_id:
                raise RecoveryError("approval_run_mismatch")
            if manifest["artifacts"]["snapshot"] != grant.snapshot_sha256:
                raise RecoveryError("approval_snapshot_mismatch")
            prepared_record = read_json(directory / "prepared.json")
            recorded_policy = prepared_record.get("policy_sha256")
            if recorded_policy is None:
                # A grant always binds an approval to a recorded policy hash;
                # a plan prepared without a contract cannot be approved.
                raise RecoveryError("approval_policy_mismatch")
            elif recorded_policy != grant.policy_sha256:
                raise RecoveryError("approval_policy_mismatch")
            if policy_file.exists():
                if digest(read_json(policy_file)) != grant.policy_sha256:
                    raise RecoveryError("approval_policy_mismatch")

            def authorize_first_dispatch():
                directory_of_tokens = (
                    Path(approval_store) if approval_store is not None
                    else Path(store).parent / "approvals"
                )
                try:
                    _consume_grant(directory_of_tokens, grant, plan_id, now)
                except ApprovalError as error:
                    code = str(error)
                    if (code == "approval_already_consumed"
                            and used_digest(directory_of_tokens, grant.approval)
                            == grant.approval.digest()):
                        # The previous attempt died after consuming the token
                        # but before writing a dispatch row. No ERP write could
                        # have happened, so the same approval may resume --
                        # but never after its expiry.
                        moment = now or datetime.now(UTC)
                        expires = datetime.fromisoformat(grant.approval.expires_at_utc)
                        if moment.astimezone(UTC) >= expires.astimezone(UTC):
                            raise ExecutionStopped("approval_expired") from None
                    else:
                        raise ExecutionStopped(code) from None
                if product_versions is not None:
                    product_versions.execution_state_locked(product_decision, "executing")

        if grant is not None and not (directory / "journal.jsonl").exists():
            moment = now or datetime.now(UTC)
            try:
                expires_at = datetime.fromisoformat(grant.approval.expires_at_utc)
            except (TypeError, ValueError):
                raise RecoveryError("invalid_execution_grant") from None
            if expires_at.tzinfo is None:
                raise RecoveryError("invalid_execution_grant")
            if moment.astimezone(UTC) >= expires_at.astimezone(UTC):
                raise RecoveryError("approval_expired")

        def preflight(adapter, steps, completed):
            if verify_sources(bundle) != sources:
                raise RecoveryError("source_bytes_changed")
            guard(adapter, steps, completed)

        adapter = ReadbackAdapter(connection, preflight, directory / "bindings",
                                  plan_sha256=plan_digest(guard.steps))
        # A failed or interrupted execution holds the scope for this exact plan.
        # A later call reconciles its journal instead of collecting a new baseline.
        atomic_json(store / "active-plan.json", {"plan_id": plan_id}, replace=True)
        journal_path = directory / "journal.jsonl"
        try:
            result = execute(
                guard.steps, adapter, journal_path,
                authorize_first_dispatch=authorize_first_dispatch,
            )
        except ExecutionStopped as error:
            error.failure = failure_projection({"schema_version": 1, "plan_id": plan_id,
                                                "context": error.context})
            if error.failure is not None:
                diagnostic = {"scope": "execution_failure_diagnostic_not_verifier",
                              "recorded_at": datetime.now(UTC).isoformat(),
                              "policy_sha256": manifest.get("policy_sha256"),
                              "snapshot_sha256": manifest["artifacts"]["snapshot"],
                              "failure": error.failure}
                try:
                    atomic_json(directory / ("execution-failure-" + digest(diagnostic) + ".json"), diagnostic)
                except (OSError, ValueError, StorageError):
                    pass  # Diagnostic storage must never turn a stopped execution into success.
            # A stale plan can fail its initial preflight without dispatching.
            # Release only when the validated journal proves that no step even
            # started; corruption or any dispatch retains the original scope.
            try:
                with Journal(journal_path) as history:
                    # Only a journal that proves no step started may release
                    # the scope: empty, or exactly the plan row. Any other row
                    # (including a dispatch) retains it.
                    no_dispatch = not history.rows or (len(history.rows) == 1 and
                        history.rows[0]["event"] == "plan" and history.rows[0]["data"] == {
                            "schema_version": 1, "plan_sha256": plan_digest(guard.steps),
                            "step_count": len(guard.steps),
                        })
                    if no_dispatch:
                        (store / "active-plan.json").unlink()
                        sync_directory(store)
            except Exception:
                pass
            raise
        observed = {}
        for token, expected in guard.compiled["final_expectations"].items():
            current = adapter.resolve(expected["identity"])
            if current is None or not matches(current, expected["fields"]):
                raise RecoveryError("final_document_readback_failed")
            observed[token] = current
        atomic_json(directory / ("readback-" + uuid.uuid4().hex + ".json"), observed)
        summary = {"ok": True, "odoo_writes": True, "plan_id": plan_id,
                   "verified_steps": result["verified_steps"], "execution_validated": True,
                   "benchmark_success": None}
        success = directory / "success.json"
        if success.exists():
            if read_json(success) != summary:
                raise RecoveryError("prior_execution_summary_changed")
        else:
            atomic_json(success, summary)
        if product_versions is not None:
            product_versions.execution_state_locked(product_decision, "executed")
        return summary


def _local_policy(path: Path) -> bool:
    """A policy file must be a regular, non-symlinked file under its directory."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_file() and not path.is_symlink() and info.st_size <= 64 * 1024


def _consume_grant(store: Path, grant: ExecutionGrant, plan_id: str, now) -> None:
    from forgeflow.approval import consume

    consume(
        store,
        grant.approval,
        run_id=grant.run_id,
        plan_id=plan_id,
        policy_sha256=grant.policy_sha256,
        snapshot_sha256=grant.snapshot_sha256,
        approver=grant.approver,
        now=now,
    )


PREPARE_FIELDS = ("ok", "odoo_writes", "plan_id", "operation_counts",
                  "step_count", "expected_new_spend")
APPLY_FIELDS = ("ok", "odoo_writes", "plan_id", "verified_steps",
                "execution_validated", "benchmark_success")


def _run_via_executor(socket_path: str, args, route_log: str) -> int:
    """Run prepare/apply through the executor socket and print tool summaries.

    The tool wrapper accepts exactly the six documented fields, so the
    response is projected here rather than forwarded verbatim.
    """
    from forgeflow.recovery import client

    def log(entry: dict) -> None:
        try:
            with open(route_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"op": args.mode, "mode": "executor",
                                         "socket_set": True, **entry}) + "\n")
        except OSError:
            pass

    run_id = os.environ.get("FORGEFLOW_RUN_ID") or socket.gethostname()
    try:
        if args.mode == "prepare":
            request = read_json(args.request, 64 * 1024)
            body = client.prepare(socket_path, run_id, request, timeout=300)
            summary = {key: body[key] for key in PREPARE_FIELDS}
        else:
            body = client.apply(socket_path, run_id, args.plan_id, None, timeout=900)
            summary = {key: body[key] for key in APPLY_FIELDS}
    except client.ExecutorError as error:
        failure = {
            "ok": False,
            "error_code": ("recovery_prepare_failed" if args.mode == "prepare"
                           else "recovery_execute_stopped"),
            "odoo_writes": args.mode == "apply",
        }
        if args.mode == "apply" and PLAN_ID.fullmatch(args.plan_id):
            failure["plan_id"] = args.plan_id
            failure["message"] = (
                "Execution did not confirm through this call. Preserve the plan_id; "
                "do not blindly retry or replan."
            )
        if args.mode == "prepare":
            failure["feedback"] = client.prepare_feedback(error)
        else:
            failure["reason"] = error.code
        log({"error": error.code, "reason": getattr(error, "reason", None)})
        print(json.dumps(failure))
        return 2
    log({"ok": True})
    print(json.dumps(summary))
    return 0


def _load_grant(path: Path) -> ExecutionGrant:
    payload = read_json(path, 64 * 1024)
    values = payload.get("approval") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        raise RecoveryError("invalid_execution_grant")
    fields = ("nonce", "run_id", "plan_id", "policy_sha256", "snapshot_sha256",
              "approver", "issued_at_utc", "expires_at_utc")
    if set(values) != set(fields):
        raise RecoveryError("invalid_execution_grant")
    approval = Approval(**{name: values[name] for name in fields})
    return ExecutionGrant(
        approval=approval,
        run_id=payload["run_id"],
        policy_sha256=payload["policy_sha256"],
        snapshot_sha256=payload["snapshot_sha256"],
        approver=payload["approver"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--request", type=Path, required=True)
    prepare_parser.add_argument("--retention-profile", choices=("fixed-totals-retention-v1",))
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--plan-id", required=True)
    apply_parser.add_argument("--autonomous", action="store_true",
                              help="benchmark path only: execute without an approval token")
    apply_parser.add_argument("--grant", type=Path, default=None,
                              help="execution grant JSON (approval + bindings)")
    apply_parser.add_argument("--legacy-authorization", type=Path,
                              help="operator-issued, current-review-bound recovery approval JSON")
    args = parser.parse_args(argv)
    if args.mode == "apply" and args.grant is None and not args.autonomous:
        print(json.dumps({"ok": False, "odoo_writes": False,
                          "error_code": "approval_required"}))
        return 2
    executor = os.environ.get("FORGEFLOW_EXECUTOR_SOCKET")
    route_log = os.environ.get("FORGEFLOW_ROUTE_LOG") or "/home/forgeflow/logs/recovery-route.jsonl"

    def log_route(entry: dict) -> None:
        try:
            with open(route_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"op": args.mode, "mode": "executor" if executor else "local",
                                         "socket_set": bool(executor), **entry}) + "\n")
        except OSError:
            pass

    log_route({})
    if executor:
        if getattr(args, "legacy_authorization", None) or getattr(args, "retention_profile", None):
            parser.error("operator recovery and retention configuration must use the trusted executor interface")
        # The permission profile keeps the write key out of this process, so
        # prepare and apply both go through the root executor.
        return _run_via_executor(executor, args, route_log)
    logging.disable(logging.CRITICAL)
    try:
        import odoolib
        connection = odoolib.get_connection(
            hostname="127.0.0.1", protocol="json2", port=8069, database="bench",
            password=read_key(),
        )
        if args.mode == "prepare":
            result = prepare(connection, read_json(args.request, 64 * 1024),
                             retention_profile=args.retention_profile)
        else:
            grant = _load_grant(args.grant) if args.grant is not None else None
            legacy = None
            if args.legacy_authorization is not None:
                record = read_json(args.legacy_authorization, 64 * 1024)
                if record.pop("schema_version", None) != 1:
                    raise RecoveryError("invalid_execution_grant")
                legacy = Approval(**record)
            result = apply(connection, args.plan_id, grant=grant,
                           require_grant=not args.autonomous, legacy_authorization=legacy)
    except Exception as error:
        # Preserve a bounded local diagnostic without printing RPC bodies,
        # credentials, or arbitrary upstream exception messages to the model.
        diagnostic = {"phase": args.mode, "error_type": type(error).__name__}
        if (type(error).__module__.startswith("forgeflow.recovery.")
                and re.fullmatch(r"[a-z_]{1,100}", str(error))):
            diagnostic["error_code"] = str(error)
        try:
            STORE.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_json(STORE / ("failure-" + uuid.uuid4().hex + ".json"), diagnostic)
        except Exception:
            pass
        result = {"ok": False, "odoo_writes": args.mode == "apply",
                  "error_code": "recovery_prepare_failed" if args.mode == "prepare"
                  else "recovery_execute_stopped"}
        if args.mode == "apply" and PLAN_ID.fullmatch(args.plan_id):
            result["plan_id"] = args.plan_id
        print(json.dumps(result))
        return 2
    print(json.dumps(result))
    return 0
