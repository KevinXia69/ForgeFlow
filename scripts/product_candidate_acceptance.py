"""Read-only candidate auditing for authorized, dedicated product acceptance.

Only bounded public facts leave this helper. It does not prepare, select,
approve, apply, contact ERP or read reference solutions. Feasibility here is
the independent plan projection/contract check, not the final ERP verifier.
"""
from __future__ import annotations

import re
from pathlib import Path

from product_cost_acceptance import audit_plan_costs, need

from forgeflow.contract import check_changes, check_request, contract_from_payload
from forgeflow.planning.objectives import bound_objective, canonical_objectives
from forgeflow.product.budget_checks import build_budget_check
from forgeflow.product.capacity import build_capacity_summary
from forgeflow.product.decision_metrics import build_decision_metrics
from forgeflow.product.readiness import build_order_readiness
from forgeflow.recovery.change_set import validate_projection
from forgeflow.recovery.product_candidates import business_fingerprint
from forgeflow.recovery.product_constraints import load_constraints
from forgeflow.recovery.product_versions import ProductVersions
from forgeflow.recovery.service import ARTIFACTS, checked_id
from forgeflow.recovery.storage import digest, read_json


def audit_comparison(store: Path, case_id: str, public: dict, *, current: bool = True,
                     require_decision_metrics: bool = False, require_budget_basis: bool = False,
                     require_impact_chain: bool = False) -> dict:
    comparison_id = public["comparison_id"]
    need(isinstance(comparison_id, str) and re.fullmatch(r"[a-f0-9]{32}", comparison_id),
         "candidate_comparison_id_invalid")
    directory = store / "product-comparisons"
    need(not directory.is_symlink(), "candidate_audit_directory_invalid")
    audit = read_json(directory / (comparison_id + ".json"))
    context = audit["product"]
    need(audit["state"] == "prepared" and context["comparison_id"] == comparison_id
         and context["run_id"] == "local-" + case_id and context["version"] == public["version"],
         "candidate_audit_context_changed")
    plans = public["plans"]
    ids = [checked_id(plan["plan_id"]) for plan in plans]
    objectives = canonical_objectives(audit["product_objectives"])
    need(objectives is not None, "candidate_objectives_missing")
    need(1 <= len(ids) <= len(objectives) and len(set(ids)) == len(ids)
         and ids == audit["candidate_plan_ids"], "candidate_set_changed")
    need(public["selected_plan_id"] is None or public["selected_plan_id"] in ids,
         "candidate_selected_plan_missing")
    decision = ProductVersions(store).status(context["run_id"], context["owner_sha256"])
    if current:
        need(decision["comparison_id"] == comparison_id
             and decision["selected_plan_id"] == public["selected_plan_id"], "candidate_current_choice_changed")
    results, common, effects = [], None, set()
    for public_plan in plans:
        if require_decision_metrics:
            need(all(isinstance(public_plan.get(field), dict) and public_plan[field]
                     for field in ("decision_metrics", "capacity", "order_readiness")),
                 "candidate_decision_metrics_required")
        if require_impact_chain:
            need(isinstance(public_plan.get("impact_chain"), dict) and public_plan["impact_chain"],
                 "candidate_impact_chain_required")
        plan_id = public_plan["plan_id"]
        plan_dir = store / plan_id
        need(not plan_dir.is_symlink(), "candidate_plan_directory_invalid")
        manifest = read_json(plan_dir / "manifest.json")
        need(digest(manifest) == plan_id and manifest["product"] == context,
             "candidate_manifest_not_bound")
        need(decision["plans"][plan_id]["comparison_sha256"] == digest(audit),
             "candidate_audit_not_bound")
        artifacts = {name: read_json(plan_dir / (name + ".json")) for name in ARTIFACTS}
        need(manifest["artifacts"] == {name: digest(value) for name, value in artifacts.items()},
             "candidate_artifact_changed")
        request, snapshot, blueprint, plan, changes, _baseline, _binding = (
            artifacts[name] for name in ARTIFACTS)
        objective = bound_objective(manifest, plan)
        need(manifest["product_objectives"] == objectives, "candidate_objectives_changed")
        need(public_plan["objective"] == objective, "candidate_public_objective_changed")
        constraints = load_constraints(plan_dir, manifest, request)
        policy = read_json(plan_dir / "product-policy.json")
        contract = contract_from_payload(policy)
        need(policy == contract.payload()
             and manifest["policy_sha256"] == contract.digest() == public_plan["policy_sha256"],
             "candidate_policy_changed")
        validate_projection(changes, request, snapshot, blueprint, plan,
                            product_constraints=constraints, product_objective=objective)
        need(check_request(request, contract)["ok"] and check_changes(changes, contract)["ok"],
             "candidate_contract_check_failed")
        same = {name: manifest["artifacts"][name] for name in ("request", "snapshot", "blueprint", "baseline")}
        same.update(policy_sha256=contract.digest(), constraints_sha256=digest(constraints),
                    sources_sha256=digest(manifest["sources"]),
                    cost_basis_sha256=digest(public_plan["costs"]["basis"]))
        need(common is None or common == same, "candidate_inputs_not_comparable")
        common = same
        if "capacity" in public_plan:
            need(public_plan["capacity"] == build_capacity_summary(request, blueprint, changes,
                plan_id=plan_id, snapshot_sha256=public_plan["snapshot_sha256"]), "candidate_capacity_changed")
        if "order_readiness" in public_plan:
            need(public_plan["order_readiness"] == build_order_readiness(request, blueprint, plan["plan"], changes,
                plan_id=plan_id, snapshot_sha256=public_plan["snapshot_sha256"]), "candidate_order_readiness_changed")
        if "decision_metrics" in public_plan:
            need(public_plan["decision_metrics"] == build_decision_metrics(
                request, snapshot, blueprint, constraints, public_plan["costs"],
                public_plan.get("capacity"), public_plan.get("order_readiness"),
                plan_id=plan_id, snapshot_sha256=public_plan["snapshot_sha256"]), "candidate_decision_metrics_changed")
        if "impact_chain" in public_plan:
            from forgeflow.product.impact import build_impact_chain

            expected_impact = build_impact_chain(
                request, snapshot, blueprint, plan, changes, public_plan.get("order_readiness"),
                plan_id=plan_id, snapshot_sha256=public_plan["snapshot_sha256"],
                product_constraints=constraints, product_objective=objective)
            need(digest(public_plan["impact_chain"]) == digest(expected_impact), "candidate_impact_chain_changed")
        budget_checks = [check for check in public_plan.get("checks", [])
                         if isinstance(check, dict) and check.get("code") == "budget"]
        if require_budget_basis:
            need(len(budget_checks) == 1 and "basis" in budget_checks[0], "candidate_budget_basis_required")
        if any("basis" in check for check in budget_checks):
            expected_budget = build_budget_check(request, snapshot, plan_id=plan_id,
                                                 snapshot_sha256=public_plan["snapshot_sha256"])
            need(budget_checks == [expected_budget], "candidate_budget_check_changed")
        fingerprint = business_fingerprint(changes)
        need(fingerprint not in effects, "candidate_business_effects_duplicate")
        effects.add(fingerprint)
        attempt = next(item for item in audit["attempts"] if item.get("plan_id") == plan_id)
        need(attempt["state"] == "prepared" and attempt["business_sha256"] == fingerprint
             and attempt["product_objective"] == objective, "candidate_attempt_changed")
        results.append({"objective": objective, "business_sha256": fingerprint,
                        "projection_and_contract_valid": True, **audit_plan_costs(store, public_plan)})
    need(len(public["attempts"]) == len(audit["attempts"]) == len(objectives),
         "candidate_attempts_missing")
    for shown, recorded, objective in zip(public["attempts"], audit["attempts"], objectives, strict=True):
        expected = {"objective": objective, "status": recorded["state"]}
        for key in ("plan_id", "equivalent_plan_id", "reason"):
            if key in recorded:
                expected[key] = recorded[key]
        need(shown == expected, "candidate_attempt_projection_changed")
    return {"comparison_id": comparison_id, "version": public["version"], "candidate_count": len(ids),
            "same_inputs": common, "distinct_business_effects": True, "candidates": results,
            "attempts": public["attempts"], "selected_plan_id": public["selected_plan_id"],
            "decision_metrics_required": require_decision_metrics,
            "budget_basis_required": require_budget_basis,
            "impact_chain_required": require_impact_chain,
            "current_root_choice_matches": True if current else None,
            "scope": "recorded_projection_contract_and_cost_checks_not_final_erp_verifier",
            "erp_or_mutation_called": False}
