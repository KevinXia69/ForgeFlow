"""Local real executor: host process + Linux Odoo sandbox + root executor.

This is the bridge used by the local workbench. It talks to a running
``forgeflow.executor.main`` over its Unix socket, reads the prepared plan
artifacts, obtains root's one-time approval, applies the same plan and runs the
task's original verifier inside the sandbox.

The executor must be started without ``--autonomous``; the host process must
have access to the same approval store. No public deployment uses this path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from forgeflow.executor.handlers import APPROVAL_FIELDS
from forgeflow.executor.protocol import ERROR_CODES
from forgeflow.planning.objectives import (
    CAPACITY_PRODUCT_OBJECTIVES,
    LEGACY_CANDIDATE_PROFILE,
    bound_objective,
    profile_objectives,
)
from forgeflow.recovery import client
from forgeflow.recovery.failure import failure_projection
from forgeflow.recovery.product_constraints import (
    ConstraintError,
    canonical_constraints,
    load_constraints,
)
from forgeflow.recovery.storage import StorageError, digest, read_json

from .executor import ProductExecutionError
from .progress import HUB
from .state import SCENARIOS, project_action_snapshot

REVISION_CAPABILITY_ENV = "FORGEFLOW_LOCAL_REVISIONS"
REVISION_ACCEPTANCE_ENV = "FORGEFLOW_LOCAL_REVISION_ACCEPTANCE"
CANDIDATE_CAPABILITY_ENV = "FORGEFLOW_LOCAL_CANDIDATES"
CANDIDATE_PROFILE_ENV = "FORGEFLOW_LOCAL_CANDIDATE_PROFILE"


def candidate_profile_setting() -> str | None:
    value = os.environ.get(CANDIDATE_PROFILE_ENV)
    if value is not None:
        profile_objectives(value)
    return value


def candidate_capability_setting() -> bool | None:
    """Explicit candidate opt-in; old and unmanaged integrations stay disabled."""
    value = os.environ.get(CANDIDATE_CAPABILITY_ENV)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in {"1", "true", "yes", "0", "false", "no"}:
        raise ValueError("invalid_candidate_capability_setting")
    return normalized in {"1", "true", "yes"}


def revision_acceptance_setting() -> bool | None:
    """Legacy acceptance alias, retained for existing named acceptance commands."""
    value = os.environ.get(REVISION_ACCEPTANCE_ENV)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError("invalid_revision_acceptance_setting")


def revision_capability_setting() -> bool | None:
    """Explicit local capability; the managed launcher owns the persisted default."""
    legacy = revision_acceptance_setting()
    value = os.environ.get(REVISION_CAPABILITY_ENV)
    if value is None:
        return legacy
    normalized = value.strip().lower()
    if normalized not in {"1", "true", "yes", "0", "false", "no"}:
        raise ValueError("invalid_revision_capability_setting")
    requested = normalized in {"1", "true", "yes"}
    if legacy is not None and requested != legacy:
        raise ValueError("conflicting_revision_settings")
    return requested


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _number_text(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _percent_text(value: float) -> str:
    return f"{_number_text(value)}%"


def _money_text(value: float) -> str:
    return f"${value:,.2f}"


def _units_text(value: float) -> str:
    return f"{_number_text(value)} units"


class LocalProductExecutor:
    """Real local executor for the product approval slice."""

    live = True

    def __init__(
        self,
        *,
        socket_path: str,
        store: str | Path,
        approvals: str | Path,
        request: str | Path,
        container: str,
        scenario: str = "A",
        task: str | None = None,
        tests_path: str = "/tests",
        docker_timeout: int = 300,
        verifier_timeout: int = 300,
        enable_revisions: bool = False,
        enable_candidates: bool = False,
        candidate_profile: str = LEGACY_CANDIDATE_PROFILE,
        artifact_rpc: bool = False,
    ) -> None:
        self.socket_path = str(socket_path)
        self.store = Path(store)
        if type(artifact_rpc) is not bool:
            raise ValueError("invalid_artifact_rpc_setting")
        self.artifact_rpc = artifact_rpc
        self.approvals = Path(approvals)
        self.request = json.loads(Path(request).read_text(encoding="utf-8"))
        if not isinstance(self.request, dict):
            raise ValueError("local request must be a JSON object")
        spec = SCENARIOS.get(scenario)
        if spec is None or (task is not None and task != spec["task"]):
            raise ValueError("local_scenario_task_mismatch")
        self.scenario = scenario
        self.task = spec["task"]
        self.supported_scenarios = (scenario,)
        disruption = self.request.get("disruption")
        if (self.request.get("target_product_code") != spec["product_code"]
                or not isinstance(disruption, dict)
                or disruption.get("kind") != spec["disruption"]["kind"]):
            raise ValueError("local_scenario_request_mismatch")
        if scenario == "B" and disruption.get("workcenter_code") != spec["disruption"]["workcenter_code"]:
            raise ValueError("local_scenario_request_mismatch")
        self.container = container
        self.tests_path = tests_path.rstrip("/") or "/tests"
        self.docker_timeout = docker_timeout
        self.verifier_timeout = verifier_timeout
        if type(enable_revisions) is not bool:
            raise ValueError("invalid_revision_capability")
        # A managed launcher passes its immutable feature choice. Direct callers
        # retain an explicit opt-in, so old integrations are never silently upgraded.
        self.supports_revisions = enable_revisions
        if type(enable_candidates) is not bool:
            raise ValueError("invalid_candidate_capability")
        self.supports_candidates = enable_candidates
        self.candidate_objectives = profile_objectives(candidate_profile)
        if not enable_candidates and candidate_profile != LEGACY_CANDIDATE_PROFILE:
            raise ValueError("candidate_profile_requires_candidates")

    @classmethod
    def from_env(cls) -> "LocalProductExecutor | None":
        if os.environ.get("FORGEFLOW_LOCAL_PRODUCT", "").lower() not in ("1", "true", "yes"):
            return None
        names = {
            "socket_path": "FORGEFLOW_LOCAL_SOCKET",
            "store": "FORGEFLOW_LOCAL_STORE",
            "approvals": "FORGEFLOW_LOCAL_APPROVALS",
            "request": "FORGEFLOW_LOCAL_REQUEST",
            "container": "FORGEFLOW_LOCAL_CONTAINER",
        }
        values = {key: os.environ.get(name, "").strip() for key, name in names.items()}
        missing = [names[key] for key, value in values.items() if not value]
        if missing:
            raise ValueError("missing local product settings: " + ", ".join(missing))
        return cls(
            socket_path=values["socket_path"], store=values["store"],
            approvals=values["approvals"], request=values["request"],
            container=values["container"],
            scenario=os.environ.get("FORGEFLOW_LOCAL_SCENARIO", "A"),
            task=os.environ.get("FORGEFLOW_LOCAL_TASK") or None,
            tests_path=os.environ.get("FORGEFLOW_LOCAL_TESTS", "/tests"),
            enable_revisions=revision_capability_setting() is True,
            enable_candidates=candidate_capability_setting() is True,
            candidate_profile=candidate_profile_setting() or LEGACY_CANDIDATE_PROFILE,
            artifact_rpc=os.environ.get("FORGEFLOW_LOCAL_ARTIFACT_RPC", "").lower() in ("1", "true", "yes"),
        )

    def _require_case_scenario(self, case: dict[str, Any]) -> None:
        # One host executor/store belongs to one configured task. Never send
        # an A case's decision to a B sandbox (or use an unlabelled old request).
        if case.get("scenario") != self.scenario:
            raise ProductExecutionError("product_scenario_unavailable")

    def _json(self, path: Path) -> dict[str, Any]:
        try:
            value = read_json(path)
        except (OSError, ValueError, StorageError) as error:
            raise ProductExecutionError("local_artifact_unavailable") from error
        if not isinstance(value, dict):
            raise ProductExecutionError("local_artifact_unavailable")
        return value

    @staticmethod
    def can_execute_plan(case: dict[str, Any], plan: dict[str, Any] | None) -> bool:
        binding = (plan or {}).get("executor_binding")
        if not isinstance(binding, dict) or set(binding) != {"revision", "product"}:
            return False
        context = binding["product"]
        return (
            type(binding["revision"]) is int and 0 < binding["revision"] <= 10000
            and isinstance(context, dict) and set(context) == {
                "schema_version", "run_id", "owner_sha256", "version", "comparison_id", "inputs_sha256"}
            and type(context["schema_version"]) is int and context["schema_version"] == 1
            and context["run_id"] == f"local-{case.get('case_id')}"
            and isinstance(case.get("_owner_sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", case["_owner_sha256"]) is not None
            and context["owner_sha256"] == case["_owner_sha256"]
            and type(context["version"]) is int
            and context["version"] == (case.get("revision") or {}).get("version", 1)
            and isinstance(context["comparison_id"], str)
            and re.fullmatch(r"[a-f0-9]{32}", context["comparison_id"]) is not None
            and isinstance(context["inputs_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", context["inputs_sha256"]) is not None
        )

    def _require_binding(self, case, plan):
        self._require_case_scenario(case)
        if not self.can_execute_plan(case, plan):
            raise ProductExecutionError("product_version_required")
        return plan["executor_binding"]

    def _product(self, case, action, payload):
        try:
            return client.product(self.socket_path, f"local-{case['case_id']}", action, payload, timeout=300)
        except client.ExecutorError as error:
            # Keep transport/protocol codes only; never expose raw server prose.
            reason = error.reason if error.reason in ERROR_CODES else error.code
            raise ProductExecutionError(reason) from None

    def retire(self, case, plan):
        self._require_case_scenario(case)
        if "executor_binding" not in plan:
            # Old cases may still be rejected locally. The current product
            # root refuses all legacy new writes, independently of this card.
            return
        binding = self._require_binding(case, plan)
        self._product(case, "decide", {
            "owner_sha256": case["_owner_sha256"], "expected_revision": binding["revision"],
            "operation_id": f"reject-{plan['plan_id']}", "action": "reject", "plan_id": plan["plan_id"],
        })

    def retire_comparison(self, case, comparison):
        self._require_case_scenario(case)
        if not self.supports_candidates or not comparison.get("plans"):
            raise ProductExecutionError("candidate_selection_unsupported")
        binding = self._require_binding(case, comparison["plans"][0])
        if comparison.get("root_revision") != binding["revision"]:
            raise ProductExecutionError("product_binding_changed")
        self._product(case, "decide", {"owner_sha256": case["_owner_sha256"],
            "expected_revision": binding["revision"], "operation_id": f"reject-{comparison['comparison_id']}",
            "action": "reject", "plan_id": None})

    def _approval_payload(self, case, plan):
        self._require_binding(case, plan)
        record = case.get("approval")
        if not isinstance(record, dict):
            raise ProductExecutionError("approval_not_bound")
        actor = record.get("approver")
        if (not isinstance(actor, str) or hashlib.sha256(actor.encode()).hexdigest() != case["_owner_sha256"]
                or record.get("plan_id") != plan["plan_id"]):
            raise ProductExecutionError("approval_not_bound")
        return {
            "nonce": record.get("nonce"), "run_id": f"local-{case['case_id']}", "plan_id": plan["plan_id"],
            "policy_sha256": plan["policy_sha256"], "snapshot_sha256": plan["snapshot_sha256"],
            "approver": actor, "issued_at_utc": record.get("issued_at"), "expires_at_utc": record.get("expires_at"),
        }

    def authorize(self, case, plan):
        expected = self._approval_payload(case, plan)
        response = self._product(case, "authorize", {
            "owner_sha256": case["_owner_sha256"], "expected_revision": plan["executor_binding"]["revision"],
            "plan_id": plan["plan_id"], "intent": {key: expected[key] for key in (
                "nonce", "approver", "issued_at_utc", "expires_at_utc")},
        })
        token = response.get("approval")
        if not isinstance(token, dict) or token != {"schema_version": 1, **expected}:
            raise ProductExecutionError("approval_not_bound")
        return expected

    def cancel_unstarted(self, case, plan):
        """Explicit control action; never used by read-only reconcile."""
        expected = self._approval_payload(case, plan)
        if (case["approval"].get("phase") != "dispatching"
                or case["approval"].get("executor_grant") != expected):
            raise ProductExecutionError("approval_not_bound")
        receipt = self._product(case, "cancel_unstarted", {
            "owner_sha256": case["_owner_sha256"], "plan_id": plan["plan_id"], "approval": expected,
        }).get("receipt")
        bindings = {key: expected[key] for key in ("run_id", "plan_id", "policy_sha256", "snapshot_sha256")}
        if (not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in bindings.items())
                or receipt.get("outcome") != "not_started" or receipt.get("approval_invalidated") is not True
                or receipt.get("plan_retired") is not True or not isinstance(receipt.get("receipt_sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", receipt["receipt_sha256"]) is None
                or not isinstance(receipt.get("observed_at"), str)):
            raise ProductExecutionError("product_cancellation_unconfirmed")
        return receipt

    def _plan_actions(self, changes: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for operation in changes.get("operations", []):
            if not isinstance(operation, dict) or operation.get("action") == "keep":
                continue
            target = operation.get("target") if isinstance(operation.get("target"), dict) else {}
            kind = operation.get("kind")
            action = operation.get("action")
            after = operation.get("after") if isinstance(operation.get("after"), dict) else {}
            before = operation.get("before") if isinstance(operation.get("before"), dict) else {}
            document = target.get("name") or target.get("token") or "document"
            quantity = after.get("quantity", before.get("quantity"))
            if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
                quantity = None
            if kind == "purchase_order":
                plan_kind = {"cancel": "cancel_po", "create": "create_po", "update": "update_po"}.get(action, "update_po")
            else:
                plan_kind = {"cancel": "cancel_mo", "create": "create_mo", "update": "adjust_mo"}.get(action, "adjust_mo")
            actions.append({
                "kind": plan_kind,
                "document": str(document),
                "summary": f"{str(action).title()} {kind} {document}",
                "quantity": quantity,
                "before": project_action_snapshot(operation.get("before")),
                "after": project_action_snapshot(operation.get("after")),
            })
        return actions

    def _request_margin_pct(self) -> float | None:
        margin = _finite_number(self.request.get("margin"))
        return None if margin is None else round(margin * 100, 2)

    def _request_demand_units(self) -> float | None:
        customers = self.request.get("customers")
        if not isinstance(customers, list):
            return None
        total = 0.0
        seen = False
        for customer in customers:
            if not isinstance(customer, dict):
                continue
            demand = _finite_number(customer.get("demand"))
            if demand is None or demand < 0:
                continue
            total += demand
            seen = True
        return total if seen else None

    def _plan_demand_covered(self, plan: dict[str, Any]) -> float | None:
        allocations = plan.get("allocations")
        if isinstance(allocations, list):
            total = 0.0
            seen = False
            for allocation in allocations:
                if not isinstance(allocation, dict):
                    return None
                fulfilled = _finite_number(allocation.get("fulfilled_demand"))
                if fulfilled is None or fulfilled < 0:
                    return None
                total += fulfilled
                seen = True
            if seen:
                return total
        return None

    @staticmethod
    def _on_time(plan: dict[str, Any], changes: dict[str, Any]) -> bool | None:
        """Check each actual allocation against the bound change-set flows."""
        allocations = plan.get("allocations")
        flows = (changes.get("projected_state") or {}).get("sales_order_flows")
        if not isinstance(allocations, list) or not allocations or not isinstance(flows, list):
            return None
        for allocation in allocations:
            if not isinstance(allocation, dict):
                return None
            name = allocation.get("task_order_ref")
            deadline = _finite_number(allocation.get("deadline"))
            demand = _finite_number(allocation.get("demand"))
            if not isinstance(name, str) or deadline is None or demand is None:
                return None
            selected = [flow for flow in flows if isinstance(flow, dict)
                        and isinstance(flow.get("sales_order"), dict)
                        and flow["sales_order"].get("name") == name]
            if not selected:
                return None
            quantities = [_finite_number(flow.get("quantity")) for flow in selected]
            days = [_finite_number(flow.get("available_day")) for flow in selected]
            if None in quantities or None in days:
                return None
            if sum(quantities) != demand or any(day > deadline for day in days):
                return False
        return True

    def _checks(
        self,
        *,
        case: dict[str, Any],
        plan: dict[str, Any],
        margin_pct: float | None,
        budget_check: dict[str, Any],
        policy: str,
        on_time: bool | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        threshold_pct = self._request_margin_pct()
        original_floor = threshold_pct
        if constraints and threshold_pct is not None:
            threshold_pct = max(threshold_pct, constraints["minimum_margin_pct"])
        if margin_pct is None:
            margin_status = "unknown"
            margin_actual = None
        elif threshold_pct is None:
            margin_status = "unknown"
            margin_actual = _percent_text(margin_pct)
        else:
            margin_actual = _percent_text(margin_pct)
            if margin_pct >= threshold_pct:
                margin_status = "pass"
            else:
                margin_status = "block"
        checks.append({
            "code": "margin",
            "label": "Planned margin floor",
            "status": margin_status,
            "actual": margin_actual,
            "threshold": (
                None if threshold_pct is None else _percent_text(threshold_pct)
            ),
            "source": (f"independent document-cost check; original floor {original_floor:g}%; "
                       f"additional floor {threshold_pct:g}%; not measured accounting margin"
                       if constraints and original_floor is not None else
                       "solver planned purchasing and manufacturing cost, including retained documents; local request margin; not measured accounting margin"),
        })

        checks.append(copy.deepcopy(budget_check))

        demand_total = self._request_demand_units()
        if demand_total is None:
            order = case.get("order") if isinstance(case.get("order"), dict) else {}
            demand_total = _finite_number(order.get("units"))
        demand_covered = self._plan_demand_covered(plan)
        if demand_covered is not None:
            demand_actual = _units_text(demand_covered)
        else:
            demand_actual = None
        checks.append({
            "code": "demand",
            "label": "Demand coverage",
            "status": ("unknown" if demand_covered is None or demand_total is None
                       else "pass" if demand_covered == demand_total else "block"),
            "actual": demand_actual,
            "threshold": None if demand_total is None else _units_text(demand_total),
            "source": "local solver plan",
        })

        checks.append({
            "code": "on_time", "label": "On-time delivery",
            "status": "unknown" if on_time is None else "pass" if on_time else "block",
            "actual": on_time, "threshold": "all selected orders by their deadlines",
            "source": "bound plan allocations and change-set sales-order flows",
        })
        checks.append({
            "code": "policy",
            "label": "Policy binding",
            "status": "pass" if policy else "unknown",
            "actual": policy,
            "threshold": "required",
            "source": "local plan binding",
        })
        return checks

    def preparation_intent(self, case: dict[str, Any]) -> dict[str, Any]:
        self._require_case_scenario(case)
        owner = case.get("_owner_sha256")
        if not isinstance(owner, str) or re.fullmatch(r"[a-f0-9]{64}", owner) is None:
            raise ProductExecutionError("product_owner_mismatch")
        version = (case.get("revision") or {}).get("version", 1)
        if type(version) is not int or version != 1:
            raise ProductExecutionError("revision_unsupported")
        intent = {"operation_id": "prepare-v1", "expected_revision": 0,
                  "version": 1, "request_sha256": digest(self.request)}
        if self.supports_candidates:
            intent["product_objectives"] = list(self.candidate_objectives)
        return intent

    def prepare(self, case: dict[str, Any]) -> dict[str, Any]:
        intent = self.preparation_intent(case)
        saved = case.get("_preparation")
        if saved is not None and (not isinstance(saved, dict)
                                  or saved.get("phase") != "pending" or saved.get("intent") != intent):
            raise ProductExecutionError("product_binding_changed")
        HUB.reset(case["case_id"])
        HUB.emit(case["case_id"], "read", "active")
        try:
            response = self._product(case, "prepare", {
                "owner_sha256": case["_owner_sha256"], "expected_revision": intent["expected_revision"],
                "operation_id": intent["operation_id"],
                "request": self.request, "constraints": {},
                **({"product_objectives": intent["product_objectives"]} if self.supports_candidates else {}),
            })
        except Exception as error:
            code = getattr(error, "code", None) or "prepare_failed"
            raise ProductExecutionError(str(code)) from None
        return self._prepared_result(case, response.get("decision"), {})

    def _prepared_result(self, case, decision, constraints):
        if self.supports_candidates:
            from .candidates import load_local_comparison

            return {"candidate_set": load_local_comparison(self, case, decision, constraints)}
        return self._prepared_plan(case, decision, constraints)

    def _objective_fields(self):
        return {"product_objectives": list(self.candidate_objectives)} if self.supports_candidates else {}

    def selection_intent(self, case, comparison, plan_id):
        if not self.supports_candidates:
            raise ProductExecutionError("candidate_selection_unsupported")
        self._require_case_scenario(case)
        plans = comparison.get("plans", []) if isinstance(comparison, dict) else []
        plan = next((item for item in plans if item.get("plan_id") == plan_id), None)
        if plan is None:
            raise ProductExecutionError("product_plan_not_selected")
        binding = self._require_binding(case, plan)
        if (binding["revision"] != comparison.get("root_revision")
                or binding["product"]["comparison_id"] != comparison.get("comparison_id")):
            raise ProductExecutionError("product_binding_changed")
        return {"operation_id": f"select-{binding['revision']}-{plan_id}",
                "expected_revision": binding["revision"], "plan_id": plan_id,
                "comparison_id": comparison["comparison_id"]}

    def continue_selection(self, case, comparison, intent):
        """Caller durably saves this intent before sending the root CAS decision."""
        expected = self.selection_intent(case, comparison, intent.get("plan_id"))
        if intent != expected:
            raise ProductExecutionError("product_binding_changed")
        response = self._product(case, "decide", {"owner_sha256": case["_owner_sha256"],
            "expected_revision": intent["expected_revision"], "operation_id": intent["operation_id"],
            "action": "select", "plan_id": intent["plan_id"]})
        from .candidates import load_local_comparison

        selected = load_local_comparison(self, case, response.get("decision"),
                                        (case.get("revision") or {}).get("changes", {}))
        if (selected["selected_plan_id"] != intent["plan_id"]
                or selected["root_revision"] != intent["expected_revision"] + 1
                or selected["comparison_id"] != comparison["comparison_id"]
                or selected["comparison_sha256"] != comparison["comparison_sha256"]):
            raise ProductExecutionError("product_binding_changed")
        before = [{k: v for k, v in plan.items() if k != "executor_binding"} for plan in comparison["plans"]]
        after = [{k: v for k, v in plan.items() if k != "executor_binding"} for plan in selected["plans"]]
        if before != after:
            raise ProductExecutionError("product_binding_changed")
        return selected

    def validate_revision(self, case, changes):
        if not self.supports_revisions:
            raise ProductExecutionError("revision_unsupported")
        self._require_case_scenario(case)
        try:
            clean = canonical_constraints(changes, self.request)
        except ConstraintError as error:
            raise ProductExecutionError(str(error)) from None
        previous = (case.get("revision") or {}).get("changes", {}).get("minimum_margin_pct", 0)
        if not clean or clean["minimum_margin_pct"] <= previous:
            raise ProductExecutionError("product_constraints_must_tighten")
        return clean

    def revision_limits(self, case):
        """Read-only business bounds; the original contract is never rewritten."""
        saved = case.get("_preparation")
        if (case.get("scenario") != self.scenario
                or saved is not None and (saved.get("intent") or {}).get("request_sha256") != digest(self.request)
                or saved is None and case.get("_plan") is not None):
            return None
        original = _finite_number(self.request.get("margin"))
        current = _finite_number((case.get("revision") or {}).get("changes", {}).get("minimum_margin_pct", 0))
        if original is None or current is None or not 0 <= original < 1 or not 0 <= current < 100:
            return None
        original_pct = round(original * 100, 10)
        return {"original_margin_pct": original_pct, "effective_margin_pct": max(original_pct, current),
                "max_exclusive_pct": 100, "decimal_places": 2}

    def revision_intent(self, case, plan, changes):
        constraints = self.validate_revision(case, changes)
        binding = self._require_binding(case, plan)
        version = binding["product"]["version"] + 1
        return {"operation_id": f"revise-v{version}", "expected_revision": binding["revision"],
                "version": version, "parent_plan_id": plan["plan_id"],
                "request_sha256": digest(self.request), "constraints": constraints,
                **self._objective_fields(),
                "inputs_sha256": digest({"request": self.request, "constraints": constraints,
                                         "policy_sha256": plan["policy_sha256"], **self._objective_fields()})}

    def continue_revision(self, case):
        saved = case.get("_preparation") or {}
        old = case.get("_plan")
        if saved.get("kind") != "revision" or saved.get("phase") != "pending" or not isinstance(old, dict):
            raise ProductExecutionError("product_binding_changed")
        intent = self.revision_intent(case, old, saved.get("changes"))
        if saved.get("intent") != intent:
            raise ProductExecutionError("product_binding_changed")
        failure = None
        try:
            response = self._product(case, "prepare", {
                "owner_sha256": case["_owner_sha256"], "expected_revision": intent["expected_revision"],
                "operation_id": intent["operation_id"], "request": self.request,
                "constraints": intent["constraints"],
                **self._objective_fields(),
            })
        except ProductExecutionError as error:
            failure = error.code
            # Read-only status proves retirement/failure even if the prepare
            # reply was lost. It cannot retry ERP, authorize or apply anything.
            response = self._product(case, "status", {"owner_sha256": case["_owner_sha256"]})
        decision = response.get("decision")
        if not isinstance(decision, dict) or decision.get("run_id") != f"local-{case['case_id']}":
            raise ProductExecutionError("product_binding_changed")
        row = (decision.get("plans") or {}).get(old["plan_id"], {})
        if row.get("product") != old["executor_binding"]["product"]:
            raise ProductExecutionError("product_binding_changed")
        operation = decision.get("preparation") or {}
        if (operation.get("operation_id") != intent["operation_id"]
                or operation.get("inputs_sha256") != intent["inputs_sha256"]):
            # A refusal before root reservation must not be described as a
            # retired old plan. Only an unchanged root selection may be reused.
            if (failure is not None and decision.get("revision") == intent["expected_revision"]
                    and decision.get("version") == intent["version"] - 1
                    and decision.get("state") == "selected" and row.get("state") == "selected"
                    and decision.get("selected_plan_id") == old["plan_id"]):
                if failure in {"unavailable", "bad_response", "internal_error", "executor_busy",
                               "another_recovery_operation_running"}:
                    raise ProductExecutionError(failure)
                return {"status": "not_started", "reason": failure}
            return {"status": "conflict", "reason": "product_revision_conflict"}
        if (decision.get("version") != intent["version"] or row.get("state") != "retired"
                or row.get("retired_reason") != "revision" or not isinstance(row.get("retired_at"), str)):
            raise ProductExecutionError("product_binding_changed")
        result = {"retired_at": row["retired_at"], "parent_plan_id": old["plan_id"],
                  "version": intent["version"]}
        if operation.get("stage") in {"failed", "pending"}:
            reason = (operation.get("failure_reason") if operation["stage"] == "failed"
                      else "product_preparation_interrupted")
            return {**result, "status": "failed", "reason": reason or "prepare_failed"}
        if operation.get("stage") != "completed":
            raise ProductExecutionError("product_binding_changed")
        target_case = {**case, "revision": {"version": intent["version"], "changes": intent["constraints"]}}
        plan = self._prepared_result(target_case, decision, intent["constraints"])
        return {**result, "status": "prepared", "plan": plan}

    def repreparation_intent(self, case):
        """A fresh version after a confirmed reserved preparation failure."""
        if not self.supports_revisions:
            raise ProductExecutionError("revision_unsupported")
        self._require_case_scenario(case)
        saved = case.get("_preparation") or {}
        previous = copy.deepcopy(saved.get("intent") or {})
        initial = (saved.get("kind") is None and previous.get("operation_id") == "prepare-v1"
                   and previous.get("version") == 1)
        if ((not initial and saved.get("kind") not in {"revision", "reprepare"}) or saved.get("phase") != "failed"
                or case.get("_plan") is not None or case.get("approval") is not None
                or case.get("execution") is not None or case.get("state") != "failed"
                or previous.get("version") != case.get("revision", {}).get("version")
                or previous.get("request_sha256") != digest(self.request)
                or previous.get("product_objectives") != self._objective_fields().get("product_objectives")
                or previous.get("constraints", {} if initial else None) != case["revision"].get("changes")):
            raise ProductExecutionError("product_binding_changed")
        decision = self._product(case, "status", {"owner_sha256": case["_owner_sha256"]}).get("decision")
        if initial:
            # v1 predates the API's knowledge of root's policy. Only root can
            # supply its reserved input binding; root pins the original identity
            # again before allowing the next reservation.
            input_sha = ((decision or {}).get("preparation") or {}).get("inputs_sha256")
            if not isinstance(input_sha, str) or re.fullmatch(r"[a-f0-9]{64}", input_sha) is None:
                raise ProductExecutionError("product_binding_changed")
            previous.update(constraints={}, inputs_sha256=input_sha)
        if not self._failed_preparation_matches(case, decision, previous):
            raise ProductExecutionError("product_revision_conflict")
        version = previous["version"] + 1
        return {"operation_id": f"reprepare-v{version}", "expected_revision": decision["revision"],
                "version": version, "request_sha256": previous["request_sha256"],
                "constraints": copy.deepcopy(previous["constraints"]),
                **self._objective_fields(),
                "inputs_sha256": previous["inputs_sha256"], "origin": previous}

    @staticmethod
    def _failed_preparation_matches(case, decision, intent):
        if not isinstance(decision, dict):
            return False
        operation = decision.get("preparation") or {}
        return (decision.get("run_id") == f"local-{case['case_id']}"
                and decision.get("version") == intent.get("version")
                and decision.get("state") in {"prepare_failed", "preparing"}
                and decision.get("selected_plan_id") is None
                and all(row.get("state") == "retired" for row in decision.get("plans", {}).values())
                and operation.get("operation_id") == intent.get("operation_id")
                and operation.get("inputs_sha256") == intent.get("inputs_sha256")
                and operation.get("stage") in {"failed", "pending"})

    def continue_repreparation(self, case):
        if not self.supports_revisions:
            raise ProductExecutionError("revision_unsupported")
        self._require_case_scenario(case)
        saved = case.get("_preparation") or {}
        intent, origin = saved.get("intent") or {}, saved.get("origin") or {}
        version = case.get("revision", {}).get("version")
        if (saved.get("kind") != "reprepare" or saved.get("phase") != "pending"
                or case.get("_plan") is not None or case.get("approval") is not None
                or case.get("execution") is not None or type(version) is not int
                or origin.get("version") != version or intent.get("version") != version + 1
                or intent.get("operation_id") != f"reprepare-v{version + 1}"
                or intent.get("request_sha256") != digest(self.request)
                or intent.get("product_objectives") != self._objective_fields().get("product_objectives")
                or any(intent.get(key) != origin.get(key) for key in (
                    "request_sha256", "constraints", "inputs_sha256"))
                or intent.get("constraints") != case["revision"].get("changes")):
            raise ProductExecutionError("product_binding_changed")
        failure = None
        try:
            response = self._product(case, "prepare", {
                "owner_sha256": case["_owner_sha256"], "expected_revision": intent["expected_revision"],
                "operation_id": intent["operation_id"], "request": self.request,
                "constraints": intent["constraints"],
                **self._objective_fields(),
            })
        except ProductExecutionError as error:
            failure = error.code
            response = self._product(case, "status", {"owner_sha256": case["_owner_sha256"]})
        decision = response.get("decision") or {}
        operation = decision.get("preparation") or {}
        if (decision.get("run_id") != f"local-{case['case_id']}"
                or decision.get("version") != intent["version"]
                or operation.get("operation_id") != intent["operation_id"]
                or operation.get("inputs_sha256") != intent["inputs_sha256"]):
            if (failure and self._failed_preparation_matches(case, decision, origin)
                    and decision.get("revision") == intent["expected_revision"]):
                raise ProductExecutionError(failure)
            return {"status": "conflict", "reason": "product_revision_conflict"}
        if operation.get("stage") in {"failed", "pending"}:
            if not self._failed_preparation_matches(case, decision, intent):
                raise ProductExecutionError("product_binding_changed")
            return {"status": "failed", "version": intent["version"], "reason": (
                operation.get("failure_reason") if operation["stage"] == "failed"
                else "product_preparation_interrupted")}
        if operation.get("stage") != "completed":
            raise ProductExecutionError("product_binding_changed")
        target = {**case, "revision": {"version": intent["version"], "changes": intent["constraints"]}}
        return {"status": "prepared", "version": intent["version"],
                "plan": self._prepared_result(target, decision, intent["constraints"])}

    def _prepared_plan(self, case, decision, constraints, *, candidate_id=None):
        plan_id = candidate_id or (decision.get("selected_plan_id") if isinstance(decision, dict) else None)
        if not isinstance(plan_id, str) or re.fullmatch(r"[a-f0-9]{64}", plan_id) is None:
            raise ProductExecutionError("prepare_failed")
        from .artifacts import prepared_directory

        with prepared_directory(self, case, plan_id) as directory:
            return self._project_prepared_plan(case, decision, constraints, plan_id,
                                               directory, candidate_id=candidate_id)

    def _project_prepared_plan(self, case, decision, constraints, plan_id, directory, *, candidate_id=None):
        prepared = self._json(directory / "prepared.json")
        manifest = self._json(directory / "manifest.json")
        changes = self._json(directory / "change-set.json")
        plan_record = self._json(directory / "plan.json")
        binding = {"revision": decision.get("revision"), "product": manifest.get("product")}
        row = (decision.get("plans") or {}).get(plan_id, {})
        selected = decision.get("selected_plan_id") == plan_id
        selection_valid = (decision.get("state") in {"prepared", "selected"}
                           and row.get("state") == ("selected" if selected else "candidate")
                           and (candidate_id is not None or selected))
        if (not self.can_execute_plan(case, {"executor_binding": binding})
                or not selection_valid
                or row.get("product") != binding["product"] or digest(manifest) != plan_id
                or manifest.get("artifacts", {}).get("plan") != digest(plan_record)
                or manifest.get("artifacts", {}).get("change-set") != digest(changes)):
            raise ProductExecutionError("plan_not_bound")
        plan = plan_record.get("plan", {})
        if not isinstance(plan, dict):
            plan = {}
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
        policy = prepared.get("policy_sha256")
        snapshot = artifacts.get("snapshot")
        if (not isinstance(policy, str) or not isinstance(snapshot, str)
                or manifest.get("policy_sha256") != policy
                or row.get("policy_sha256") != policy or row.get("snapshot_sha256") != snapshot):
            raise ProductExecutionError("plan_not_bound")
        try:
            objective = bound_objective(manifest, plan_record)
            if constraints and "product_constraints_sha256" not in manifest:
                raise ValueError
            if load_constraints(directory, manifest, self.request) != constraints:
                raise ValueError
            if ("product_constraints_sha256" in manifest
                    and artifacts.get("request") != digest(self.request)):
                raise ValueError
        except (OSError, ValueError, StorageError):
            raise ProductExecutionError("plan_not_bound") from None
        from forgeflow.recovery.product_costs import load_cost_report

        try:
            costs = load_cost_report(directory, manifest)
        except ValueError:
            raise ProductExecutionError("plan_not_bound") from None
        HUB.emit(case["case_id"], "read", "done")
        HUB.emit(case["case_id"], "plan", "done")
        HUB.emit(case["case_id"], "policy", "done")
        raw_margin = _finite_number(plan.get("margin"))
        if constraints:
            checked = (changes.get("projected_state") or {}).get("checks") or {}
            if (checked.get("product_margin_satisfied") is not True
                    or checked.get("product_minimum_margin_pct") != constraints["minimum_margin_pct"]):
                raise ProductExecutionError("plan_not_bound")
            raw_margin = _finite_number(checked.get("combined_new_spend_margin"))
        margin_pct = None if raw_margin is None else round(raw_margin * 100, 2)
        # Legacy wire name: the solver includes all projected POs and MO standard
        # manufacturing costs, including retained documents. This is not a delta
        # or the value of newly created purchase orders.
        new_spend = _finite_number(plan.get("expected_new_spend"))
        if costs is not None:
            total = costs["planned"]["total"]
            new_spend = None if total is None else float(total)
        on_time = self._on_time(plan, changes)
        from .budget_checks import build_budget_check

        bound_request = self._json(directory / "request.json")
        bound_snapshot = self._json(directory / "snapshot.json")
        if digest(bound_request) != artifacts.get("request") or digest(bound_snapshot) != snapshot:
            raise ProductExecutionError("plan_not_bound")
        try:
            budget_check = build_budget_check(bound_request, bound_snapshot,
                plan_id=plan_id, snapshot_sha256=snapshot)
        except ValueError:
            raise ProductExecutionError("plan_not_bound") from None
        capacity = {}
        if manifest.get("product_objectives") == list(CAPACITY_PRODUCT_OBJECTIVES):
            from .capacity import build_capacity_summary
            from .decision_metrics import build_decision_metrics
            from .impact import build_impact_chain
            from .readiness import build_order_readiness

            blueprint = self._json(directory / "blueprint.json")
            if digest(blueprint) != artifacts.get("blueprint"):
                raise ProductExecutionError("plan_not_bound")
            try:
                capacity["capacity"] = build_capacity_summary(bound_request, blueprint, changes,
                    plan_id=plan_id, snapshot_sha256=snapshot)
                capacity["order_readiness"] = build_order_readiness(bound_request, blueprint, plan, changes,
                    plan_id=plan_id, snapshot_sha256=snapshot)
                capacity["decision_metrics"] = build_decision_metrics(bound_request, bound_snapshot, blueprint,
                    constraints, costs, capacity["capacity"], capacity["order_readiness"],
                    plan_id=plan_id, snapshot_sha256=snapshot)
                capacity["impact_chain"] = build_impact_chain(bound_request, bound_snapshot, blueprint,
                    plan_record, changes, capacity["order_readiness"], plan_id=plan_id,
                    snapshot_sha256=snapshot, product_constraints=constraints, product_objective=objective)
            except (ValueError, KeyError, TypeError):
                raise ProductExecutionError("plan_not_bound") from None
        return {
            **capacity,
            "plan_id": plan_id,
            "actions": self._plan_actions(changes),
            "margin": margin_pct,
            "on_time": on_time,
            "new_spend": new_spend,
            "checks": self._checks(
                case=case, plan=plan, margin_pct=margin_pct,
                budget_check=budget_check, policy=policy, on_time=on_time, constraints=constraints,
            ),
            "cost_delta": None,
            "costs": costs,
            "policy_sha256": policy,
            "snapshot_sha256": snapshot,
            "prepared_at": prepared.get("prepared_at") or decision.get("updated_at") or "",
            "executor_binding": copy.deepcopy(binding),
            **({"objective": objective} if objective is not None else {}),
        }

    def _run_verifier(self) -> dict[str, Any]:
        env = os.environ.copy()
        command = ["docker", "exec", self.container, "bash", f"{self.tests_path}/test.sh"]
        try:
            run = subprocess.run(
                command, env=env, capture_output=True, text=True,
                timeout=self.verifier_timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "not_run", "strict_success": None,
                    "evaluation_valid": None, "error": "verifier_unavailable"}
        if run.returncode != 0:
            return {"status": "failed", "strict_success": None,
                    "evaluation_valid": False, "error": "verifier_failed"}
        return self._read_verifier_reward()

    def _read_verifier_reward(self) -> dict[str, Any]:
        """Read the original receipt; never infer strict success from score alone."""
        from forgeflow.runner import rule_projection

        try:
            reward = subprocess.run(
                ["docker", "exec", self.container, "cat", "/logs/verifier/reward.json"],
                env=os.environ.copy(), capture_output=True, text=True, timeout=60,
            )
            payload = json.loads(reward.stdout)
            if reward.returncode != 0 or not isinstance(payload, dict):
                raise ValueError("unavailable")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {"status": "unknown", "strict_success": None,
                    "evaluation_valid": None, "error": "reward_unavailable"}
        score = _finite_number(payload.get("overall_score"))
        original_passed = payload.get("passed")
        projection = rule_projection(payload)
        valid = (score is not None and 0 <= score <= 100
                 and isinstance(original_passed, bool) and original_passed == (score >= 99)
                 and (projection is None or projection["issue"] is None))
        strict = None
        if valid:
            if original_passed is False:
                strict = False
            elif projection is not None:
                strict = projection["all_pass"]
        rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else {}
        return {
            "status": "passed" if strict is True else "failed" if strict is False else "unknown",
            "score": score,
            "original_passed": original_passed if isinstance(original_passed, bool) else None,
            "strict_success": strict,
            "evaluation_valid": valid,
            "evaluation_scope": "product_original_verifier",
            "rules": rules,
            "passed_rule_count": projection["passed"] if projection else None,
            "applicable_rule_count": projection["applicable"] if projection else None,
            "failed_rule_count": projection["failed"] if projection else None,
            "checks": [{
                "kind": "original_verifier",
                "document": f"{score}/100" if score is not None else "unknown",
                "passed": strict,
                "passed_count": projection["passed"] if projection else None,
                "total": rules.get("total"),
                "applicable": projection["applicable"] if projection else None,
            }],
        }

    def apply(self, case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        self._require_case_scenario(case)
        run_id = f"local-{case['case_id']}"
        plan_id = plan["plan_id"]
        HUB.reset(case["case_id"])
        HUB.emit(case["case_id"], "approval", "done")
        HUB.emit(case["case_id"], "apply", "active")
        may_have_dispatched = False
        try:
            expected = self._approval_payload(case, plan)
            approval_record = case["approval"]
            approval = approval_record.get("executor_grant")
            if (approval_record.get("phase") != "dispatching" or not isinstance(approval, dict)
                    or set(approval) != set(APPROVAL_FIELDS) or approval != expected):
                raise ProductExecutionError("approval_not_bound")
            may_have_dispatched = True
            result = client.apply(
                self.socket_path, run_id, plan_id,
                approval=approval,
                timeout=900,
            )
            HUB.emit(case["case_id"], "apply", "done")
            HUB.emit(case["case_id"], "readback", "done")
            HUB.emit(case["case_id"], "verify", "active")
        except Exception as error:
            code = getattr(error, "code", None) or "apply_failed"
            known_codes = {value[0] for value in ERROR_CODES.values()} | {
                "unavailable", "bad_response", "apply_failed", "approval_not_bound",
            }
            if not isinstance(code, str) or code not in known_codes:
                code = "apply_failed"
            HUB.emit(case["case_id"], "apply", "failed", code)
            # Once a request may have reached the executor, a lost response is
            # not evidence of zero writes. Recovery only queries the original plan.
            unknown = may_have_dispatched and code != "approval_rejected"
            failure = failure_projection(getattr(error, "failure", None), plan_id=plan_id)
            reason = getattr(error, "reason", None)
            if isinstance(reason, str) and reason in ERROR_CODES and ERROR_CODES[reason][0] == code:
                code = reason
            raise ProductExecutionError(str(code), outcome_unknown=unknown, failure=failure) from None
        if result.get("execution_validated") is not True:
            raise ProductExecutionError("readback_unconfirmed", outcome_unknown=True)
        try:
            execution = self._bound_verification(case, plan)
        except ProductExecutionError:
            # The apply receipt is known, but no attributable verifier receipt
            # reached us. Keep execution evidence and offer only read recovery.
            execution = {
                "status": "succeeded",
                "readback": {"execution_validated": True, "verified_steps": result.get("verified_steps")},
                "verifier": {"status": "unknown", "strict_success": None,
                             "evaluation_valid": None, "error": "verification_receipt_unavailable"},
                "writes": plan.get("actions", []),
            }
        verifier = execution["verifier"]
        HUB.emit(case["case_id"], "verify", "done" if verifier.get("status") == "passed" else "failed")
        return execution

    def _bound_verification(self, case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        run_id = f"local-{case['case_id']}"
        try:
            result = client.reconcile(self.socket_path, run_id, plan["plan_id"], timeout=900)
        except client.ExecutorError:
            raise ProductExecutionError("reconciliation_unavailable", outcome_unknown=True) from None
        expected = {
            "run_id": run_id, "case_id": case["case_id"], "plan_id": plan["plan_id"],
            "policy_sha256": plan.get("policy_sha256"), "snapshot_sha256": plan.get("snapshot_sha256"),
        }
        if (any(value is None or result.get(key) != value for key, value in expected.items())
                or result.get("execution_validated") is not True
                or any(not isinstance(result.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", result[key]) is None
                       for key in ("receipt_sha256", "readback_sha256"))
                or not isinstance(result.get("verifier"), dict)):
            raise ProductExecutionError("reconciliation_binding_changed", outcome_unknown=True)
        writes = plan.get("actions", [])
        return {
            "status": "succeeded",
            "readback": {
                "execution_validated": True,
                "verified_steps": result.get("verified_steps"),
                "receipt_sha256": result["receipt_sha256"],
                "readback_sha256": result["readback_sha256"],
                "verified_at": result.get("verified_at"),
                "records": [
                    {
                        "kind": action.get("kind"),
                        "document": action.get("document"),
                        "state": "reflected",
                        "quantity": action.get("quantity"),
                    }
                    for action in writes
                ],
            },
            "verifier": result["verifier"],
            "writes": writes,
        }

    def reconcile(self, case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        """Recover original readback/verification; never apply, re-plan or approve."""
        self._require_case_scenario(case)
        prior = case.get("execution") or {}
        verifier = prior.get("verifier")
        missing_receipt = isinstance(verifier, dict) and verifier.get("error") == "verification_receipt_unavailable"
        if isinstance(verifier, dict) and verifier.get("status") not in (None, "not_run") and not missing_receipt:
            # An original failure (or measured-but-unknown result) is history,
            # not permission to rerun the checker until it produces a pass.
            return prior
        return self._bound_verification(case, plan)
