#!/usr/bin/env python3
"""Verify the default A/B product path in an explicitly named fresh sandbox.

The approval is an authorized synthetic acceptance-driver action. This is not
an autonomous benchmark or a human browser approval. Never resets ERP. Keeps
raw original reward and public case evidence, without keys or approval tokens.
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
from decimal import Decimal
from pathlib import Path

import httpx

from forgeflow.api.evidence import public_evidence
from forgeflow.recovery.margin import MARGIN_BASIS
from forgeflow.recovery.storage import digest, read_json

ROOT = Path(__file__).resolve().parents[1]


def need(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sources():
    paths = sorted((ROOT / "src/forgeflow").rglob("*.py"))
    paths += [Path(__file__).resolve(), ROOT / "scripts/local_workbench.py",
              ROOT / "scripts/product_acceptance_snapshot.py"]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def independent_margin(directory, public_plan):
    """Recalculate from persisted SO prices and finished supply flows."""
    manifest = read_json(directory / "manifest.json")
    need(digest(manifest) == public_plan["plan_id"], "manifest_identity_changed")
    need(manifest.get("margin_basis") == MARGIN_BASIS, "legacy_plan")
    snapshot = read_json(directory / "snapshot.json")
    changes = read_json(directory / "change-set.json")
    costs = read_json(directory / "product-costs.json")
    blueprint = read_json(directory / "blueprint.json")
    need(digest(costs) == manifest["product_costs_sha256"], "cost_identity_changed")
    need(digest(blueprint) == manifest["artifacts"]["blueprint"], "blueprint_identity_changed")
    plan = read_json(directory / "plan.json")
    for name, value in (("snapshot", snapshot), ("change-set", changes), ("plan", plan)):
        need(digest(value) == manifest["artifacts"][name], "artifact_identity_changed")
    orders = {row["id"]: row for row in snapshot["models"]["sale.order"]}
    lines = {(row["order_id"][0] if isinstance(row["order_id"], list) else row["order_id"]): row
             for row in snapshot["models"]["sale.order.line"] if not row["display_type"]}
    names = {row["name"]: sid for sid, row in orders.items()}
    revenue, units, stock = Decimal(0), 0, 0
    projected = changes["projected_state"]
    docs = {row["target"]["token"]: row for key in ("purchase_orders", "manufacturing_orders")
            for row in projected[key]}
    flows = projected["sales_order_flows"]
    for flow in flows:
        qty = flow["quantity"]
        source = flow["source_token"]
        if source.startswith("stock:"):
            stock += qty
        else:
            need(source in docs and docs[source]["product_code"] == flow["product_code"],
                 "unknown_finished_supply_kind")
            units += qty
            price = Decimal(str(lines[names[flow["sales_order"]["name"]]]["price_unit"]))
            revenue += price * qty
    standards = {choice["workcenter_code"]: Decimal(str(choice["unit_cost_dollars"]))
                 for item in blueprint["manufactured_products"] for choice in item["workcenter_choices"]}
    cost = sum((Decimal(str(row["unit_price"])) * row["quantity"] for row in projected["purchase_orders"]), Decimal(0))
    cost += sum((standards[row["workcenter_code"]] * row["quantity"] for row in projected["manufacturing_orders"]), Decimal(0))
    need(cost == Decimal(costs["planned"]["total"]), "cost_report_mismatch")
    ratio = None if revenue == 0 else (1 - cost / revenue) * 100
    expected = None if ratio is None else round(float(ratio), 2)
    need(public_plan["margin_basis"] == MARGIN_BASIS and public_plan["margin"] == expected,
         "public_margin_mismatch")
    metrics = public_plan["decision_metrics"]
    need(metrics["schema_version"] == 2 and metrics["basis"]["margin_basis"] == MARGIN_BASIS,
         "decision_metrics_basis_mismatch")
    need(Decimal(metrics["margin"]["new_spend_revenue"]) == revenue
         and metrics["margin"]["new_spend_units"] == units
         and metrics["margin"]["stock_units"] == stock, "decision_metrics_coverage_mismatch")
    return {"new_spend_revenue": str(revenue), "new_spend_units": units, "stock_units": stock,
            "planned_cost": str(cost), "margin_pct": None if ratio is None else str(ratio),
            "repair_objective": plan["plan"]["objective_value"], "margin_basis": MARGIN_BASIS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--authorize-product-approval", action="store_true")
    args = parser.parse_args()
    need(args.authorize_product_approval, "explicit_product_approval_required")
    required = ("FORGEFLOW_LOCAL_SANDBOX_ROOT", "FORGEFLOW_LOCAL_CASE_ROOT",
                "FORGEFLOW_LOCAL_CONTAINER_NAME", "FORGEFLOW_LOCAL_SOCKET_PATH",
                "FORGEFLOW_LOCAL_API_PORT", "FORGEFLOW_LOCAL_ODOO_PORT")
    need(all(os.environ.get(key) for key in required), "explicit_destinations_required")
    output = args.out.resolve()
    need(not output.exists(), "preserve_existing_evidence_use_new_directory")
    output.mkdir(parents=True)
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    need(launcher["_instance"]()["ready"], "sandbox_not_initialized")
    sandbox = launcher["SANDBOX"]
    report = {"category": "dedicated_synthetic_product_http_acceptance", "scenario": launcher["SCENARIO"],
              "approval_origin": "explicitly_authorized_acceptance_driver", "browser_approval": False,
              "autonomous_benchmark": False, "started_at": datetime.now(UTC).isoformat(),
              "source_sha256": sources(), "stages": []}

    def save(name, value):
        (output / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def stage(name, **facts):
        report["stages"].append({"stage": name, **facts})
        save("report", report)
        print(json.dumps({"stage": name, **facts}), flush=True)

    def command(name):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/local_workbench.py"), name],
                                cwd=ROOT, capture_output=True, timeout=180)
        need(result.returncode == 0, "launcher_" + name + "_failed")

    def tables(name):
        path = output / (name + "-tables.json")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/product_acceptance_snapshot.py"),
            "--container", launcher["CONTAINER"], "--label", name, "--out", str(path)],
            cwd=ROOT, capture_output=True, timeout=120)
        need(result.returncode == 0, "business_snapshot_failed")
        return read_json(path)["tables"]

    save("report", report)
    try:
        baseline = tables("baseline")
        with httpx.Client(base_url=f"http://127.0.0.1:{launcher['PORT']}", trust_env=False, timeout=660) as http:
            need(http.post("/api/auth/session", json={"invite_code": launcher["INVITE"]}).status_code == 201,
                 "login_failed")

            def request(method, path, body=None, status=200):
                response = http.request(method, path, json=body)
                need(response.status_code == status, "unexpected_http_status_" + str(response.status_code))
                return response.json()

            need(request("GET", "/api/product/cases") == [], "sandbox_cases_not_fresh")
            case = request("POST", "/api/product/cases", {"scenario": launcher["SCENARIO"]}, status=201)
            save("prepared", public_evidence(case))
            url = "/api/product/cases/" + case["case_id"]
            comparison = case.get("comparison") or {}
            candidates = comparison.get("plans") or []
            selected = next((p for p in candidates if p.get("objective") == "preserve_quantities"), case.get("plan"))
            need(selected is not None and selected.get("objective") == "preserve_quantities", "default_candidate_missing")
            plan_id = selected["plan_id"]
            if case["state"] == "awaiting_selection" or (case.get("plan") or {}).get("plan_id") != plan_id:
                case = request("POST", url + "/select", {"plan_id": plan_id,
                    "comparison_id": comparison["comparison_id"], "selection_revision": comparison["selection_revision"]})
            need(case["state"] == "awaiting_approval", "default_plan_not_approvable")
            save("selected", public_evidence(case))
            metrics = independent_margin(sandbox / "store" / plan_id, case["plan"])
            report.update(case_id=case["case_id"], plan_id=plan_id, margin=metrics,
                          plan_manifest=read_json(sandbox / "store" / plan_id / "manifest.json"))
            if launcher["SCENARIO"] == "A":
                need(Decimal(metrics["planned_cost"]) == Decimal("4794.94")
                     and metrics["repair_objective"] == 6 and metrics["new_spend_units"] == 43
                     and Decimal(metrics["new_spend_revenue"]) == Decimal("20623.23"), "a_default_baseline_changed")
            need(tables("before-approval") == baseline, "business_write_before_approval")
            answers = {}
            for key, question in (("margin_en", "What is the plan margin?"), ("margin_zh", "方案利润率是多少？"),
                                  ("impact_en", "What is the impact?"), ("impact_zh", "影响范围是什么？")):
                answers[key] = request("POST", url + "/ask", {"question": question})
            save("answers", answers)
            command("stop")
            command("resume")
            need(request("GET", url) == case and tables("resumed-before-approval") == baseline,
                 "restart_changed_prepared_case_or_business_tables")
            stage("prepared_selected_and_resumed", candidates=len(candidates), margin=metrics,
                  eight_business_tables_unchanged=True)
            case = request("POST", url + "/approve", {"plan_id": plan_id,
                "comment": "Authorized isolated next-phase acceptance of the default repair objective"})
            save("executed", public_evidence(case))
            need(case["state"] == "succeeded" and case["result"]["verified"] is True,
                 "original_verification_not_confirmed")
            execution = case["execution"]
            need(case["approval"]["plan_id"] == plan_id and execution["readback"]["execution_validated"] is True,
                 "executed_plan_binding_mismatch")
            reward = launcher["_docker"]("exec", launcher["CONTAINER"], "cat", "/logs/verifier/reward.json", capture=True)
            save("original-reward", json.loads(reward.stdout))
            journal = sandbox / "store" / plan_id / "journal.jsonl"
            report["journal"] = {"sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
                                 "records": len(journal.read_text().splitlines())} if journal.is_file() else None
            after = tables("after-approval")
            need(request("POST", url + "/approve", {"plan_id": plan_id}) == case, "approval_replay_changed_case")
            need(tables("after-replay") == after, "approval_replay_changed_business_tables")
            command("stop")
            command("resume")
            need(request("GET", url) == case and tables("resumed-after-approval") == after,
                 "restart_changed_execution_receipt_or_business_tables")
            stage("executed_and_replay_safe", verifier=execution["verifier"],
                  business_actions=len(case["plan"]["actions"]), confirmed_steps=execution["readback"]["verified_steps"])
            report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__, error_code=str(error)[:120])
        raise
    finally:
        report["source_unchanged"] = sources() == report["source_sha256"]
        report["finished_at"] = datetime.now(UTC).isoformat()
        save("report", report)
    need(report["source_unchanged"], "source_changed_during_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
