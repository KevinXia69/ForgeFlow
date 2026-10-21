"""Explicit new-sandbox lifecycle acceptance; no prepare, approval or ERP repair.

Creates one local pending case via the service and reads it through the real API.
Uses existing launchers and digest-only acceptance commands for protected state.
Always attempts a preserving stop. The operator controls VM start/stop separately.
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
from pathlib import Path

import httpx

from forgeflow.product.local import LocalProductExecutor
from forgeflow.product.service import ProductCaseService
from forgeflow.runtime_lifecycle import process_identity, same_process

ROOT = Path(__file__).resolve().parents[1]


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path, help="existing failed acceptance evidence; never reinitialize")
    args = parser.parse_args()
    required = ("FORGEFLOW_LOCAL_SANDBOX_ROOT", "FORGEFLOW_LOCAL_CASE_ROOT",
                "FORGEFLOW_LOCAL_CONTAINER_NAME", "FORGEFLOW_LOCAL_SOCKET_PATH",
                "FORGEFLOW_LOCAL_API_PORT", "FORGEFLOW_LOCAL_ODOO_PORT")
    need(all(os.environ.get(key) for key in required), "explicit_destinations_required")
    output = args.out.resolve()
    need(not output.exists(), "evidence_already_exists")
    output.mkdir(parents=True)
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    sandbox = launcher["SANDBOX"]
    prior_tables = None
    if args.resume_from:
        prior = json.loads((args.resume_from / "before-stop-tables.json").read_text())
        need(prior["container"] == launcher["CONTAINER"], "prior_container_mismatch")
        prior_tables = prior["tables"]
        need(sandbox.is_dir() and launcher["_case_root"]().is_dir(), "existing_sandbox_required")
    else:
        need(not sandbox.exists(), "fresh_sandbox_required")
        need(not launcher["_case_root"]().exists(), "fresh_case_directory_required")
    report = {"scope": "real_local_start_stop_resume_without_business_operations",
              "model_called": False, "prepare_called": False, "approval_issued": False,
              "stages": [], "status": "running"}
    report["recovery_of_prior_acceptance"] = args.resume_from.name if args.resume_from else None
    tracked = ["scripts/local_workbench.py", "src/forgeflow/runtime_lifecycle.py",
               "scripts/verify_local_lifecycle.py", "src/forgeflow/api/app.py",
               "src/forgeflow/product/service.py"]
    source_hashes = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in tracked}
    report["source_sha256"] = source_hashes

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    def stage(name, **fields):
        report["stages"].append({"stage": name, **fields})
        save()
        print(json.dumps({"stage": name, **fields}), flush=True)

    def command(action):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/local_workbench.py"), action],
                                capture_output=True, text=True, timeout=360, cwd=ROOT)
        refusal = re.search(r"Workbench operation refused: ([a-z_]+)", result.stderr)
        facts = {"returncode": result.returncode,
                 "refusal": refusal[1] if refusal else None,
                 "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest()}
        stage("command_" + action, **facts)
        need(result.returncode == 0, "launcher_" + action + "_failed")
        return result.stdout

    def doctor(label):
        result = json.loads(command("doctor"))
        (output / f"{label}-doctor.json").write_text(json.dumps(result, indent=2) + "\n")
        return result

    def tables(label):
        path = output / f"{label}-tables.json"
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts/product_acceptance_snapshot.py"),
            "--container", launcher["CONTAINER"], "--label", label, "--out", str(path)],
            capture_output=True, text=True, timeout=120, cwd=ROOT)
        need(result.returncode == 0, "table_snapshot_failed")
        return json.loads(path.read_text())["tables"]

    def identities(observed):
        return {role: observed["processes"][role]["identity"] for role in ("api", "executor")}

    def stopped(observed, former):
        need(observed["container"]["status"] == "exited", "container_not_preserved_stopped")
        need(all(item["state"] in ("not_recorded", "exited")
                 for item in observed["processes"].values()), "service_still_running")
        need(not any(same_process(item, process_identity(item["pid"])) for item in former.values()),
             "original_service_still_alive")

    save()
    previous = None
    try:
        doctor("cold")
        command("resume" if args.resume_from else "start")
        first = doctor("started")
        previous = identities(first)
        need(all(x["state"] == "running" for x in first["processes"].values()), "services_not_running")
        before = tables("before-stop")
        if prior_tables is not None:
            need(before == prior_tables, "prior_business_tables_changed")
        identity_digest = hashlib.sha256(launcher["_api_session_secret"]().encode()).hexdigest()
        with httpx.Client(base_url=f"http://127.0.0.1:{launcher['PORT']}", trust_env=False) as client:
            login = client.post("/api/auth/session", json={"invite_code": launcher["INVITE"]})
            need(login.status_code == 201, "login_failed")
            executor = LocalProductExecutor(socket_path=launcher["SOCKET"], store=sandbox / "store",
                                             approvals=sandbox / "approvals", request=sandbox / "request.json",
                                             container=launcher["CONTAINER"], scenario=launcher["SCENARIO"])
            service = ProductCaseService(launcher["_case_root"](), executor)
            if args.resume_from:
                listing = client.get("/api/product/cases")
                need(listing.status_code == 200 and len(listing.json()) == 1, "prior_case_or_owner_lost")
                case = listing.json()[0]
            else:
                case = service.create(login.json()["uid"], launcher["SCENARIO"])
            url = "/api/product/cases/" + case["case_id"]
            original = client.get(url)
            need(original.status_code == 200, "case_not_readable")
            command("start")
            duplicate = doctor("duplicate-start")
            need(identities(duplicate) == previous, "duplicate_service_created")
            command("stop")
            stopped(doctor("stopped"), previous)
            command("resume")
            restored = doctor("resumed")
            need(restored["container"]["id"] == first["container"]["id"], "container_replaced")
            need(hashlib.sha256(launcher["_api_session_secret"]().encode()).hexdigest() == identity_digest,
                 "api_identity_changed")
            restored_case = client.get(url)
            need(restored_case.status_code == 200, "session_or_case_lost")
            need(restored_case.json() == original.json(), "case_changed_during_resume")
            need(tables("after-resume") == before, "business_tables_changed")
            previous = identities(restored)
            stage("preservation_verified", same_container=True, same_session=True, same_case=True,
                  business_tables_unchanged=True, duplicate_start_idempotent=True)
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, RuntimeError) and re.fullmatch(r"[a-z_]+", str(error)):
            report["reason"] = str(error)
    finally:
        try:
            command("stop")
            final = doctor("final-stopped")
            if previous:
                stopped(final, previous)
            report["preserving_cleanup_confirmed"] = True
        except Exception as error:
            report.update(status="failed", preserving_cleanup_confirmed=False,
                           cleanup_error_type=type(error).__name__)
        report["source_unchanged"] = all(hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == value
                                          for n, value in source_hashes.items())
        if not report["source_unchanged"]:
            report["status"] = "failed"
        save()
    print(json.dumps({"status": report["status"], "reason": report.get("reason"),
                      "preserving_cleanup_confirmed": report.get("preserving_cleanup_confirmed")}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
