#!/usr/bin/env python3
"""One-off continuation of qa3's documented pre-approval selection-check failure.

Runs with the deployed stage's Python. Preserves the failed output and existing
case; never POSTs /api/product/cases, resets ERP, or touches other environments.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx

ALLOWED_SCRIPT_CHANGES = {"scripts/vps_upgrade_acceptance.py", "scripts/verify_next_phase.py"}
CASE_FIELDS = ("case_id", "scenario", "state", "synthetic", "order", "disruption", "plan",
               "approval", "execution", "created_at", "updated_at", "revision", "history", "audit")


def check_source_continuity(driver, old, current):
    driver.need(old["bundle_files_sha256"] == current["bundle_files_sha256"]
                and old["bundle_provenance_sha256"] == current["bundle_provenance_sha256"],
                "continuation_bundle_changed")
    before, after = old["files_sha256"], current["files_sha256"]
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    driver.need(changed <= ALLOWED_SCRIPT_CHANGES, "continuation_business_or_asset_source_changed")
    return sorted(changed)


def check_unexecuted(driver, config, case, prepared, case_id):
    driver.need(case_id == prepared["case_id"] == case["case_id"]
                and case.get("scenario") == "A" and case.get("state") == "awaiting_approval"
                and case.get("approval") is None and case.get("execution") is None
                and not case.get("comparison"), "continuation_case_not_single_unapproved_plan")
    driver.need(all(driver.publish(case.get(key)) == prepared.get(key) for key in CASE_FIELDS),
                "continuation_case_changed_since_failure")
    files = [path for path in Path(config["case_root"]).rglob("*.json")
             if re.fullmatch(r"[0-9a-f]{32}\.json", path.name)]
    driver.need(len(files) == 1 and files[0].stem == case_id, "continuation_has_other_persisted_cases")
    store = Path(config["sandbox_root"]) / "store"
    plan_id = case["plan"]["plan_id"]
    plans = [path.name for path in store.iterdir() if re.fullmatch(r"[0-9a-f]{64}", path.name)]
    driver.need(plans == [plan_id] and not (store / "active-plan.json").exists()
                and not any(store.rglob("journal.jsonl"))
                and not any(store.rglob("product-verification*.json"))
                and not any((Path(config["sandbox_root"]) / "approvals").iterdir()),
                "continuation_has_prior_authorization_or_execution")


def private_json(driver, path):
    info = path.lstat()
    driver.need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "untrusted_previous_evidence")
    return driver.read_json(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--previous-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("upgrade_acceptance", args.repo / "scripts/vps_upgrade_acceptance.py")
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    report = None
    try:
        driver.need(os.geteuid() == 0 and args.execute, "explicit_root_continuation_required")
        driver.need(re.fullmatch(r"[0-9a-f]{32}", args.case_id), "invalid_case_id")
        os.umask(0o077)
        previous = args.previous_output.resolve(strict=True)
        old_report = private_json(driver, previous / "report.json")
        prepared = private_json(driver, previous / "prepared.json")
        baseline = private_json(driver, previous / "before-prepare-tables.json")
        old_source = private_json(driver, previous / "source.json")
        driver.need(old_report.get("status") == "failed"
                    and old_report.get("error_code") == "default_candidate_missing"
                    and old_report.get("stage") == "execute" and old_report.get("name") == "qa3"
                    and old_report.get("base_url") == args.base_url
                    and old_report.get("source_commit") == old_source.get("commit")
                    and not any((previous / name).exists() for name in (
                        "selected.json", "executed.json", "after-approval-tables.json", "receipt-binding.json")),
                    "previous_failure_not_the_known_preapproval_gate")
        item, config, instance = driver.load_target("qa3", args.base_url)
        driver.need(old_report["generation"] == instance["generation"], "continuation_generation_changed")
        current_source = driver.sources(config)
        changed = check_source_continuity(driver, old_source, current_source)
        driver.need(not args.output.exists() and not args.output.is_symlink(), "new_output_directory_required")
        args.output.mkdir(parents=True, mode=0o700)
        report = {"schema_version": 1, "name": "qa3", "stage": "resume-prepared", "status": "running",
                  "category": "public_https_dedicated_synthetic_product_acceptance",
                  "operator_driver_sha256": driver.sha256(Path(__file__).read_bytes()),
                  "source_commit": current_source["commit"], "base_url": args.base_url,
                  "generation": instance["generation"], "odoo_image": config["odoo_image"],
                  "data_profile": "planning-metadata-v1", "started_at": datetime.now(UTC).isoformat(),
                  "tls_verification": True, "official_or_historical_environments_used": False,
                  "approval_origin": "explicitly_authorized_http_driver", "browser_approval": False,
                  "autonomous_benchmark": False, "business_table_scope": list(driver.TABLES),
                  "all_database_writes_measured": False,
                  "continuation": {"previous_report_sha256": driver.sha256((previous / "report.json").read_bytes()),
                      "previous_prepared_sha256": driver.sha256((previous / "prepared.json").read_bytes()),
                      "previous_source_commit": old_source["commit"], "changed_acceptance_scripts": changed,
                      "original_after_prepare_capture_existed": False,
                      "measurement": "Original pre-prepare digest compared with two newly captured pre-approval digests; no new case or reset."}}
        driver.save(args.output, "source", current_source)
        driver.save(args.output, "before-prepare-tables", baseline)
        with httpx.Client(base_url=args.base_url.rstrip("/"), headers={"Origin": args.base_url.rstrip("/")},
                          timeout=960, trust_env=False, follow_redirects=False) as client:
            environment = driver.authenticate(client, item, instance)
            driver.need(environment.get("owned_by_me") is True, "continuation_owner_changed")
            cases = driver.request(client, "GET", "/api/product/cases")
            driver.need(len(cases) == 1 and cases[0]["case_id"] == args.case_id, "continuation_has_other_cases")
            case = driver.request(client, "GET", "/api/product/cases/" + args.case_id)
            check_unexecuted(driver, config, case, prepared, args.case_id)
            first = driver.tables(config)
            driver.save(args.output, "continuation-entry-tables", first)
            driver.need(first == baseline, "continuation_business_rows_changed")
            case = driver.select_default(client, case, config)
            driver.save(args.output, "selected", case)
            report.update(case_id=case["case_id"], plan_id=case["plan"]["plan_id"],
                          margin=driver.independent_margin(config, case))
            second = driver.tables(config)
            driver.save(args.output, "after-prepare-tables", second)
            driver.need(second == first == baseline, "continuation_preapproval_rows_changed")
            # Reload the same case and local stores immediately before approval.
            latest = driver.request(client, "GET", "/api/product/cases/" + args.case_id)
            check_unexecuted(driver, config, latest, prepared, args.case_id)
            driver.need(driver.sources(config) == current_source, "continuation_source_changed")
            driver.execute_prepared(client, config, instance, args.output, report, latest, baseline)
        driver.need(driver.sources(config) == current_source, "continuation_source_changed")
        report.update(status="passed", source_unchanged=True)
    except Exception as error:
        label = str(error) if type(error) is driver.AcceptanceFailure else type(error).__name__
        if report is not None:
            report.update(status="failed", error_code=label)
        print(json.dumps({"passed": False, "error_code": label}), flush=True)
        return 1
    finally:
        if report is not None:
            report["finished_at"] = datetime.now(UTC).isoformat()
            driver.save(args.output, "report", report)
    driver.save(args.output, "manifest", {"files": {
        path.name: driver.sha256(path.read_bytes()) for path in sorted(args.output.iterdir())},
        "scope": "Continuation of the same unapproved case after an acceptance-script fix; original failed evidence retained."})
    print(json.dumps({"passed": True, "name": "qa3", "stage": "resume-prepared"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
