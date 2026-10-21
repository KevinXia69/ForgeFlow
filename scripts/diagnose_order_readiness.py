"""Read-only readiness recomputation through the existing managed launcher.

Use previously recorded public checkpoints and their bound planning artifacts.
Do not start ERP, solve, approve, execute, read a reward/reference plan or alter
the saved case. This is retrospective planning diagnosis, not new acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
from pathlib import Path

from product_candidate_acceptance import audit_comparison
from product_cost_acceptance import need

from forgeflow.product.readiness import build_order_readiness
from forgeflow.recovery.storage import read_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    need(not args.out.exists(), "readiness_output_exists")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    launcher["_require_dedicated_revision_sandbox"]()
    record = launcher["_instance"]()
    need(record is not None and launcher["_candidate_capability"](record), "managed_candidates_required")
    launcher["_check_candidate_bundle"](True)
    checkpoint = json.loads(args.checkpoint.read_text())
    need((checkpoint["scenario"], checkpoint["task"]) == (launcher["SCENARIO"], launcher["TASK"]),
         "readiness_wrong_checkpoint")
    store, cases = launcher["SANDBOX"] / "store", []
    for case in checkpoint["cases"]:
        comparison = case["comparison"]
        audit = audit_comparison(store, case["case_id"], comparison)
        plans = []
        for public in comparison["plans"]:
            directory = store / public["plan_id"]
            files = [directory / (name + ".json") for name in ("request", "blueprint", "plan", "change-set", "manifest")]
            before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]
            request, blueprint, plan, changes, _ = (read_json(path) for path in files)
            summary = build_order_readiness(request, blueprint, plan["plan"], changes,
                plan_id=public["plan_id"], snapshot_sha256=public["snapshot_sha256"])
            need(before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in files], "readiness_sources_changed")
            plans.append({"objective": public["objective"], "recomputed_order_readiness": summary})
        cases.append({"case_id": case["case_id"], "original_candidates_audited": audit["candidate_count"], "plans": plans})
    value = {"scope": "retrospective_read_only_bound_plan_recomputation_not_new_ERP_or_verifier",
             "saved_case_modified": False, "erp_or_model_called": False, "scenario": launcher["SCENARIO"],
             "checkpoint_sha256": hashlib.sha256(args.checkpoint.read_bytes()).hexdigest(), "cases": cases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"scenario": value["scenario"], "cases": len(cases),
                      "plans": sum(len(case["plans"]) for case in cases), "read_only": True}))


if __name__ == "__main__":
    main()
