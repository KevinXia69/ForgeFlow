"""Read-only impact-chain replay from managed, already-bound product evidence.

Never solve, start ERP, approve, apply, or alter the saved case/checkpoint. The
derived copy retains all recorded budget checks and execution results verbatim.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import runpy
from pathlib import Path

from product_candidate_acceptance import audit_comparison
from product_cost_acceptance import need

from forgeflow.planning.objectives import bound_objective
from forgeflow.product.impact import build_impact_chain
from forgeflow.recovery.product_constraints import load_constraints
from forgeflow.recovery.service import ARTIFACTS
from forgeflow.recovery.storage import digest, read_json

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reproject_case(store, original):
    replay = copy.deepcopy(original)
    reports, hashes = [], {}
    for index, row in enumerate([replay, *replay.get("history", [])]):
        comparison = row.get("comparison")
        need(isinstance(comparison, dict), "impact_comparison_required")
        options = {"current": index == 0, "require_decision_metrics": True}
        audit_comparison(store, original["case_id"], comparison, **options)
        # The first observation must survive repeated paths in historical rows.
        for path in (store / "product-comparisons" / (comparison["comparison_id"] + ".json"),
                     store / "product-decisions" / ("local-" + original["case_id"] + ".json")):
            hashes.setdefault(path, sha(path))
        previous_plans = {plan["plan_id"]: copy.deepcopy(plan) for plan in comparison["plans"]}
        need(comparison.get("selected_plan_id") is None or row.get("plan") is not None,
             "impact_selected_plan_missing")
        updates = []
        for plan in comparison["plans"]:
            directory = store / plan["plan_id"]
            manifest = read_json(directory / "manifest.json")
            names = [*ARTIFACTS, "manifest", "product-policy", "product-costs"]
            if "product_constraints_sha256" in manifest:
                names.append("product-constraints")
            for name in names:
                path = directory / (name + ".json")
                hashes.setdefault(path, sha(path))
            request, snapshot, blueprint, record, changes = (
                read_json(directory / (name + ".json"))
                for name in ("request", "snapshot", "blueprint", "plan", "change-set"))
            constraints = load_constraints(directory, manifest, request)
            objective = bound_objective(manifest, record)
            rebuilt = build_impact_chain(request, snapshot, blueprint, record, changes, plan["order_readiness"],
                plan_id=plan["plan_id"], snapshot_sha256=plan["snapshot_sha256"],
                product_constraints=constraints, product_objective=objective)
            updates.append({"plan_id": plan["plan_id"], "objective": objective,
                            "original_chain_recorded": "impact_chain" in plan,
                            "nodes": len(rebuilt["nodes"]), "edges": len(rebuilt["edges"])})
            plan["impact_chain"] = rebuilt
        if row.get("plan") is not None:
            previous_selected = previous_plans.get(row["plan"].get("plan_id"))
            need(previous_selected is not None
                 and row["plan"].get("plan_id") == comparison.get("selected_plan_id")
                 and all(row["plan"].get(key) == previous_selected.get(key)
                         for key in ("plan_id", "policy_sha256", "snapshot_sha256")),
                 "impact_selected_binding_changed")
            # Do not launder changed selected facts into a freshly derived chain.
            need(digest(row["plan"]) == digest(previous_selected), "impact_selected_mirror_changed")
            selected = next(plan for plan in comparison["plans"] if plan["plan_id"] == row["plan"]["plan_id"])
            row["plan"]["impact_chain"] = copy.deepcopy(selected["impact_chain"])
        audited = audit_comparison(store, original["case_id"], comparison, require_impact_chain=True, **options)
        reports.append({"current": index == 0, "audit": audited, "chains": updates})
    need(all(sha(path) == value for path, value in hashes.items()), "impact_sources_changed")
    return {"case_id": original["case_id"], "comparisons": reports, "replay_case": replay,
            "bound_sources_sha256": {str(path.relative_to(store)): value for path, value in hashes.items()},
            "bound_sources_unchanged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    need(not args.out.exists(), "impact_output_exists")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    launcher["_require_dedicated_revision_sandbox"]()
    record = launcher["_instance"]()
    need(record is not None and launcher["_candidate_capability"](record), "managed_candidates_required")
    launcher["_check_candidate_bundle"](True, launcher["_candidate_profile"](record, True))
    checkpoint_hash = sha(args.checkpoint)
    checkpoint = json.loads(args.checkpoint.read_text())
    need((checkpoint["scenario"], checkpoint["task"], checkpoint["container_id"]) ==
         (launcher["SCENARIO"], launcher["TASK"], record["container_id"]), "impact_wrong_checkpoint")
    cases = [reproject_case(launcher["SANDBOX"] / "store", case) for case in checkpoint["cases"]]
    need(sha(args.checkpoint) == checkpoint_hash, "impact_checkpoint_changed")
    result = {"scope": "retrospective_read_only_impact_reprojection_not_new_ERP_or_verifier",
              "saved_case_modified": False, "recorded_budget_checks_modified": False,
              "erp_or_model_called": False, "scenario": launcher["SCENARIO"],
              "checkpoint_sha256": checkpoint_hash, "checkpoint_unchanged": True, "cases": cases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"scenario": result["scenario"], "cases": len(cases), "read_only": True,
                      "comparisons": sum(len(case["comparisons"]) for case in cases)}))


if __name__ == "__main__":
    main()
