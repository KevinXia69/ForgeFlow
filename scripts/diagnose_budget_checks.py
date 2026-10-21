"""Read-only budget reprojection of managed, already-recorded product trials.

Audit immutable planning inputs and return a separately labelled replay copy.
Never start ERP, solve, approve, execute, or alter the original saved case.
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

from forgeflow.product.budget_checks import build_budget_check
from forgeflow.recovery.service import ARTIFACTS
from forgeflow.recovery.storage import read_json

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reproject_case(store, original, *, require_decision_metrics=True):
    replay = copy.deepcopy(original)
    reports, hashes = [], {}
    for index, row in enumerate([replay, *replay.get("history", [])]):
        comparison = row.get("comparison")
        need(isinstance(comparison, dict), "budget_comparison_required")
        options = {"current": index == 0, "require_decision_metrics": require_decision_metrics}
        audit_comparison(store, original["case_id"], comparison, **options)
        previous_plans = {plan["plan_id"]: copy.deepcopy(plan) for plan in comparison["plans"]}
        changes = []
        for plan in comparison["plans"]:
            directory = store / plan["plan_id"]
            for name in (*ARTIFACTS, "manifest", "product-policy"):
                path = directory / (name + ".json")
                hashes.setdefault(path, sha(path))
            request, snapshot = (read_json(directory / (name + ".json")) for name in ("request", "snapshot"))
            previous = [check for check in plan["checks"] if check.get("code") == "budget"]
            need(len(previous) == 1, "budget_original_check_required")
            rebuilt = build_budget_check(request, snapshot, plan_id=plan["plan_id"],
                                         snapshot_sha256=plan["snapshot_sha256"])
            plan["checks"] = [rebuilt if check.get("code") == "budget" else check for check in plan["checks"]]
            changes.append({"plan_id": plan["plan_id"], "objective": plan["objective"],
                            "original_check": previous[0], "recomputed_check": rebuilt})
        if row.get("plan") is not None:
            previous_selected = previous_plans.get(row["plan"].get("plan_id"))
            need(previous_selected is not None
                 and row["plan"].get("plan_id") == comparison.get("selected_plan_id")
                 and all(row["plan"].get(key) == previous_selected.get(key)
                         for key in ("plan_id", "policy_sha256", "snapshot_sha256")),
                 "budget_selected_binding_changed")
            selected = next((plan for plan in comparison["plans"]
                             if plan["plan_id"] == row["plan"]["plan_id"]), None)
            need(selected is not None, "budget_selected_plan_missing")
            old_checks = row["plan"]["checks"]
            need(len([check for check in old_checks if check.get("code") == "budget"]) == 1,
                 "budget_selected_check_missing")
            need([check for check in old_checks if check.get("code") == "budget"] ==
                 [check for check in previous_selected["checks"] if check.get("code") == "budget"],
                 "budget_selected_check_changed")
            rebuilt = next(check for check in selected["checks"] if check.get("code") == "budget")
            row["plan"]["checks"] = [copy.deepcopy(rebuilt) if check.get("code") == "budget" else check
                                     for check in old_checks]
        audited = audit_comparison(store, original["case_id"], comparison, require_budget_basis=True, **options)
        reports.append({"current": index == 0, "audit": audited, "checks": changes})
    need(all(sha(path) == value for path, value in hashes.items()), "budget_sources_changed")
    return {"case_id": original["case_id"], "comparisons": reports, "replay_case": replay,
            "bound_sources_sha256": {str(path.relative_to(store)): value for path, value in hashes.items()},
            "bound_sources_unchanged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    need(not args.out.exists(), "budget_output_exists")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    launcher["_require_dedicated_revision_sandbox"]()
    record = launcher["_instance"]()
    need(record is not None and launcher["_candidate_capability"](record), "managed_candidates_required")
    launcher["_check_candidate_bundle"](True, launcher["_candidate_profile"](record, True))
    checkpoint_hash = sha(args.checkpoint)
    checkpoint = json.loads(args.checkpoint.read_text())
    need((checkpoint["scenario"], checkpoint["task"], checkpoint["container_id"]) ==
         (launcher["SCENARIO"], launcher["TASK"], record["container_id"]), "budget_wrong_checkpoint")
    cases = [reproject_case(launcher["SANDBOX"] / "store", case) for case in checkpoint["cases"]]
    need(sha(args.checkpoint) == checkpoint_hash, "budget_checkpoint_changed")
    result = {"scope": "retrospective_read_only_budget_reprojection_not_new_ERP_or_verifier",
              "saved_case_modified": False, "erp_or_model_called": False,
              "scenario": launcher["SCENARIO"], "checkpoint_sha256": checkpoint_hash,
              "checkpoint_unchanged": True, "cases": cases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"scenario": result["scenario"], "cases": len(cases), "read_only": True,
                      "comparisons": sum(len(case["comparisons"]) for case in cases)}))


if __name__ == "__main__":
    main()
