"""Read-only preference diagnosis using a managed acceptance instance's bound facts.

The existing launcher validates the dedicated instance and pinned generic bundle.
No ERP, provider, reference solutions, prepare, selection or approval is opened.
Only whitelisted business numbers/codes and public dependency enum values leave
the helper. Existing plans, bundles and acceptance checkpoints are never edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

from product_candidate_acceptance import audit_comparison
from product_cost_acceptance import need

from forgeflow.recovery.storage import read_json

ROOT = Path(__file__).resolve().parents[1]


def fields(value, keys):
    need(isinstance(value, dict), "candidate_diagnosis_invalid_record")
    result = {key: value[key] for key in keys if key in value}
    need(all(item is None or type(item) in (bool, int, float)
             or isinstance(item, str) and len(item) <= 256 for item in result.values()),
         "candidate_diagnosis_invalid_scalar")
    return result


def rows(value, keys):
    need(isinstance(value, list) and len(value) <= 500, "candidate_diagnosis_invalid_rows")
    return [fields(row, keys) for row in value]


def summarize_blueprint(blueprint):
    repair = blueprint["repair_context"]
    need(not repair.get("baseline_plan"), "reference_plan_not_allowed")
    manufacturing = blueprint["manufactured_products"]
    need(isinstance(manufacturing, list) and len(manufacturing) <= 100,
         "candidate_diagnosis_invalid_manufacturing")
    return {
        "objective_kind": blueprint["objective_kind"], "margin": blueprint["margin"],
        "product": fields(blueprint["product"], ("code", "list_price_dollars")),
        "orders": rows(blueprint["customers"], ("task_order_ref", "demand", "deadline", "budget_dollars")),
        "offers": rows(blueprint["vendors"], ("ref", "product_code", "supply_role", "price_dollars",
                                               "lead_time", "min_qty", "max_qty")),
        "manufacturing": [{"product_code": item["product"]["code"],
            "choices": rows(item["workcenter_choices"], ("workcenter_code", "unit_cost_dollars",
                             "lead_time_days", "time_per_unit_minutes"))} for item in manufacturing],
        "capacity": rows(blueprint["workcenters"], ("code", "capacity_minutes")),
        "existing_purchase": rows(repair["seeded_purchase_orders"], ("ref", "product_code", "quantity",
                                    "vendor_ref", "price_unit", "planned_arrival_days", "supply_role")),
        "existing_manufacturing": rows(repair["seeded_manufacturing_orders"], ("ref", "product_code",
                                        "quantity", "workcenter_code", "start_days", "deadline_days")),
    }


def runtime_objectives(bundle):
    script = """
import json,sys
sys.path.insert(0,sys.argv[1])
from procurement.procurement_objectives import ProcurementObjectiveKind
print(json.dumps([item.value for item in ProcurementObjectiveKind]))
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(bundle / "upstream")],
                            capture_output=True, text=True, timeout=20)
    need(result.returncode == 0, "candidate_diagnosis_runtime_import_failed")
    values = json.loads(result.stdout)
    need(isinstance(values, list) and len(values) <= 50
         and all(isinstance(item, str) and len(item) <= 64 for item in values),
         "candidate_diagnosis_runtime_values_invalid")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    need(not args.out.exists(), "candidate_diagnosis_output_exists")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    launcher["_require_dedicated_revision_sandbox"]()
    record = launcher["_instance"]()
    need(record is not None and launcher["_candidate_capability"](record), "managed_candidates_required")
    launcher["_check_candidate_bundle"](True)
    checkpoint = json.loads(args.checkpoint.read_text())
    need(checkpoint["scenario"] == launcher["SCENARIO"] and checkpoint["task"] == launcher["TASK"],
         "candidate_diagnosis_wrong_checkpoint")
    store, bundle = launcher["SANDBOX"] / "store", launcher["SANDBOX"] / "planner"
    cases = []
    for case in checkpoint["cases"]:
        comparison = case["comparison"]
        audit = audit_comparison(store, case["case_id"], comparison)
        plan_id = comparison["plans"][0]["plan_id"]
        plan = read_json(store / plan_id / "plan.json")
        blueprint = read_json(store / plan_id / "blueprint.json")
        request = read_json(store / plan_id / "request.json")
        cases.append({"case_id": case["case_id"], "input_bindings": audit["same_inputs"],
            "candidate_count": audit["candidate_count"], "attempts": audit["attempts"],
            "disruption": fields(request["disruption"], ("kind", "vendor_ref", "offer_key", "workcenter_code")),
            "blueprint_facts": summarize_blueprint(blueprint),
            "retained_objective_kind": plan["plan"]["objective_kind"],
            "retained_objective_value": plan["plan"]["objective_value"],
            "retained_planning_cost": case["plan"]["new_spend"]})
    result = {"scope": "read_only_bound_business_facts_and_runtime_enum_not_new_solver_or_ERP_acceptance",
        "erp_or_model_or_mutation_called": False, "scenario": launcher["SCENARIO"],
        "generic_runtime_objectives": runtime_objectives(bundle), "cases": cases,
        "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (Path(__file__).resolve(), ROOT / "scripts/product_candidate_acceptance.py")}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"scenario": result["scenario"], "cases": len(cases),
                      "runtime_objectives": result["generic_runtime_objectives"]}))


if __name__ == "__main__":
    main()
