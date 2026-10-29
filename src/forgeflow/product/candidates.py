"""Bound candidate sets crossing from root artifacts into the local product.

The returned set is private service data. HTTP callers must use the explicit
projection; root context, comparison hashes and per-plan authority stay private.
"""
from __future__ import annotations

import copy
import re

from forgeflow.planning.objectives import CAPACITY_PRODUCT_OBJECTIVES, canonical_objectives
from forgeflow.recovery.change_set import CHANGE_SET_REASONS
from forgeflow.recovery.storage import StorageError, digest, read_json

from .executor import ProductExecutionError
from .state import _project_plan, normalize_plan


def load_local_comparison(executor, case, decision, constraints):
    """Project only a complete root-owned comparison with its original audit."""
    try:
        if (not isinstance(decision, dict) or decision.get("run_id") != f"local-{case['case_id']}"
                or decision.get("state") not in {"prepared", "selected"}
                or decision.get("version") != case.get("revision", {}).get("version", 1)
                or type(decision.get("revision")) is not int):
            raise ValueError
        comparison_id = decision.get("comparison_id")
        if not isinstance(comparison_id, str) or re.fullmatch(r"[a-f0-9]{32}", comparison_id) is None:
            raise ValueError
        objectives = list(executor.candidate_objectives)
        rows = {key: row for key, row in decision["plans"].items() if row["state"] != "retired"}
        if not 1 <= len(rows) <= len(objectives):
            raise ValueError
        selected = decision.get("selected_plan_id")
        if (selected is None and (len(rows) == 1 or decision["state"] != "prepared")
                or selected is not None and (selected not in rows or decision["state"] != "selected")):
            raise ValueError
        directory = executor.store / "product-comparisons"
        if directory.is_symlink():
            raise ValueError
        audit = read_json(directory / (comparison_id + ".json"))
        audit_sha = digest(audit)
        context = next(iter(rows.values()))["product"]
        candidate_ids = audit.get("candidate_plan_ids")
        if (audit.get("schema_version") != 1 or audit.get("state") != "prepared"
                or audit.get("product") != context
                or audit.get("product_objectives") != objectives
                or not isinstance(candidate_ids, list) or len(candidate_ids) != len(rows)
                or any(type(key) is not str for key in candidate_ids)
                or set(candidate_ids) != set(rows)
                or any(row.get("comparison_sha256") != audit_sha or row.get("product") != context
                       for row in rows.values())):
            raise ValueError
        # JSON transports may sort mapping keys. The bound audit array, not
        # dictionary insertion order, defines the reviewed objective ordering.
        rows = {key: rows[key] for key in candidate_ids}
        plans = [executor._prepared_plan(case, decision, constraints, candidate_id=plan_id) for plan_id in rows]
        if (any(plan["executor_binding"]["product"] != context for plan in plans)
                or len({plan["objective"] for plan in plans}) != len(plans)
                or context.get("comparison_id") != comparison_id
                or context["inputs_sha256"] != digest({"request": executor.request,
                    "policy_sha256": plans[0]["policy_sha256"], "constraints": constraints,
                    "product_objectives": objectives,
                    **({"retention_profile": plans[0]["retention_refinement"]["profile"]}
                       if "retention_refinement" in plans[0] else {})})):
            raise ValueError
        facts = [(plan["policy_sha256"], plan["snapshot_sha256"],
                  digest(plan["costs"]["basis"])) for plan in plans]
        if any(fact != facts[0] for fact in facts):
            raise ValueError
        attempts = audit.get("attempts")
        if not isinstance(attempts, list) or len(attempts) != len(objectives):
            raise ValueError
        summaries = []
        for objective, attempt in zip(objectives, attempts, strict=True):
            if not isinstance(attempt, dict) or attempt.get("product_objective") != objective:
                raise ValueError
            status = attempt.get("state")
            summary = {"objective": objective, "status": status}
            if status == "prepared":
                plan_id = attempt.get("plan_id")
                if not any(plan["plan_id"] == plan_id and plan["objective"] == objective for plan in plans):
                    raise ValueError
                summary["plan_id"] = plan_id
            elif status == "duplicate":
                plan_id = attempt.get("equivalent_plan_id")
                if plan_id not in rows:
                    raise ValueError
                summary["equivalent_plan_id"] = plan_id
            elif status in {"infeasible", "rejected"}:
                reason = attempt.get("reason")
                if (status == "infeasible" and reason != "planning_model_infeasible"
                        or status == "rejected" and reason not in CHANGE_SET_REASONS | {"product_constraint_not_satisfied"}):
                    raise ValueError
                summary["reason"] = reason
            else:
                raise ValueError
            summaries.append(summary)
        if {s.get("plan_id") for s in summaries if s["status"] == "prepared"} != set(rows):
            raise ValueError
        return {"schema_version": 1, "comparison_id": comparison_id, "version": decision["version"],
                "root_revision": decision["revision"], "selected_plan_id": selected,
                "comparison_sha256": audit_sha, "plans": plans, "attempts": summaries}
    except (OSError, ValueError, TypeError, KeyError, StorageError):
        raise ProductExecutionError("product_comparison_invalid") from None


