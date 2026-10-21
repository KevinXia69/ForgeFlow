"""Explicit fresh-sandbox HTTP acceptance of real product revision and recovery.

Uses the existing managed launcher and digest-only ERP snapshot command. No
model is called. Approval is supplied by this authorized test driver, not a
human browser or autonomous benchmark. Always attempts a preserving stop.
The operator starts/stops the VM separately. Never retries a failed mutation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def sources():
    # Host services use workspace imports; this is drift detection, not a claim
    # that they executed from the research runner's isolated frozen runtime.
    paths = [ROOT / "scripts/local_workbench.py", Path(__file__).resolve(),
             ROOT / "scripts/product_acceptance_snapshot.py"]
    paths += sorted((ROOT / "src/forgeflow").rglob("*.py"))
    paths += sorted((ROOT / "contracts").glob("*.json"))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--minimum-margin-pct", required=True, type=float)
    parser.add_argument("--authorize-product-approval", action="store_true")
    args = parser.parse_args()
    need(args.authorize_product_approval, "explicit_product_approval_required")
    need(0 < args.minimum_margin_pct < 99.99, "invalid_acceptance_margin")
    required = ("FORGEFLOW_LOCAL_SANDBOX_ROOT", "FORGEFLOW_LOCAL_CASE_ROOT",
                "FORGEFLOW_LOCAL_CONTAINER_NAME", "FORGEFLOW_LOCAL_SOCKET_PATH",
                "FORGEFLOW_LOCAL_API_PORT", "FORGEFLOW_LOCAL_ODOO_PORT")
    need(all(os.environ.get(key) for key in required), "explicit_destinations_required")
    need(os.environ.get("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE") == "1", "explicit_acceptance_required")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    need(not launcher["_candidate_capability"](None), "candidate_mode_requires_candidate_acceptance")
    need(not launcher["SANDBOX"].exists(), "fresh_sandbox_required")
    need(not launcher["_case_root"]().exists(), "fresh_case_root_required")
    output = args.out.resolve()
    need(not output.exists(), "new_evidence_directory_required")
    output.mkdir(parents=True)
    report = {
        "scope": "real_erp_product_http_revision_acceptance", "autonomous_benchmark": False,
        "model_called": False, "browser_approval": False,
        "approval_origin": "authorized_http_acceptance_driver",
        "source_scope": "workspace_hashes_before_after_not_frozen_host_imports",
        "started_at": datetime.now(UTC).isoformat(), "source_sha256": sources(),
        "scenario": launcher["SCENARIO"], "task": launcher["TASK"],
        "minimum_margin_pct": args.minimum_margin_pct, "stages": [], "status": "running",
    }

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    def stage(name, **facts):
        report["stages"].append({"stage": name, "at": datetime.now(UTC).isoformat(), **facts})
        save()
        print(json.dumps({"stage": name, **facts}), flush=True)

    def command(action, *, omit_acceptance=False):
        env = dict(os.environ)
        if omit_acceptance:
            env.pop("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", None)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/local_workbench.py"), action],
                                capture_output=True, text=True, timeout=360, cwd=ROOT, env=env)
        refusal = re.search(r"Workbench operation refused: ([a-z_]+)", result.stderr)
        stage("launcher_" + action, returncode=result.returncode,
              refusal=refusal[1] if refusal else None, acceptance_env_omitted=omit_acceptance,
              stderr_sha256=hashlib.sha256(result.stderr.encode()).hexdigest())
        need(result.returncode == 0, "launcher_" + action.replace("-", "_") + "_failed")
        return result.stdout

    def doctor(label):
        observed = json.loads(command("doctor", omit_acceptance=True))
        (output / (label + "-doctor.json")).write_text(json.dumps(observed, indent=2) + "\n")
        return observed

    def tables(label):
        path = output / (label + "-tables.json")
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts/product_acceptance_snapshot.py"),
            "--container", launcher["CONTAINER"], "--label", label, "--out", str(path)],
            capture_output=True, text=True, timeout=120, cwd=ROOT)
        need(result.returncode == 0, "table_snapshot_failed")
        return json.loads(path.read_text())["tables"]

    def save_case(label, case):
        (output / (label + "-case.json")).write_text(json.dumps(case, indent=2) + "\n")

    save()
    try:
        doctor("cold")
        command("start")
        started = doctor("started")
        need(started["features"] == {"revisions": True, "candidates": False},
             "revision_capability_not_persisted")
        baseline = tables("baseline")
        original_bindings = launcher["_bound_hashes"]()
        report["original_bindings"] = original_bindings
        with httpx.Client(base_url=f"http://127.0.0.1:{launcher['PORT']}",
                          trust_env=False, timeout=660) as http:
            login = http.post("/api/auth/session", json={"invite_code": launcher["INVITE"]})
            need(login.status_code == 201, "login_failed")

            def request(method, url, body=None, expected=200):
                response = http.request(method, url, json=body)
                need(response.status_code == expected, "unexpected_http_status")
                return response.json()

            need(request("GET", "/api/product/cases") == [], "prior_owner_cases_present")
            # First prove an impossible tightening retires its former plan and
            # does not write ERP or revive that plan when retried as a new version.
            failed = request("POST", "/api/product/cases", {"scenario": launcher["SCENARIO"]}, expected=201)
            save_case("failure-case-initial", failed)
            need(failed["state"] == "awaiting_approval", "initial_failure_control_not_prepared")
            fail_url = "/api/product/cases/" + failed["case_id"]
            retired = failed["plan"]["plan_id"]
            failed = request("POST", fail_url + "/revision", {
                "plan_id": retired, "reason": "Acceptance: impossible 99.99% margin tightening",
                "changes": {"minimum_margin_pct": 99.99}})
            save_case("infeasible", failed)
            need(failed["state"] == "failed" and failed.get("plan") is None
                 and failed["decision"]["can_reprepare"], "infeasibility_not_confirmed")
            failed = request("POST", fail_url + "/reprepare", {
                "failed_version": failed["revision"]["version"],
                "reason": "Acceptance: keep impossible constraint on fresh facts"})
            save_case("infeasible-new-version", failed)
            need(failed["state"] == "failed" and failed["revision"]["version"] == 3
                 and failed["revision"]["changes"] == {"minimum_margin_pct": 99.99}
                 and failed.get("plan") is None, "failed_version_relaxed_or_revived")
            response = http.post(fail_url + "/approve", json={"plan_id": retired})
            need(response.status_code == 409, "retired_failed_plan_not_rejected")
            need(tables("after-infeasible") == baseline, "business_write_before_approval")
            stage("infeasible_versions_preserved", failed_versions=[2, 3], retired_plan_rejected=True,
                  eight_business_tables_unchanged=True)

            case = request("POST", "/api/product/cases", {"scenario": launcher["SCENARIO"]}, expected=201)
            save_case("prepared-v1", case)
            need(case["state"] == "awaiting_approval" and case["decision"]["can_request_changes"],
                 "revision_capability_unavailable")
            url = "/api/product/cases/" + case["case_id"]
            original = case["plan"]["plan_id"]
            need(args.minimum_margin_pct > case["revision_limits"]["effective_margin_pct"], "margin_not_tightened")
            case = request("POST", url + "/revision", {
                "plan_id": original, "reason": "Acceptance: tighten margin without changing original contract",
                "changes": {"minimum_margin_pct": args.minimum_margin_pct}})
            save_case("prepared-v2", case)
            need(case["state"] == "awaiting_approval" and case["revision"]["version"] == 2
                 and case["plan"]["plan_id"] != original, "revision_not_prepared")
            plan_id = case["plan"]["plan_id"]
            need(case["revision_limits"]["effective_margin_pct"] == args.minimum_margin_pct,
                 "revision_bound_mismatch")
            need(launcher["_bound_hashes"]() == original_bindings, "original_contract_or_request_changed")
            need(tables("before-approval") == baseline, "business_write_before_approval")
            response = http.post(url + "/approve", json={"plan_id": original})
            need(response.status_code == 409, "retired_plan_not_rejected")
            command("restart-api", omit_acceptance=True)
            need(request("GET", url) == case, "case_changed_on_api_restart")
            command("stop", omit_acceptance=True)
            command("resume", omit_acceptance=True)
            need(request("GET", url) == case, "case_changed_on_resume")
            need(tables("resumed-before-approval") == baseline, "business_write_during_resume")
            stage("revised_and_resumed", version=2, original_binding_unchanged=True,
                  old_plan_rejected=True, same_cookie=True, eight_business_tables_unchanged=True)

            case = request("POST", url + "/approve", {"plan_id": plan_id,
                           "comment": "Authorized dedicated-sandbox revision acceptance"})
            save_case("after-approval", case)
            stage("approval_returned", state=case["state"], reason=case.get("reason"), result=case["result"])
            after = tables("after-approval")
            need(case["state"] == "succeeded" and case["result"]["verified"] is True,
                 "original_verification_not_confirmed")
            need(case["plan"]["plan_id"] == case["approval"]["plan_id"] == plan_id
                 and case["execution"]["readback"]["execution_validated"] is True
                 and case["execution"]["readback"].get("receipt_sha256"), "executed_plan_mismatch")
            repeated = request("POST", url + "/approve", {"plan_id": plan_id})
            need(repeated == case, "approval_replay_changed_case")
            need(tables("after-approval-replay") == after, "approval_replay_changed_business_tables")
            command("stop", omit_acceptance=True)
            command("resume", omit_acceptance=True)
            need(request("GET", url) == case, "receipt_changed_on_resume")
            need(tables("resumed-after-approval") == after, "business_tables_changed_on_receipt_resume")
            save_case("resumed-verified", case)
            report.update(case_id=case["case_id"], plan_id=plan_id)
            stage("same_plan_verified_and_preserved", duplicate_approval_unchanged=True,
                  same_receipt_after_resume=True, result=case["result"])
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, RuntimeError) and re.fullmatch(r"[a-z_]+", str(error)):
            report["reason"] = str(error)
        stage("acceptance_stopped", reason=report.get("reason"), error_type=type(error).__name__)
    finally:
        try:
            command("stop", omit_acceptance=True)
            final = doctor("final-stopped")
            need(final["container"]["status"] in {"exited", "created"}, "container_not_preserved")
            need(all(p["state"] in {"not_recorded", "exited"} for p in final["processes"].values()),
                 "services_still_running")
            report["preserving_cleanup_confirmed"] = True
        except Exception as error:
            report.update(status="failed", preserving_cleanup_confirmed=False,
                          cleanup_error_type=type(error).__name__)
        report["source_unchanged"] = sources() == report["source_sha256"]
        if not report["source_unchanged"]:
            report["status"] = "failed"
        report["finished_at"] = datetime.now(UTC).isoformat()
        save()
    print(json.dumps({"status": report["status"], "reason": report.get("reason"),
                      "preserving_cleanup_confirmed": report.get("preserving_cleanup_confirmed")}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
