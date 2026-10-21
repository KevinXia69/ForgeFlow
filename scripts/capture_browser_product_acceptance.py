"""Read-only checkpoints for a manually operated, dedicated live workbench.

Use the existing launcher configuration and same local owner. Never prepare,
revise, approve, reconcile or execute; the browser performs those actions.
Evidence contains public case projections and table digests, never credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from forgeflow.recovery.storage import digest

ROOT = Path(__file__).resolve().parents[1]


def sources():
    paths = [Path(__file__).resolve(), ROOT / "scripts/local_workbench.py",
             ROOT / "scripts/product_cost_acceptance.py",
             ROOT / "scripts/product_candidate_acceptance.py",
             ROOT / "scripts/product_acceptance_snapshot.py", ROOT / "web/package-lock.json"]
    for folder in ("src/forgeflow", "web/src", "contracts"):
        paths += [p for p in (ROOT / folder).rglob("*") if p.is_file()
                  and p.suffix in {".py", ".ts", ".tsx", ".css", ".json"}
                  and "__pycache__" not in p.parts and "static" not in p.parts]
    paths += [p for p in (ROOT / "src/forgeflow/api/static/app").rglob("*") if p.is_file()]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(paths))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--phase", choices=("initial", "after-prepare", "after-selection", "before-approval",
                                           "after-approval", "after-reload"),
                        required=True)
    parser.add_argument("--require-costs", action="store_true", help="Audit new current and historical product cost artifacts")
    parser.add_argument("--require-candidates", action="store_true", help="Audit all candidates, including unselected plans")
    parser.add_argument("--require-decision-metrics", action="store_true",
                        help="Require and independently audit decision, capacity and readiness facts on every candidate; implies --require-candidates")
    parser.add_argument("--require-budget-basis", action="store_true",
                        help="Require independent per-order budget-check audit on every candidate; implies --require-candidates")
    parser.add_argument("--require-impact-chain", action="store_true",
                        help="Require independent impact-chain audit on every candidate and selected copy; implies --require-candidates")
    args = parser.parse_args()
    if args.require_decision_metrics or args.require_budget_basis or args.require_impact_chain:
        args.require_candidates = True
    args.out.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out / (args.phase + ".json")
    if checkpoint.exists():
        raise SystemExit("checkpoint already exists; preserve it")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    record = launcher["_instance"]()
    if not launcher["_revision_capability"](record):
        raise SystemExit("managed revision-enabled instance required")
    if args.require_candidates and not launcher["_candidate_capability"](record):
        raise SystemExit("managed candidate-enabled instance required")
    info = launcher["_check_container"](launcher["_container_info"](), record)
    table_path = args.out / (args.phase + "-tables.json")
    tables = subprocess.run([
        sys.executable, str(ROOT / "scripts/product_acceptance_snapshot.py"),
        "--container", launcher["CONTAINER"], "--label", args.phase, "--out", str(table_path),
    ], capture_output=True, timeout=120)
    if tables.returncode:
        raise SystemExit("table snapshot failed; no browser action retried")
    with httpx.Client(base_url=f"http://127.0.0.1:{launcher['PORT']}", trust_env=False, timeout=30) as http:
        if http.post("/api/auth/session", json={"invite_code": launcher["INVITE"]}).status_code != 201:
            raise SystemExit("acceptance login failed")
        response = http.get("/api/product/cases")
        response.raise_for_status()
        cases = []
        for case in response.json():
            detail = http.get("/api/product/cases/" + case["case_id"])
            detail.raise_for_status()
            cases.append(detail.json())
    source_sha = sources()
    candidate_audits = []
    if args.require_candidates:
        from product_candidate_acceptance import audit_comparison

        for case in cases:
            for current, row in [(True, case), *((False, row) for row in case.get("history", []))]:
                comparison = row.get("comparison")
                if (args.require_impact_chain and (not current or args.phase != "initial" or row.get("plan") is not None)
                        and (not isinstance(comparison, dict) or not comparison)):
                    raise SystemExit("impact-chain acceptance requires every current and historical candidate set; no browser action retried")
                if (args.require_impact_chain and isinstance(comparison, dict)
                        and comparison.get("selected_plan_id") is not None and row.get("plan") is None):
                    raise SystemExit("selected impact chain missing or different from audited candidate; no browser action retried")
                if args.require_decision_metrics and row.get("plan") is not None:
                    selected = row["plan"]
                    candidates = comparison.get("plans", []) if isinstance(comparison, dict) else []
                    audited = next((plan for plan in candidates
                                    if plan.get("plan_id") == selected.get("plan_id")), None)
                    if audited is None or any(
                            not isinstance(selected.get(field), dict) or not selected[field]
                            or selected[field] != audited.get(field)
                            for field in ("decision_metrics", "capacity", "order_readiness")):
                        raise SystemExit("selected decision facts missing or different from audited candidate; no browser action retried")
                if args.require_budget_basis and row.get("plan") is not None:
                    selected = row["plan"]
                    candidates = comparison.get("plans", []) if isinstance(comparison, dict) else []
                    audited = next((plan for plan in candidates
                                    if plan.get("plan_id") == selected.get("plan_id")), None)
                    selected_checks = [check for check in selected.get("checks", [])
                                       if isinstance(check, dict) and check.get("code") == "budget"]
                    audited_checks = [check for check in (audited or {}).get("checks", [])
                                      if isinstance(check, dict) and check.get("code") == "budget"]
                    if (audited is None or selected.get("plan_id") != comparison.get("selected_plan_id")
                            or any(selected.get(key) != audited.get(key) for key in ("policy_sha256", "snapshot_sha256"))
                            or len(selected_checks) != 1 or "basis" not in selected_checks[0] or selected_checks != audited_checks):
                        raise SystemExit("selected budget check missing or different from audited candidate; no browser action retried")
                if args.require_impact_chain and row.get("plan") is not None:
                    selected = row["plan"]
                    candidates = comparison.get("plans", []) if isinstance(comparison, dict) else []
                    audited = next((plan for plan in candidates
                                    if plan.get("plan_id") == selected.get("plan_id")), None)
                    if (audited is None or selected.get("plan_id") != comparison.get("selected_plan_id")
                            or any(selected.get(key) != audited.get(key) for key in ("policy_sha256", "snapshot_sha256"))
                            or not isinstance(selected.get("impact_chain"), dict) or not selected["impact_chain"]
                            or digest(selected["impact_chain"]) != digest(audited.get("impact_chain"))):
                        raise SystemExit("selected impact chain missing or different from audited candidate; no browser action retried")
                if comparison:
                    candidate_audits.append({"case_id": case["case_id"], "current": current,
                        **audit_comparison(launcher["SANDBOX"] / "store", case["case_id"], comparison,
                                           current=current, require_decision_metrics=args.require_decision_metrics,
                                           require_budget_basis=args.require_budget_basis,
                                           require_impact_chain=args.require_impact_chain)})
        if args.phase != "initial" and not candidate_audits:
            raise SystemExit("candidate acceptance requires a recorded set; no browser action retried")
    cost_audits = []
    if args.require_costs:
        from product_cost_acceptance import audit_plan_costs

        for case in cases:
            plans = []
            for row in [case, *case.get("history", [])]:
                plans += [row.get("plan"), *(row.get("comparison") or {}).get("plans", [])]
            seen = set()
            for plan in plans:
                if plan and plan["plan_id"] not in seen:
                    seen.add(plan["plan_id"])
                    cost_audits.append({"case_id": case["case_id"],
                                       **audit_plan_costs(launcher["SANDBOX"] / "store", plan)})
        if args.phase != "initial" and not cost_audits:
            raise SystemExit("cost acceptance requires a recorded plan; no browser action retried")
    result = {
        "scope": "read_only_manual_browser_product_checkpoint", "phase": args.phase,
        "recorded_at": datetime.now(UTC).isoformat(), "scenario": launcher["SCENARIO"],
        "task": launcher["TASK"], "container_id": info["id"], "cases": cases,
        "revision_binding": record.get("features", record.get("acceptance")),
        "revision_binding_source": "features" if "features" in record else "legacy_acceptance",
        "revision_overrides_present": [name for name in ("FORGEFLOW_LOCAL_REVISIONS",
                                                       "FORGEFLOW_LOCAL_REVISION_ACCEPTANCE")
                                       if name in os.environ],
        "source_sha256": source_sha, "called_prepare_or_approval_or_apply": False,
        "autonomous_benchmark": False, "model_called_by_capture": False,
        "cost_audits_required": args.require_costs, "cost_audits": cost_audits,
        "candidate_audits_required": args.require_candidates, "candidate_audits": candidate_audits,
        "decision_metrics_required": args.require_decision_metrics,
        "budget_basis_required": args.require_budget_basis,
        "impact_chain_required": args.require_impact_chain,
    }
    if args.phase != "initial":
        initial = json.loads((args.out / "initial.json").read_text())
        result["source_unchanged"] = source_sha == initial["source_sha256"]
        result["same_container"] = info["id"] == initial["container_id"]
        initial_tables = json.loads((args.out / "initial-tables.json").read_text())["tables"]
        current_tables = json.loads(table_path.read_text())["tables"]
        result["tables_unchanged_from_initial"] = initial_tables == current_tables
    checkpoint.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"phase": args.phase, "case_count": len(cases),
                      "states": [c["state"] for c in cases],
                      "source_unchanged": result.get("source_unchanged"),
                      "tables_unchanged_from_initial": result.get("tables_unchanged_from_initial")}))


if __name__ == "__main__":
    main()
