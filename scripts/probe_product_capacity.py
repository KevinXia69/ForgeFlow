"""Offline product-capacity experiment on a managed instance's bound business snapshot.

Existing launcher/acceptance checks validate provenance. A new temporary generic
bundle executes current project code against the same immutable facts/policy.
No ERP/provider/reference verifier is opened; no product state or approval changes.
Raw input copies are private temporary files, removed on exit. Output is bounded
business metrics, independent validation outcomes and solver statuses, not plans
that can be selected/applied. Original evidence is never replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from product_candidate_acceptance import audit_comparison
from product_cost_acceptance import need

from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.recovery.storage import digest, read_json

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
from forgeflow.planning.tool import solve_blueprint,SolverFailure
from forgeflow.recovery.change_set import build_change_set,validate_projection
from forgeflow.recovery.product_costs import build_cost_report
from forgeflow.recovery.product_candidates import business_fingerprint
from forgeflow.contract import contract_from_payload,check_request,check_changes
data=json.loads(Path(sys.argv[2]).read_text())
request,snapshot,blueprint,policy=(data[k] for k in ('request','snapshot','blueprint','policy'))
contract=contract_from_payload(policy)
standards={(p['product']['code'],c['workcenter_code']):c['time_per_unit_minutes']
 for p in blueprint['manufactured_products'] for c in p['workcenter_choices']}
def business(changes):
    minutes={}
    manufactured=0
    finished_purchase=0
    for op in changes['operations']:
        row=op['after']
        if row is None: continue
        if op['kind']=='manufacturing_order':
            center=row['workcenter_code']
            minutes[center]=minutes.get(center,0)+row['quantity']*standards[(row['product_code'],center)]
            manufactured+=row['quantity']
        elif row.get('supply_role')=='finished':
            finished_purchase+=row['quantity']
    costs=build_cost_report(request,snapshot,blueprint,changes)
    return {'business_sha256':business_fingerprint(changes),'manufactured_units':manufactured,
            'finished_purchase_units':finished_purchase,'workcenter_minutes':minutes,
            'total_workcenter_minutes':sum(minutes.values()),'planned_cost':costs['planned']['total'],
            'changes':sum(op['action']!='keep' for op in changes['operations'])}
def trace_summary(trace):
    return [{k:call[k] for k in ('phase','status','objective_value','best_objective_bound','wall_time_seconds') if k in call}
             for call in trace.get('solve_calls',[])][:20]
result={'original':business(data['changes']),'objective':'preserve_capacity'}
try:
    plan=solve_blueprint(blueprint,root/'upstream',product_objective='preserve_capacity')
    result['trace']=trace_summary(plan['trace'])
    changes=build_change_set(request,snapshot,blueprint,plan,product_objective='preserve_capacity',
                            product_constraints=data['constraints'])
    validate_projection(changes,request,snapshot,blueprint,plan,product_objective='preserve_capacity',
                        product_constraints=data['constraints'])
    assert check_request(request,contract)['ok'] and check_changes(changes,contract)['ok']
    result.update(status='projection_and_original_contract_passed',proposed=business(changes),
                  actual_objective_kind=plan['plan']['objective_kind'],objective_value=plan['plan']['objective_value'])
except SolverFailure as error:
    result.update(status=error.reason,trace=trace_summary(error.trace))
except Exception as error:
    result.update(status='independent_validation_or_solver_failed',error_type=type(error).__name__)
    # ChangeSetError is a bounded project's validation reason, not ERP/provider stderr.
    if type(error).__name__=='ChangeSetError':result['validation_reason']=str(error)[:240]
print(json.dumps(result,allow_nan=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    need(not args.out.exists(), "capacity_probe_output_exists")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    launcher["_require_dedicated_revision_sandbox"]()
    instance = launcher["_instance"]()
    need(instance is not None and launcher["_candidate_capability"](instance), "managed_candidates_required")
    launcher["_check_candidate_bundle"](True)
    checkpoint = json.loads(args.checkpoint.read_text())
    need(checkpoint["task"] == launcher["TASK"] and checkpoint["scenario"] == launcher["SCENARIO"],
         "capacity_probe_wrong_checkpoint")
    need(len(checkpoint["cases"]) == 1, "capacity_probe_requires_one_case")
    case = checkpoint["cases"][0]
    store = launcher["SANDBOX"] / "store"
    audit = audit_comparison(store, case["case_id"], case["comparison"])
    plan_id = case["plan"]["plan_id"]
    directory = store / plan_id
    data = {key: read_json(directory / (name + ".json")) for key, name in {
        "request": "request", "snapshot": "snapshot", "blueprint": "blueprint",
        "policy": "product-policy", "changes": "change-set"}.items()}
    from forgeflow.recovery.product_constraints import load_constraints

    data["constraints"] = load_constraints(directory, read_json(directory / "manifest.json"), data["request"])
    with tempfile.TemporaryDirectory(prefix="forgeflow-capacity-probe-") as temporary:
        temp = Path(temporary).resolve()
        archive = temp / "planner.tar.gz"
        sources = build_planner_bundle(ROOT, archive, recovery=True)
        with tarfile.open(archive, "r:gz") as packed:
            packed.extractall(temp, filter="data")
        input_file = temp / "input.json"
        input_file.write_text(json.dumps(data, allow_nan=False))
        input_file.chmod(0o600)
        run = subprocess.run([sys.executable, "-I", "-B", "-c", PROBE, str(temp / "planner"), str(input_file)],
                             capture_output=True, text=True, timeout=90)
        need(run.returncode == 0, "capacity_probe_process_failed")
        result = json.loads(run.stdout)
    result.update(scope="offline actual saved A/B business snapshot; not ERP/verifier acceptance",
                  scenario=launcher["SCENARIO"], inputs=audit["same_inputs"],
                  new_bundle_sources_sha256=digest(sources),
                  source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  erp_or_model_or_product_mutation_called=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
