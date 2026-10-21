"""Pure state model for the server-independent product approval slice.

Nothing in this module touches the network or the filesystem.  The service
persists :class:`ProductCase` records; ``view()`` is the only projection the
API exposes, so internal fields such as ``uid`` never leak into a CaseFile.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from forgeflow.planning.objectives import PRODUCT_OBJECTIVE_KINDS
from forgeflow.recovery.failure import failure_projection
from forgeflow.recovery.product_costs import project_cost_report

from .budget_checks import project_budget_check
from .capacity import project_capacity
from .decision_metrics import project_decision_metrics
from .impact import project_impact_chain
from .readiness import project_order_readiness

PREPARING = "preparing"
AWAITING_APPROVAL = "awaiting_approval"
AWAITING_SELECTION = "awaiting_selection"
EXECUTING = "executing"
SUCCEEDED = "succeeded"
FAILED = "failed"
REJECTED = "rejected"
EXPIRED = "expired"
REVISION_REQUESTED = "revision_requested"
EXECUTION_UNKNOWN = "execution_unknown"

PRODUCT_STATES = (
    PREPARING,
    AWAITING_APPROVAL,
    AWAITING_SELECTION,
    EXECUTING,
    SUCCEEDED,
    FAILED,
    REJECTED,
    EXPIRED,
    REVISION_REQUESTED,
    EXECUTION_UNKNOWN,
)
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, REJECTED, EXPIRED})
STATES = PRODUCT_STATES
SUCCESS_STATUSES = frozenset(
    {"succeeded", "success", "applied", "ok", "completed"}
)

CASE_ID = re.compile(r"[0-9a-f]{32}")
PLAN_ID = re.compile(r"[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CHECK_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CHECK_STATUSES = frozenset({"pass", "warn", "block", "unknown"})
MAX_CHECK_TEXT = 500
MAX_CHECKS = 32
REQUIRED_CHECKS = frozenset({"margin", "budget", "policy", "demand", "on_time"})

SCENARIO_A_ORDER: dict[str, Any] = {
    "ref": "SO-1..5",
    "product": "Clean Room Sealed Fixture",
    "units": 50,
    "due_days": 8,
    "customer": "5 confirmed sales orders",
}
SCENARIO_A_DISRUPTION: dict[str, Any] = {
    "kind": "supplier_cancellation",
    "vendor": "Stonewall Components",
}
SCENARIOS: dict[str, dict[str, Any]] = {
    "A": {
        "task": "2280_medium_repair_plan_medium",
        "product_code": "P27637E67E7-LED-CRS-009",
        "label": "Supplier cancellation",
        "order": SCENARIO_A_ORDER, "disruption": SCENARIO_A_DISRUPTION,
    },
    # Reviewed task facts, not a solver result or a claim of live execution.
    # Source: contracts/2299-workcenter-outage-v1.json, including its citations.
    "B": {
        "task": "2299_hard_repair_plan_hard",
        "product_code": "P1223565D60-SOL-CSC-007",
        "label": "Workcenter outage",
        "order": {
            "ref": "SO-1..12", "product": "Carport Solar Canopy Module",
            "units": 228, "due_days": 8, "due_days_max": 12,
            "customer": "12 confirmed sales orders",
        },
        "disruption": {
            "kind": "workcenter_outage", "workcenter": "Assembly Line 1",
            "workcenter_code": "P1223565D60-WC01",
        },
    },
}


def utc_stamp() -> str:
    """Return the current UTC time in the wire format used by CaseFile."""
    return datetime.now(UTC).isoformat()


def new_case_id() -> str:
    return secrets.token_hex(16)


def new_nonce() -> str:
    return secrets.token_hex(16)


def canonical_digest(value: Any) -> str:
    """Hash a JSON value with a stable encoding."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def scenario_spec(scenario: str) -> dict[str, Any]:
    """Return a private copy of the synthetic order/disruption for a scenario."""
    try:
        spec = SCENARIOS[scenario]
    except (KeyError, TypeError):
        raise KeyError("invalid_scenario") from None
    return copy.deepcopy(spec)


