"""Root-owned product selection and approval state, separate from benchmarks.

All transitions share recovery's ERP scope lock, including the apply check.
The API's current-card pointer is not an execution authorization. These local
files are a correctness protocol, not isolation from an equal-privilege host.
"""
from __future__ import annotations

import copy
import hashlib
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.approval import Approval
from forgeflow.planning.objectives import bound_objective
from forgeflow.planning.tool import SolverFailure
from forgeflow.recovery.change_set import CHANGE_SET_REASONS, ChangeSetError
from forgeflow.recovery.product_constraints import (
    canonical_constraints,
    check_no_relaxation,
    load_constraints,
)
from forgeflow.recovery.product_costs import load_cost_report
from forgeflow.recovery.storage import (
    StorageError,
    atomic_json,
    digest,
    read_json,
    scope_lock,
    sync_directory,
)

HASH = re.compile(r"[a-f0-9]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
STATES = {"preparing", "prepare_failed", "prepared", "selected", "retired",
          "approval_issuing", "approved", "executing", "executed"}
LOCKED_STATES = {"approval_issuing", "approved", "executing", "executed"}
# Fixed-width bindings keep the complete response below the 64 KiB wire limit.
# History is never silently deleted to make room for another version.
MAX_PLANS = 32
PREPARATION_FAILURES = CHANGE_SET_REASONS | frozenset({
    "prepare_failed", "planning_model_infeasible", "product_constraint_not_satisfied",
    "product_constraints_invalid", "product_constraints_must_tighten",
    "product_constraints_relaxed", "product_constraints_binding_changed",
    "unfinished_execution_requires_original_plan", "incomplete_source_manifest",
    "source_bytes_changed", "source_file_missing", "plan_contract_violation",
    "product_candidates_unavailable",
})


class VersionError(RuntimeError):
    pass


def _stamp():
    return datetime.now(UTC).isoformat()


def owner_scope(owner: str) -> str:
    if not isinstance(owner, str) or not owner:
        raise VersionError("product_owner_mismatch")
    return hashlib.sha256(owner.encode()).hexdigest()


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise VersionError("product_invalid_request")
    return value


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise VersionError("product_invalid_request")
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value <= 10000:
        raise VersionError("product_invalid_request")
    return value


class ProductVersions:
    def __init__(self, store: Path):
        self.store = Path(store)

    def _path(self, run_id: str) -> Path:
        _name(run_id)
        directory = self.store / "product-decisions"
        if directory.is_symlink():
            raise VersionError("product_decision_corrupt")
        return directory / f"{run_id}.json"

    def _read(self, run_id: str, owner: str | None = None) -> dict | None:
        path = self._path(run_id)
        if not path.exists() and not path.is_symlink():
            return None
        try:
            value = read_json(path, 512 * 1024)
            if (
                not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] not in (1, 2, 3)
                or value.get("run_id") != run_id or value.get("state") not in STATES
                or type(value.get("revision")) is not int or value["revision"] < 1
                or type(value.get("version")) is not int or value["version"] < 1
                or not HASH.fullmatch(value.get("owner_sha256", ""))
                or not isinstance(value.get("plans"), dict) or len(value["plans"]) > MAX_PLANS
                or not isinstance(value.get("last_operation"), dict)
            ):
                raise ValueError
            if value["schema_version"] >= 2:
                constraints = canonical_constraints(value["constraints"])
                if (constraints != value["constraints"]
                        or digest(constraints) != value["constraints_sha256"]):
                    raise ValueError
            elif {"constraints", "constraints_sha256"} & value.keys():
                raise ValueError
            if value["schema_version"] == 3:
                identity = value["task_identity"]
                if identity is not None and (not isinstance(identity, dict)
                    or set(identity) != {"request_sha256", "policy_sha256"}
                    or any(not HASH.fullmatch(item) for item in identity.values())):
                    raise ValueError
            elif "task_identity" in value:
                raise ValueError
            last = value["last_operation"]
            if last.get("kind") == "prepare" and (
                not NAME.fullmatch(last.get("id", ""))
                or not HASH.fullmatch(last.get("input_sha256", ""))
                or last.get("stage") not in {"pending", "completed", "failed"}
                or last.get("failure_reason", "prepare_failed") not in PREPARATION_FAILURES
            ):
                raise ValueError
            for plan_id, row in value["plans"].items():
                if (not HASH.fullmatch(plan_id) or not isinstance(row, dict)
                        or row.get("state") not in {"candidate", "selected", "retired"}
                        or type(row.get("version")) is not int
                        or not isinstance(row.get("product"), dict)):
                    raise ValueError
                if (row["product"].get("run_id") != run_id
                        or row["product"].get("owner_sha256") != value["owner_sha256"]
                        or row["product"].get("version") != row["version"]):
                    raise ValueError
                if row["state"] != "retired" and (
                    row["version"] != value["version"]
                    or row["product"].get("comparison_id") != value["comparison_id"]
                ):
                    raise ValueError
                for key in ("policy_sha256", "snapshot_sha256", "baseline_sha256", "sources_sha256",
                            "request_sha256"):
                    if not HASH.fullmatch(row.get(key, "")):
                        raise ValueError
                if "comparison_sha256" in row and not HASH.fullmatch(row["comparison_sha256"]):
                    raise ValueError
                if value.get("task_identity") is not None and any(
                    row[key] != expected for key, expected in value["task_identity"].items()
                ):
                    raise ValueError
            selected = value.get("selected_plan_id")
            if selected is not None and (selected not in value["plans"]
                                         or value["plans"][selected]["state"] != "selected"):
                raise ValueError
            chosen = {key for key, row in value["plans"].items() if row["state"] == "selected"}
            if chosen != (set() if selected is None else {selected}):
                raise ValueError
            if value["state"] in LOCKED_STATES | {"selected"} and selected is None:
                raise ValueError
            if value["state"] in {"preparing", "prepare_failed", "retired", "prepared"} and selected is not None:
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError, StorageError):
            raise VersionError("product_decision_corrupt") from None
        if owner is not None and value["owner_sha256"] != _hash(owner):
            raise VersionError("product_owner_mismatch")
        return value

    def _save(self, value: dict):
        path = self._path(value["run_id"])
        path.parent.mkdir(mode=0o700, exist_ok=True)
        sync_directory(self.store)
        value["updated_at"] = _stamp()
        atomic_json(path, value, replace=True)

    def _archive_failed_preparation(self, value: dict):
        """Keep the private failed reservation/solver trace before replacing it."""
        if (value.get("last_operation", {}).get("kind") != "prepare"
                or value["last_operation"].get("stage") not in {"pending", "failed"}):
            return
        directory = self._path(value["run_id"]).parent / f"{value['run_id']}-preparations"
        if directory.is_symlink():
            raise VersionError("product_decision_corrupt")
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"v{value['version']}.json"
        try:
            if path.exists() or path.is_symlink():
                if read_json(path, 512 * 1024) != value:
                    raise VersionError("product_decision_corrupt")
            else:
                atomic_json(path, value)
        except (OSError, StorageError, ValueError):
            raise VersionError("product_decision_corrupt") from None

    @staticmethod
    def _view(value: dict) -> dict:
        # Approval credentials never appear in the selection/status projection.
        view = {key: copy.deepcopy(value.get(key)) for key in (
            "run_id", "revision", "version", "state", "comparison_id",
            "selected_plan_id", "plans", "updated_at",
        )}
        for row in view["plans"].values():
            row.pop("unstarted_cancellation", None)
        last = value["last_operation"]
        if last.get("kind") == "prepare":
            view["preparation"] = {"operation_id": last["id"], "inputs_sha256": last["input_sha256"],
                                   "stage": last["stage"], "failure_reason": (
                                       last.get("failure_reason", "prepare_failed")
                                       if last["stage"] == "failed" else None)}
        return view

    def status(self, run_id: str, owner: str) -> dict:
        with scope_lock(self.store):
            value = self._read(run_id, _hash(owner))
            if value is None:
                raise VersionError("product_decision_missing")
            return self._view(value)

    @staticmethod
    def _cas(value: dict | None, expected: int):
        if (0 if value is None else value["revision"]) != _revision(expected):
            raise VersionError("product_revision_conflict")

    @staticmethod
    def _retire(value: dict, reason: str):
        for row in value["plans"].values():
            if row["state"] != "retired":
                row.update(state="retired", retired_reason=reason, retired_at=_stamp())
        value["selected_plan_id"] = None

    def prepare(self, run_id: str, owner: str, *, expected_revision: int,
                operation_id: str, inputs: dict, produce) -> dict:
        """Reserve a new version, then let trusted root code prepare candidates.

        `produce(context)` must already enforce the original contract and return
        only plans it wrote under this same lock. It never comes from the wire.
        A crash or failure retains the reservation and old plans' retirement.
        """
        _name(run_id), _hash(owner), _name(operation_id), _revision(expected_revision)
        constraints = canonical_constraints(inputs.get("constraints"), inputs.get("request"))
        identity = None
        if "request" in inputs or "policy_sha256" in inputs:
            if not isinstance(inputs.get("request"), dict):
                raise VersionError("product_invalid_request")
            identity = {"request_sha256": digest(inputs["request"]),
                        "policy_sha256": _hash(inputs.get("policy_sha256"))}
        input_sha = digest(inputs)
        with scope_lock(self.store):
            value = self._read(run_id, owner)
            if value is not None:
                last = value["last_operation"]
                if last.get("id") == operation_id:
                    if last.get("kind") != "prepare" or last.get("input_sha256") != input_sha:
                        raise VersionError("product_operation_conflict")
                    if last.get("stage") != "completed":
                        raise VersionError("product_preparation_interrupted")
                    return self._view(value)
                if value["state"] in LOCKED_STATES:
                    raise VersionError("product_already_authorized")
            self._cas(value, expected_revision)
            if value is not None:
                original = value.get("task_identity")
                if original is None and value["plans"]:
                    # Migrate a legacy successful version only from its
                    # recorded plan facts, never from a new caller's policy.
                    facts = {(row["request_sha256"], row["policy_sha256"]) for row in value["plans"].values()}
                    if len(facts) != 1:
                        raise VersionError("product_binding_changed")
                    request_sha, policy_sha = next(iter(facts))
                    original = {"request_sha256": request_sha, "policy_sha256": policy_sha}
                if (identity is not None and identity != original
                        or value.get("task_identity") is not None and identity is None):
                    raise VersionError("product_binding_changed")
                check_no_relaxation(value.get("constraints", {}), constraints)
            if value is None:
                value = {"schema_version": 1, "run_id": run_id, "owner_sha256": owner,
                         "revision": 0, "version": 0, "plans": {}}
            if len(value["plans"]) >= MAX_PLANS:
                raise VersionError("product_history_limit")
            self._archive_failed_preparation(value)
            self._retire(value, "revision")
            value.update(schema_version=3, task_identity=identity,
                         constraints=constraints, constraints_sha256=digest(constraints),
                         revision=value["revision"] + 1, version=value["version"] + 1,
                         state="preparing", comparison_id=uuid.uuid4().hex, approval=None,
                         last_operation={"id": operation_id, "kind": "prepare",
                                         "input_sha256": input_sha, "stage": "pending"})
            self._save(value)
            context = {"schema_version": 1, "run_id": run_id, "owner_sha256": owner,
                       "version": value["version"], "comparison_id": value["comparison_id"],
                       "inputs_sha256": input_sha}
            try:
                candidates = produce(copy.deepcopy(context))
                if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
                    raise VersionError("product_candidate_invalid")
                rows = {}
                common = None
                for plan_id in candidates:
                    _hash(plan_id)
                    if plan_id in rows or plan_id in value["plans"]:
                        raise VersionError("product_candidate_invalid")
                    directory = self.store / plan_id
                    if directory.is_symlink():
                        raise VersionError("product_candidate_invalid")
                    manifest = read_json(directory / "manifest.json")
                    prepared = read_json(directory / "prepared.json")
                    if (digest(manifest) != plan_id or manifest.get("product") != context
                            or prepared.get("plan_id") != plan_id or prepared.get("ok") is not True
                            or prepared.get("odoo_writes") is not False):
                        raise VersionError("product_binding_changed")
                    row = {"version": value["version"], "product": context,
                           "state": "candidate", "retired_reason": None, "retired_at": None,
                           "policy_sha256": _hash(prepared.get("policy_sha256")),
                           "snapshot_sha256": _hash(manifest["artifacts"]["snapshot"]),
                           "request_sha256": _hash(manifest["artifacts"]["request"]),
                           "baseline_sha256": _hash(manifest["artifacts"]["baseline"]),
                           "sources_sha256": digest(manifest["sources"])}
                    if identity is not None and any(row[key] != expected for key, expected in identity.items()):
                        raise VersionError("product_binding_changed")
                    facts = tuple(row[key] for key in ("policy_sha256", "snapshot_sha256",
                                                       "baseline_sha256", "sources_sha256", "request_sha256"))
                    objective = bound_objective(manifest)
                    comparison = None
                    if objective is not None:
                        recorded_request = read_json(directory / "request.json")
                        load_constraints(directory, manifest, recorded_request)
                        bound_objective(manifest, read_json(directory / "plan.json"))
                        costs = load_cost_report(directory, manifest)
                        if costs is None:
                            raise VersionError("product_binding_changed")
                        comparison = (manifest["product_constraints_sha256"],
                                      manifest["artifacts"]["blueprint"],
                                      digest(manifest["product_objectives"]), digest(costs["basis"]))
                        audit_dir = self.store / "product-comparisons"
                        if audit_dir.is_symlink():
                            raise VersionError("product_binding_changed")
                        audit = read_json(audit_dir / (context["comparison_id"] + ".json"))
                        if (audit.get("schema_version") != 1 or audit.get("product") != context
                                or audit.get("state") != "prepared"
                                or audit.get("candidate_plan_ids") != candidates
                                or audit.get("product_objectives") != manifest["product_objectives"]
                                or audit.get("policy_sha256") != row["policy_sha256"]
                                or audit.get("constraints_sha256") != manifest["product_constraints_sha256"]
                                or any(audit.get(key + "_sha256") != manifest["artifacts"][key]
                                       for key in ("snapshot", "baseline", "blueprint"))):
                            raise VersionError("product_binding_changed")
                        row["comparison_sha256"] = digest(audit)
                        comparison += (row["comparison_sha256"],)
                    facts += (comparison,)
                    if common is not None and common != facts:
                        raise VersionError("product_comparison_facts_mismatch")
                    common, rows[plan_id] = facts, row
                if len(value["plans"]) + len(rows) > MAX_PLANS:
                    raise VersionError("product_history_limit")
                value["plans"].update(rows)
                if len(rows) == 1:
                    value["selected_plan_id"] = candidates[0]
                    value["plans"][candidates[0]]["state"] = "selected"
                value["state"] = "selected" if len(rows) == 1 else "prepared"
                value["last_operation"]["stage"] = "completed"
            except BaseException as error:
                value["state"] = "prepare_failed"
                value["last_operation"]["stage"] = "failed"
                reason = error.code if isinstance(error, ChangeSetError) else str(error)
                value["last_operation"]["failure_reason"] = (
                    reason if reason in PREPARATION_FAILURES else "prepare_failed")
                if isinstance(error, SolverFailure):
                    # The full trace is private durable evidence. Only the
                    # bounded reason/operation binding enters the wire view.
                    value["last_operation"]["solver_trace"] = copy.deepcopy(error.trace)
                self._save(value)
                raise
            value["revision"] += 1
            self._save(value)
            return self._view(value)

    def decide(self, run_id: str, owner: str, *, expected_revision: int,
               operation_id: str, action: str, plan_id: str | None = None) -> dict:
        _name(operation_id)
        if (not isinstance(action, str) or action not in {"select", "reject"}
                or (action == "select" and plan_id is None)):
            raise VersionError("product_invalid_request")
        if plan_id is not None:
            _hash(plan_id)
        input_sha = digest({"action": action, "plan_id": plan_id, "expected_revision": expected_revision})
        with scope_lock(self.store):
            value = self._read(run_id, _hash(owner))
            if value is None:
                raise VersionError("product_decision_missing")
            last = value["last_operation"]
            if last.get("id") == operation_id:
                if last.get("kind") != "decide" or last.get("input_sha256") != input_sha:
                    raise VersionError("product_operation_conflict")
                return self._view(value)
            if value["state"] in LOCKED_STATES:
                raise VersionError("product_already_authorized")
            self._cas(value, expected_revision)
            if action == "select":
                row = value["plans"].get(plan_id)
                if (row is None or row["state"] == "retired"
                        or row["version"] != value["version"]):
                    raise VersionError("product_plan_retired")
                for candidate in value["plans"].values():
                    if candidate["state"] == "selected":
                        candidate["state"] = "candidate"
                row["state"] = "selected"
                value.update(state="selected", selected_plan_id=plan_id)
            else:
                if plan_id is not None and value["selected_plan_id"] != plan_id:
                    raise VersionError("product_selection_changed")
                self._retire(value, "rejected")
                value["state"] = "retired"
            value["revision"] += 1
            value["last_operation"] = {"id": operation_id, "kind": "decide", "input_sha256": input_sha}
            self._save(value)
            return self._view(value)

    def authorize(self, run_id: str, owner: str, *, expected_revision: int,
                  plan_id: str, intent: dict, issue) -> dict:
        """Persist approval intent before issuing its one idempotent token.

        The trusted caller validates actor/timestamps/nonce before this method;
        `issue(binding, intent)` persists exactly that nonce, including retries.
        """
        _hash(plan_id), _hash(owner), _revision(expected_revision)
        with scope_lock(self.store):
            value = self._read(run_id, owner)
            if value is None:
                raise VersionError("product_decision_missing")
            if value["selected_plan_id"] != plan_id:
                raise VersionError("product_plan_not_selected")
            intent_sha = digest(intent)
            approval = value.get("approval")
            if value["state"] in LOCKED_STATES:
                if not isinstance(approval, dict) or approval.get("intent_sha256") != intent_sha:
                    raise VersionError("product_already_authorized")
            else:
                self._cas(value, expected_revision)
                if value["state"] != "selected":
                    raise VersionError("product_plan_not_selected")
                value.update(state="approval_issuing", revision=value["revision"] + 1,
                             approval={"intent_sha256": intent_sha, "token": None})
                self._save(value)
            binding = value["plans"][plan_id]
            token = issue(copy.deepcopy(binding), copy.deepcopy(intent))
            if not isinstance(token, Approval) or (
                token.run_id != run_id or token.plan_id != plan_id
                or token.policy_sha256 != binding["policy_sha256"]
                or token.snapshot_sha256 != binding["snapshot_sha256"]
                or owner_scope(token.approver) != owner
            ):
                raise VersionError("product_approval_mismatch")
            prior = value["approval"]["token"]
            if prior is not None and prior != token.payload():
                raise VersionError("product_approval_mismatch")
            if prior is None:
                value["approval"]["token"] = token.payload()
                value.update(state="approved", revision=value["revision"] + 1)
                self._save(value)
            return {"decision": self._view(value), "approval": token.payload()}

    def assert_executable_locked(self, manifest: dict, plan_id: str, grant) -> dict:
        context = manifest.get("product")
        if not isinstance(context, dict):
            raise VersionError("product_binding_changed")
        value = self._read(context.get("run_id"), context.get("owner_sha256"))
        if value is None:
            raise VersionError("product_decision_missing")
        row = value["plans"].get(plan_id)
        if row is None or row["state"] == "retired":
            raise VersionError("product_plan_retired")
        if row["product"] != context or row["version"] != value["version"]:
            raise VersionError("product_binding_changed")
        if value["selected_plan_id"] != plan_id or row["state"] != "selected":
            raise VersionError("product_plan_not_selected")
        if grant is None or value["state"] not in {"approved", "executing", "executed"}:
            raise VersionError("product_approval_required")
        approval = value.get("approval")
        if not isinstance(approval, dict) or approval.get("token") != grant.approval.payload():
            raise VersionError("product_approval_mismatch")
        return value

    def execution_state_locked(self, value: dict, state: str):
        if state not in {"executing", "executed"}:
            raise VersionError("product_invalid_request")
        if value["state"] == state or value["state"] == "executed":
            return
        value.update(state=state, revision=value["revision"] + 1)
        self._save(value)