def normalize_comparison(case, value):
    """Validate a complete private set before saving it outside the case row."""
    keys = {"schema_version", "comparison_id", "version", "root_revision", "selected_plan_id",
            "comparison_sha256", "plans", "attempts"}
    if (not isinstance(value, dict) or set(value) != keys or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or type(value["version"]) is not int
            or value["version"] != case.revision["version"]
            or type(value["root_revision"]) is not int or value["root_revision"] < 1
            or not isinstance(value["plans"], list) or not 1 <= len(value["plans"]) <= len(CAPACITY_PRODUCT_OBJECTIVES)
            or not isinstance(value["comparison_id"], str)
            or re.fullmatch(r"[a-f0-9]{32}", value["comparison_id"]) is None
            or not isinstance(value["comparison_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", value["comparison_sha256"]) is None):
        raise ValueError("invalid_comparison")
    attempts = value["attempts"]
    if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
        raise ValueError("invalid_comparison")
    try:
        objectives = canonical_objectives([item.get("objective") for item in attempts])
    except ValueError:
        raise ValueError("invalid_comparison") from None
    if len(value["plans"]) > len(objectives):
        raise ValueError("invalid_comparison")
    plans = [normalize_plan(case, plan) for plan in value["plans"]]
    ids = [plan["plan_id"] for plan in plans]
    if (len(set(ids)) != len(ids) or (value["selected_plan_id"] is not None and value["selected_plan_id"] not in ids)
            or len(ids) == 1 and value["selected_plan_id"] != ids[0]
            or any(plan.get("costs") is None for plan in plans)
            or len({plan.get("objective") for plan in plans}) != len(plans)):
        raise ValueError("invalid_comparison")
    facts = [(plan["policy_sha256"], plan["snapshot_sha256"], digest(plan["costs"]["basis"])) for plan in plans]
    if any(row != facts[0] for row in facts):
        raise ValueError("invalid_comparison")
    first = plans[0]["executor_binding"]
    if any(plan.get("executor_binding") != first or plan.get("objective") not in objectives for plan in plans):
        raise ValueError("invalid_comparison")
    if first["revision"] != value["root_revision"] or first["product"]["comparison_id"] != value["comparison_id"]:
        raise ValueError("invalid_comparison")
    # Attempts cross the HTTP boundary, so allow only bounded reviewed fields.
    for objective, attempt in zip(objectives, attempts, strict=True):
        if not isinstance(attempt, dict) or attempt.get("objective") != objective:
            raise ValueError("invalid_comparison")
        status = attempt.get("status")
        if status == "prepared":
            valid = (set(attempt) == {"objective", "status", "plan_id"} and any(
                plan["plan_id"] == attempt["plan_id"] and plan["objective"] == objective for plan in plans))
        elif status == "duplicate":
            valid = set(attempt) == {"objective", "status", "equivalent_plan_id"} and attempt["equivalent_plan_id"] in ids
        elif status in {"infeasible", "rejected"}:
            reasons = {"planning_model_infeasible"} if status == "infeasible" else CHANGE_SET_REASONS | {"product_constraint_not_satisfied"}
            valid = set(attempt) == {"objective", "status", "reason"} and attempt["reason"] in reasons
        else:
            valid = False
        if not valid:
            raise ValueError("invalid_comparison")
    if {item.get("plan_id") for item in attempts if item["status"] == "prepared"} != set(ids):
        raise ValueError("invalid_comparison")
    return {**copy.deepcopy(value), "plans": plans}


def project_comparison(value):
    """Project a previously normalized set without root authority or raw traces."""
    return {"comparison_id": value["comparison_id"], "version": value["version"],
            "selection_revision": value["root_revision"],
            "selected_plan_id": value["selected_plan_id"], "plans": [_project_plan(plan) for plan in value["plans"]],
            "attempts": copy.deepcopy(value["attempts"])}