def _project_order(order: dict[str, Any]) -> dict[str, Any]:
    projected = {
        "ref": order["ref"],
        "product": order["product"],
        "units": order["units"],
        "due_days": order["due_days"],
        "customer": order["customer"],
    }
    if "due_days_max" in order:
        projected["due_days_max"] = order["due_days_max"]
    return projected


def _project_disruption(disruption: dict[str, Any]) -> dict[str, Any]:
    return {key: disruption[key] for key in ("kind", "vendor", "workcenter", "workcenter_code") if key in disruption}


def project_action_snapshot(value: Any) -> dict[str, Any] | None:
    """Expose only recorded business fields from a bound change-set snapshot.

    Missing snapshots stay missing. In particular, a cancelled document's
    absent active after-state does not imply that its observed quantity is zero.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid_action_snapshot")
    fields = {
        "quantity", "unit_price", "arrival_day", "date_planned", "start_day",
        "due_day", "date_start", "date_deadline", "product_code", "vendor_ref",
        "workcenter_code", "supply_role", "state",
    }
    projected = {key: _check_scalar("snapshot", item) for key, item in value.items() if key in fields}
    nested_fields = {
        "workorder_plan": {"duration_expected_minutes", "start_day", "due_day", "date_start", "date_finished"},
        "public_standard": {"time_per_unit_minutes", "unit_cost", "lead_days"},
    }
    for key, allowed in nested_fields.items():
        raw = value.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError("invalid_action_snapshot")
        child = {field: _check_scalar("snapshot", item) for field, item in raw.items() if field in allowed}
        if child:
            projected[key] = child
    if "origin_refs" in value:
        refs = value["origin_refs"]
        if not isinstance(refs, list) or len(refs) > 256:
            raise ValueError("invalid_action_snapshot")
        projected["origin_refs"] = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("invalid_action_snapshot")
            projected["origin_refs"].append({
                key: _check_scalar("snapshot", item)
                for key, item in ref.items() if key in {"model", "name", "token"}
            })
    return projected or None


def _project_action(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": action["kind"],
        "document": action["document"],
        "summary": action["summary"],
        "quantity": action["quantity"],
        "before": project_action_snapshot(action.get("before")),
        "after": project_action_snapshot(action.get("after")),
    }


def _project_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": plan["plan_id"],
        "actions": [_project_action(action) for action in plan["actions"]],
        "margin": plan["margin"],
        "on_time": plan["on_time"],
        "new_spend": plan.get("new_spend"),
        "checks": _bound_checks(plan),
        "cost_delta": plan["cost_delta"],
        "costs": _bound_costs(plan),
        "policy_sha256": plan["policy_sha256"],
        "snapshot_sha256": plan["snapshot_sha256"],
        "prepared_at": plan["prepared_at"],
        **({"capacity": project_capacity(plan["capacity"], plan_id=plan["plan_id"],
            snapshot_sha256=plan["snapshot_sha256"])} if "capacity" in plan else {}),
        **({"order_readiness": project_order_readiness(plan["order_readiness"], plan_id=plan["plan_id"],
            snapshot_sha256=plan["snapshot_sha256"])} if "order_readiness" in plan else {}),
        **({"decision_metrics": _bound_decision_metrics(plan)} if "decision_metrics" in plan else {}),
        **({"impact_chain": _bound_impact_chain(plan)} if "impact_chain" in plan else {}),
        **({"objective": plan["objective"]} if "objective" in plan else {}),
    }


def _bound_checks(plan):
    checks = plan.get("checks") or []
    if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
        raise ValueError("invalid_checks")
    codes = [check.get("code") for check in checks]
    if any(not isinstance(code, str) for code in codes):
        raise ValueError("invalid_check_code")
    if len(set(codes)) != len(codes):
        raise ValueError("duplicate_check_code")
    return [project_budget_check(check, plan_id=plan.get("plan_id"),
                                 snapshot_sha256=plan.get("snapshot_sha256")) for check in checks]


def _validate_saved_plan_facts(case):
    # Never rewrite historical facts or infer a new basis for legacy records.
    if not isinstance(case.history, list) or any(not isinstance(entry, dict) for entry in case.history):
        raise ValueError("invalid_history")
    for plan in [case.plan, *(entry.get("plan") for entry in case.history)]:
        if plan is not None:
            _bound_checks(plan)
            if "impact_chain" in plan:
                _bound_impact_chain(plan)


def _bound_costs(plan):
    costs = project_cost_report(plan.get("costs"))
    if costs is not None and costs["basis"]["snapshot_sha256"] != plan.get("snapshot_sha256"):
        raise ValueError("product_costs_binding_changed")
    return costs


def _bound_decision_metrics(plan):
    return project_decision_metrics(plan.get("decision_metrics"), plan_id=plan["plan_id"],
        snapshot_sha256=plan["snapshot_sha256"], costs=plan.get("costs"),
        capacity=plan.get("capacity"), readiness=plan.get("order_readiness"))


def _bound_impact_chain(plan):
    return project_impact_chain(plan.get("impact_chain"), plan_id=plan["plan_id"],
        snapshot_sha256=plan["snapshot_sha256"], readiness=plan.get("order_readiness"))


def _project_approval(approval: dict[str, Any]) -> dict[str, Any]:
    projected = {"nonce": approval["nonce"], "expires_at": approval["expires_at"]}
    for key in ("approver", "issued_at", "comment", "plan_id", "phase"):
        value = approval.get(key)
        if isinstance(value, str):
            projected[key] = value
    return projected


def _project_execution(execution: dict[str, Any], plan_id=None) -> dict[str, Any]:
    result = {
        "status": execution["status"],
        "readback": execution["readback"],
        "verifier": execution["verifier"],
        "writes": execution["writes"],
    }
    failure = failure_projection(execution.get("failure"), plan_id=plan_id)
    if failure is not None:
        result["failure"] = failure
    return result


def _number(name: str, value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_{name}")
    if not math.isfinite(float(value)):
        raise ValueError(f"invalid_{name}")
    return float(value)


def _plan_hash(name: str, value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        return fallback
    return value


def _check_scalar(name: str, value: Any) -> Any:
    """Return a JSON-safe scalar for one policy-check actual/threshold field."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"invalid_check_{name}")
        return value
    if isinstance(value, str):
        if len(value) > MAX_CHECK_TEXT:
            raise ValueError(f"invalid_check_{name}")
        return value
    raise ValueError(f"invalid_check_{name}")


def _normalize_checks(value: Any) -> list[dict[str, Any]]:
    """Validate executor checks without inventing or reordering evidence."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CHECKS:
        raise ValueError("invalid_checks")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("invalid_check")
        code = raw.get("code")
        if not isinstance(code, str) or CHECK_CODE.fullmatch(code) is None:
            raise ValueError("invalid_check_code")
        if code in seen:
            raise ValueError("duplicate_check_code")
        seen.add(code)
        status = raw.get("status")
        if status not in CHECK_STATUSES:
            raise ValueError("invalid_check_status")
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            label = code
        elif len(label) > MAX_CHECK_TEXT:
            raise ValueError("invalid_check_label")
        source = raw.get("source")
        if source is None:
            source = ""
        if not isinstance(source, str) or len(source) > MAX_CHECK_TEXT:
            raise ValueError("invalid_check_source")
        checks.append(
            {
                "code": code,
                "label": label,
                "status": status,
                "actual": _check_scalar("actual", raw.get("actual")),
                "threshold": _check_scalar("threshold", raw.get("threshold")),
                "source": source,
                **({"basis": copy.deepcopy(raw["basis"])} if "basis" in raw else {}),
            }
        )
    return checks


def normalize_executor_binding(case: "ProductCase", value: Any) -> dict[str, Any]:
    """Validate private root context; never derive authority from a UI pointer."""
    if not isinstance(value, dict) or set(value) != {"revision", "product"}:
        raise ValueError("invalid_executor_binding")
    product = value["product"]
    if (type(value["revision"]) is not int or not 0 < value["revision"] <= 10000
            or not isinstance(product, dict) or set(product) != {
                "schema_version", "run_id", "owner_sha256", "version", "comparison_id", "inputs_sha256"}
            or type(product["schema_version"]) is not int or product["schema_version"] != 1
            or product["run_id"] != f"local-{case.case_id}"
            or product["owner_sha256"] != hashlib.sha256(case.uid.encode()).hexdigest()
            or type(product["version"]) is not int or product["version"] != case.revision["version"]
            or not isinstance(product["comparison_id"], str)
            or re.fullmatch(r"[a-f0-9]{32}", product["comparison_id"]) is None
            or not isinstance(product["inputs_sha256"], str)
            or PLAN_ID.fullmatch(product["inputs_sha256"]) is None):
        raise ValueError("invalid_executor_binding")
    return copy.deepcopy(value)


def normalize_plan(case: "ProductCase", value: Any) -> dict[str, Any]:
    """Validate an executor plan and project it onto the CaseFile plan shape.

    A compliant executor supplies every field.  Missing immutable hashes are
    filled from the synthetic case snapshot so a minimal fake executor cannot
    produce a CaseFile that is missing the approval binding fields.
    """
    if not isinstance(value, dict):
        raise ValueError("invalid_plan")
    plan_id = value.get("plan_id")
    if not isinstance(plan_id, str) or PLAN_ID.fullmatch(plan_id) is None:
        raise ValueError("invalid_plan_id")

    raw_actions = value.get("actions")
    if raw_actions is None:
        raw_actions = []
    if not isinstance(raw_actions, list):
        raise ValueError("invalid_actions")
    actions: list[dict[str, Any]] = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            raise ValueError("invalid_action")
        kind = raw.get("kind")
        document = raw.get("document")
        if not isinstance(kind, str) or not kind:
            raise ValueError("invalid_action_kind")
        if not isinstance(document, str) or not document:
            raise ValueError("invalid_action_document")
        summary = raw.get("summary")
        if not isinstance(summary, str):
            summary = ""
        quantity = raw.get("quantity")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, (int, float))):
            raise ValueError("invalid_action_quantity")
        if quantity is not None and not math.isfinite(float(quantity)):
            raise ValueError("invalid_action_quantity")
        actions.append(
            {
                "kind": kind,
                "document": document,
                "summary": summary,
                "quantity": quantity,
                "before": project_action_snapshot(raw.get("before")),
                "after": project_action_snapshot(raw.get("after")),
            }
        )

    on_time = value.get("on_time")
    if not isinstance(on_time, bool):
        on_time = None
    prepared_at = value.get("prepared_at")
    if not isinstance(prepared_at, str) or not prepared_at:
        prepared_at = case.updated_at

    raw_new_spend = value.get("new_spend")
    new_spend = (
        None if raw_new_spend is None else _number("new_spend", raw_new_spend)
    )
    checks = _normalize_checks(value.get("checks"))

    fallback_policy = canonical_digest(
        {"scenario": case.scenario, "order": case.order, "policy": "synthetic-v1"}
    )
    fallback_snapshot = canonical_digest(
        {"order": case.order, "disruption": case.disruption}
    )
    cost_delta = _number("cost_delta", value.get("cost_delta"))
    result = {
        "plan_id": plan_id,
        "actions": actions,
        "margin": _number("margin", value.get("margin")),
        "on_time": on_time,
        "new_spend": new_spend,
        "checks": checks,
        "cost_delta": cost_delta,
        "costs": project_cost_report(value.get("costs")),
        "policy_sha256": _plan_hash(
            "policy_sha256", value.get("policy_sha256"), fallback_policy
        ),
        "snapshot_sha256": _plan_hash(
            "snapshot_sha256", value.get("snapshot_sha256"), fallback_snapshot
        ),
        "prepared_at": prepared_at,
    }
    if "executor_binding" in value:
        result["executor_binding"] = normalize_executor_binding(case, value["executor_binding"])
    if "objective" in value:
        if type(value["objective"]) is not str or value["objective"] not in PRODUCT_OBJECTIVE_KINDS:
            raise ValueError("invalid_product_objective")
        result["objective"] = value["objective"]
    if "capacity" in value:
        result["capacity"] = project_capacity(value["capacity"], plan_id=plan_id, snapshot_sha256=result["snapshot_sha256"])
    if "order_readiness" in value:
        result["order_readiness"] = project_order_readiness(value["order_readiness"], plan_id=plan_id,
                                                          snapshot_sha256=result["snapshot_sha256"])
    if "decision_metrics" in value:
        result["decision_metrics"] = _bound_decision_metrics({**result, "decision_metrics": value["decision_metrics"]})
    if "impact_chain" in value:
        result["impact_chain"] = _bound_impact_chain({**result, "impact_chain": value["impact_chain"]})
    result["checks"] = _bound_checks(result)
    _bound_costs(result)
    return result


def normalize_execution(value: Any) -> dict[str, Any]:
    """Validate an executor result and project it onto the CaseFile shape."""
    if not isinstance(value, dict):
        raise ValueError("invalid_execution")
    status = value.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("invalid_execution_status")
    writes = value.get("writes")
    if writes is None:
        writes = []
    if not isinstance(writes, list):
        raise ValueError("invalid_execution_writes")
    execution = {
        "status": status,
        "readback": value.get("readback"),
        "verifier": value.get("verifier"),
        "writes": writes,
    }
    failure = failure_projection(value.get("failure"))
    if failure is not None:
        execution["failure"] = failure
    try:
        json.dumps(execution, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("invalid_execution") from None
    return execution


def failed_execution(*, failure=None, plan_id=None) -> dict[str, Any]:
    """Return the explicit execution record for an executor exception."""
    result = {"status": "failed", "readback": None, "verifier": None, "writes": []}
    projected = failure_projection(failure, plan_id=plan_id)
    if projected is not None:
        result["failure"] = projected
    return result


def execution_result(execution: Any, state: str) -> dict[str, Any]:
    """Project recorded evidence without upgrading legacy threshold passes."""
    record = execution if isinstance(execution, dict) else {}
    readback = record.get("readback")
    readback = readback if isinstance(readback, dict) else {}
    verifier = record.get("verifier")
    verifier = verifier if isinstance(verifier, dict) else {}
    synthetic = verifier.get("source") == "synthetic_executor"
    original = not synthetic and verifier.get("evaluation_scope") == "product_original_verifier"
    valid = verifier.get("evaluation_valid")
    valid = valid if isinstance(valid, bool) else None
    strict = verifier.get("strict_success")
    strict = strict if isinstance(strict, bool) else None
    if "rules" in verifier:
        # Preserve raw evidence and the benchmark's existing applicability
        # definition. Contradictory rules can revoke a receipt's strict claim,
        # but a legacy receipt missing strict is never upgraded from its rules.
        from forgeflow.runner import rule_projection

        projection = rule_projection(verifier)
        if projection is not None and projection["issue"] is not None:
            valid = False
        if projection is not None and projection["all_pass"] is False:
            strict = False
        elif strict is True and (projection is None or projection["all_pass"] is None):
            strict = None
    # An inconsistent receipt is never a strict pass, even if a boolean says so.
    checks = verifier.get("checks")
    checks = checks if isinstance(checks, list) else []
    failed = verifier.get("failed_rule_count")
    if (isinstance(failed, int) and failed > 0) or any(
        isinstance(check, dict) and (check.get("passed") is False or check.get("status") == "FAIL")
        for check in checks
    ):
        strict = False
    if valid is not True and strict is True:
        strict = None
    if strict is True and not (original or synthetic):
        # A pass from an unidentified checker is not original ERP acceptance.
        strict = None
    execution_status = record.get("status")
    readback_status = (
        "passed" if readback.get("execution_validated") is True
        else "failed" if readback.get("execution_validated") is False else "unknown"
    )
    passed = execution_status in SUCCESS_STATUSES and readback_status == "passed" and strict is True
    verified = passed and original
    simulation_passed = passed and synthetic
    if verified:
        outcome = "verified"
    elif simulation_passed:
        outcome = "simulation_passed"
    elif execution_status in SUCCESS_STATUSES:
        outcome = "verification_failed" if strict is False or verifier.get("status") == "failed" else "verification_unknown"
    elif execution_status == "not_started":
        outcome = "not_started"
    elif state == EXECUTION_UNKNOWN or execution_status == "unknown":
        outcome = EXECUTION_UNKNOWN
    else:
        outcome = state
    return {
        "execution_status": execution_status,
        "readback_status": readback_status,
        "verifier_status": verifier.get("status", "unknown"),
        "strict_success": strict,
        "evaluation_valid": valid,
        "verified": verified,
        "simulation_passed": simulation_passed,
        "verification_source": "original_verifier" if original else "synthetic_executor" if synthetic else "unknown",
        "outcome": outcome,
    }


def approval_decision(case: "ProductCase") -> dict[str, Any]:
    """One fail-closed policy shared by API views and the approval service.

    Required hard checks must pass. Other named checks may warn/be unknown;
    a block always blocks. Missing cost measurements are not themselves checks.
    """
    try:
        checks = _bound_checks(case.plan or {})
        invalid_budget = False
    except (ValueError, TypeError, KeyError):
        checks = (case.plan or {}).get("checks") or []
        invalid_budget = True
    states = {check.get("code"): check.get("status") for check in checks if isinstance(check, dict)}
    if invalid_budget:
        states["budget"] = "unknown"
    if "budget_policy" in states:
        states.setdefault("budget", states["budget_policy"])
        states.setdefault("policy", states["budget_policy"])
    blocked = sorted(str(code) for code, status in states.items() if status == "block")
    unknown = sorted(code for code in REQUIRED_CHECKS if states.get(code) in (None, "unknown"))
    warnings = sorted(str(code) for code, status in states.items() if status == "warn")
    hard_warnings = sorted(REQUIRED_CHECKS.intersection(warnings))
    reason = None
    if case.execution is not None:
        reason = "plan_already_applied"
    elif (case.selection or {}).get("phase") == "pending":
        reason = "selection_confirmation_pending"
    elif case.state == AWAITING_SELECTION:
        reason = "candidate_selection_required"
    elif case.state != AWAITING_APPROVAL:
        reason = "case_not_approvable"
    elif case.plan is None:
        reason = "plan_not_prepared"
    elif blocked:
        reason = "business_check_blocked"
    elif unknown:
        reason = "required_checks_unknown"
    elif hard_warnings:
        reason = "hard_constraint_not_satisfied"
    mutable = (case.state in (PREPARING, AWAITING_APPROVAL, AWAITING_SELECTION, REVISION_REQUESTED)
               and case.execution is None and case.approval is None
               and (case.selection or {}).get("phase") != "pending")
    return {
        "can_approve": reason is None,
        "reason": reason,
        "blocked_checks": blocked,
        "unknown_checks": unknown,
        "warnings": warnings,
        "can_reject": mutable,
        # Executor availability and supported changes are added by the service.
        "can_request_changes": False,
        "revision_reason": "revision_unsupported",
    }


@dataclass(slots=True)
class ProductCase:
    """One synthetic product case, including its private owner identity."""

    case_id: str
    uid: str
    scenario: str
    state: str
    synthetic: bool
    order: dict[str, Any]
    disruption: dict[str, Any]
    created_at: str
    updated_at: str
    plan: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    reason: str | None = None
    revision: dict[str, Any] = field(default_factory=lambda: {
        "version": 1, "parent_plan_id": None, "reason": None, "changes": {},
    })
    history: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    preparation: dict[str, Any] | None = None
    preparation_history: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None
    selection: dict[str, Any] | None = None

    @classmethod
    def new(cls, uid: str, scenario: str) -> "ProductCase":
        spec = scenario_spec(scenario)
        stamp = utc_stamp()
        return cls(
            case_id=new_case_id(),
            uid=uid,
            scenario=scenario,
            state=PREPARING,
            synthetic=True,
            order=spec["order"],
            disruption=spec["disruption"],
            created_at=stamp,
            updated_at=stamp,
        )

    @classmethod
    def from_record(cls, record: Any) -> "ProductCase":
        """Rebuild a case from its persisted JSON, rejecting malformed files."""
        if not isinstance(record, dict):
            raise ValueError("invalid_case")
        case = cls(
            case_id=record["case_id"],
            uid=record["uid"],
            scenario=record["scenario"],
            state=record["state"],
            synthetic=record["synthetic"],
            order=copy.deepcopy(record["order"]),
            disruption=copy.deepcopy(record["disruption"]),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
            plan=copy.deepcopy(record.get("plan")),
            approval=copy.deepcopy(record.get("approval")),
            execution=copy.deepcopy(record.get("execution")),
            preparation=copy.deepcopy(record.get("preparation")),
            preparation_history=copy.deepcopy(record.get("preparation_history", [])),
            comparison=copy.deepcopy(record.get("comparison")),
            selection=copy.deepcopy(record.get("selection")),
            reason=record.get("reason"),
            revision=copy.deepcopy(record.get("revision") or {
                "version": 1, "parent_plan_id": None, "reason": None, "changes": {},
            }),
            history=copy.deepcopy(record.get("history", [])),
            audit=copy.deepcopy(record.get("audit", [])),
        )
        if not isinstance(case.case_id, str) or CASE_ID.fullmatch(case.case_id) is None:
            raise ValueError("invalid_case_id")
        if not isinstance(case.uid, str) or not case.uid:
            raise ValueError("invalid_owner")
        if case.scenario not in SCENARIOS:
            raise ValueError("invalid_scenario")
        if case.state not in PRODUCT_STATES:
            raise ValueError("invalid_state")
        if not isinstance(case.synthetic, bool):
            raise ValueError("invalid_synthetic")
        if not isinstance(case.order, dict) or not isinstance(case.disruption, dict):
            raise ValueError("invalid_scenario_data")
        if not isinstance(case.created_at, str) or not isinstance(case.updated_at, str):
            raise ValueError("invalid_timestamp")
        if case.plan is not None and not isinstance(case.plan, dict):
            raise ValueError("invalid_plan")
        if case.approval is not None and not isinstance(case.approval, dict):
            raise ValueError("invalid_approval")
        if case.execution is not None and not isinstance(case.execution, dict):
            raise ValueError("invalid_execution")
        if case.comparison is not None:
            from .candidate_store import validate_reference

            validate_reference(case, case.comparison)
        if case.selection is not None and (
            not isinstance(case.selection, dict) or set(case.selection) != {"phase", "intent", "requested_at", "error"}
            or case.selection["phase"] not in {"pending", "completed", "failed"}
            or not isinstance(case.selection["intent"], dict) or not isinstance(case.selection["requested_at"], str)
            or case.comparison is None
        ):
            raise ValueError("invalid_selection")
        if case.selection is not None:
            intent = case.selection["intent"]
            if (set(intent) != {"operation_id", "expected_revision", "plan_id", "comparison_id"}
                    or not isinstance(intent["operation_id"], str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", intent["operation_id"]) is None
                    or type(intent["expected_revision"]) is not int or not 0 < intent["expected_revision"] <= 10000
                    or not isinstance(intent["plan_id"], str) or PLAN_ID.fullmatch(intent["plan_id"]) is None
                    or intent["comparison_id"] != case.comparison["comparison_id"]
                    or case.comparison["root_revision"] != intent["expected_revision"] + (case.selection["phase"] == "completed")
                    or case.selection["error"] is not None and (not isinstance(case.selection["error"], str)
                        or re.fullmatch(r"[a-z0-9_]{1,64}", case.selection["error"]) is None)):
                raise ValueError("invalid_selection")
        if case.preparation is not None and (
            not isinstance(case.preparation, dict)
            or case.preparation.get("phase") not in {"pending", "completed", "failed"}
            or not isinstance(case.preparation.get("intent"), dict)
        ):
            raise ValueError("invalid_preparation")
        if not isinstance(case.preparation_history, list) or any(
            not isinstance(item, dict) or item.get("phase") != "failed"
            or not isinstance(item.get("intent"), dict) for item in case.preparation_history
        ):
            raise ValueError("invalid_preparation_history")
        if case.preparation is not None and case.preparation.get("kind") in {"revision", "reprepare"}:
            saved = case.preparation
            if (not isinstance(saved.get("changes"), dict) or not isinstance(saved.get("reason"), str)
                    or not isinstance(saved.get("requested_at"), str)
                    or type(saved["intent"].get("version")) is not int):
                raise ValueError("invalid_revision_intent")
            if saved["kind"] == "reprepare" and (
                not isinstance(saved.get("origin"), dict) or type(saved["origin"].get("version")) is not int
            ):
                raise ValueError("invalid_repreparation_intent")
        if case.reason is not None and not isinstance(case.reason, str):
            raise ValueError("invalid_reason")
        if not isinstance(case.revision, dict) or not isinstance(case.history, list) or not isinstance(case.audit, list):
            raise ValueError("invalid_history")
        _validate_saved_plan_facts(case)
        return case

    def to_record(self) -> dict[str, Any]:
        """Return the full private record written to the case store."""
        _validate_saved_plan_facts(self)
        return {
            "case_id": self.case_id,
            "uid": self.uid,
            "scenario": self.scenario,
            "state": self.state,
            "synthetic": self.synthetic,
            "order": copy.deepcopy(self.order),
            "disruption": copy.deepcopy(self.disruption),
            "plan": copy.deepcopy(self.plan),
            "approval": copy.deepcopy(self.approval),
            "execution": copy.deepcopy(self.execution),
            "preparation": copy.deepcopy(self.preparation),
            "preparation_history": copy.deepcopy(self.preparation_history),
            "comparison": copy.deepcopy(self.comparison),
            "selection": copy.deepcopy(self.selection),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "revision": copy.deepcopy(self.revision),
            "history": copy.deepcopy(self.history),
            "audit": copy.deepcopy(self.audit),
        }

    def view(self) -> dict[str, Any]:
        """Return exactly the frontend-facing CaseFile JSON shape."""
        return {
            "case_id": self.case_id,
            "scenario": self.scenario,
            "state": self.state,
            "synthetic": self.synthetic,
            "order": _project_order(self.order),
            "disruption": _project_disruption(self.disruption),
            "plan": None if self.plan is None else _project_plan(self.plan),
            "approval": (
                None if self.approval is None else _project_approval(self.approval)
            ),
            "execution": (
                None if self.execution is None else _project_execution(self.execution, (self.plan or {}).get("plan_id"))
            ),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "decision": approval_decision(self),
            "result": execution_result(self.execution, self.state),
            "revision": copy.deepcopy(self.revision),
            "history": [{**{key: copy.deepcopy(entry[key]) for key in ("version", "retired_at", "reason") if key in entry},
                         "plan": _project_plan(entry["plan"]) if entry.get("plan") else None}
                        for entry in self.history],
            "audit": [{key: copy.deepcopy(value) for key, value in entry.items() if key != "uid"}
                      for entry in self.audit],
        }
