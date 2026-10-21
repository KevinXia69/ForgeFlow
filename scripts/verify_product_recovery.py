#!/usr/bin/env python3
"""Opt-in real ERP product recovery acceptance with controlled response loss.

Requires an already-started, fresh dedicated local_workbench sandbox and explicit
FORGEFLOW_LOCAL_* destinations. Never starts/resets Docker. The operator's
explicit --authorize-product-approval authorizes one service-level product test
approval by a synthetic acceptance identity; this is not a browser approval,
model decision, autonomous benchmark or a grant to run against production ERP.

The actual Unix client completes apply before its return is discarded. The first
root reconciliation completes before its return is discarded. A second read-only
reconciliation must recover the identical original-verifier receipt. No private
credential, approval token, raw reward, customer row or prompt is reported.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from forgeflow.api.evidence import public_evidence
from forgeflow.product import ProductCaseService
from forgeflow.product import local as local_module
from forgeflow.product.service import ProductCaseError
from forgeflow.recovery import client

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "scripts/verify_product_recovery.py", "scripts/local_workbench.py",
    "src/forgeflow/product/local.py", "src/forgeflow/product/service.py",
    "src/forgeflow/product/reconciliation.py", "src/forgeflow/product/verifier.py",
    "src/forgeflow/executor/handlers.py", "src/forgeflow/executor/daemon.py",
    "src/forgeflow/executor/protocol.py", "src/forgeflow/recovery/client.py",
    "src/forgeflow/recovery/product_versions.py", "src/forgeflow/recovery/service.py",
    "src/forgeflow/product/state.py", "src/forgeflow/approval.py",
)


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def sources():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def receipt_summary(result):
    verifier = result.get("verifier", {})
    return {
        **{key: result.get(key) for key in (
            "execution_validated", "verified_steps", "receipt_sha256", "verified_at",
            "run_id", "case_id", "plan_id", "policy_sha256", "snapshot_sha256", "readback_sha256",
        )},
        "verifier": {key: verifier.get(key) for key in (
            "status", "score", "strict_success", "evaluation_valid", "evaluation_scope",
            "passed_rule_count", "applicable_rule_count", "failed_rule_count", "verification_attempt",
        )},
    }


class LostResponses:
    """Faults occur only after successful real transport returns, never before."""

    def __init__(self):
        self.apply_calls = 0
        self.reconcile_calls = 0
        self.authorization_requests = 0
        self.apply_return = None
        self.reconcile_returns = []
        self.apply = client.apply
        self.reconcile = client.reconcile
        self.issue = client.product

    def issue_once(self, *args, **kwargs):
        self.authorization_requests += 1
        need(self.authorization_requests == 1, "unexpected_second_authorization_request")
        return self.issue(*args, **kwargs)

    def apply_then_lose(self, *args, **kwargs):
        self.apply_calls += 1
        need(self.apply_calls == 1, "unexpected_second_apply")
        actual = self.apply(*args, **kwargs)
        need(actual.get("execution_validated") is True, "actual_apply_readback_not_confirmed")
        self.apply_return = {key: actual.get(key) for key in ("state", "execution_validated", "verified_steps")}
        raise client.ExecutorError("unavailable", retryable=True, reason="controlled response loss")

    def reconcile_observed(self, *args, **kwargs):
        self.reconcile_calls += 1
        need(self.reconcile_calls <= 2, "unexpected_additional_reconciliation")
        actual = self.reconcile(*args, **kwargs)
        need(actual.get("execution_validated") is True and isinstance(actual.get("receipt_sha256"), str),
             "original_receipt_not_confirmed")
        self.reconcile_returns.append(receipt_summary(actual))
        if self.reconcile_calls == 1:
            raise client.ExecutorError("unavailable", retryable=True, reason="controlled response loss")
        return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new evidence directory")
    parser.add_argument("--authorize-product-approval", action="store_true")
    args = parser.parse_args()
    if not args.authorize_product_approval:
        parser.error("explicit product test approval authorization is required")
    required = ("FORGEFLOW_LOCAL_SANDBOX_ROOT", "FORGEFLOW_LOCAL_CASE_ROOT",
                "FORGEFLOW_LOCAL_CONTAINER_NAME", "FORGEFLOW_LOCAL_SOCKET_PATH")
    if any(not os.environ.get(key) for key in required):
        parser.error("explicit dedicated sandbox/case/container/socket configuration is required")
    output = args.output.resolve()
    if output.exists():
        parser.error("evidence directory already exists; preserve it and choose a new path")
    output.mkdir(parents=True)
    report = {"category": "real_erp_product_service_with_controlled_response_loss",
              "approval_origin": "explicitly_authorized_acceptance_driver_synthetic_identity",
              "browser_approval": False, "autonomous_benchmark": False,
              "started_at": datetime.now(UTC).isoformat(), "stages": [], "source_sha256": sources()}

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    def stage(name, **facts):
        report["stages"].append({"stage": name, **facts})
        save()
        print(json.dumps({"stage": name, **facts}), flush=True)

    def save_case(name, case):
        (output / (name + "-case.json")).write_text(json.dumps(public_evidence(case), indent=2) + "\n")

    def snapshot(name, container):
        path = output / (name + "-tables.json")
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts/product_acceptance_snapshot.py"),
            "--container", container, "--label", name, "--out", str(path),
        ], cwd=ROOT, capture_output=True, text=True, timeout=120)
        need(result.returncode == 0, "business_snapshot_unavailable")
        return json.loads(path.read_text())["tables"]

    save()
    try:
        launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
        sandbox, container = launcher["SANDBOX"], launcher["CONTAINER"]
        cases_root = Path(os.environ["FORGEFLOW_LOCAL_CASE_ROOT"])
        need(launcher["SCENARIO"] == "A", "acceptance_requires_dedicated_a_sandbox")
        need(sandbox.is_dir() and (sandbox / "request.json").is_file(), "sandbox_not_initialized")
        need(not any(p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name)
                     for p in (sandbox / "store").iterdir()), "existing_execution_state")
        need(not any((cases_root / "product-cases").glob("*.json")), "existing_product_cases")
        need(not any((sandbox / "approvals").glob("approval-*.json")), "existing_approval")
        client.health(launcher["SOCKET"])
        executor = local_module.LocalProductExecutor(
            socket_path=launcher["SOCKET"], store=sandbox / "store", approvals=sandbox / "approvals",
            request=sandbox / "request.json", container=container, scenario=launcher["SCENARIO"], task=launcher["TASK"],
        )
        service = ProductCaseService(cases_root, executor)
        # This records the driver's real test identity; it never impersonates
        # an authenticated UI user or claims that a browser supplied approval.
        uid = "authorized-product-recovery-acceptance"
        before = snapshot("baseline", container)
        case = service.create(uid, "A")
        case = service.prepare(case["case_id"], uid)
        save_case("prepared", case)
        need(case["state"] == "awaiting_approval" and case["decision"]["can_approve"], "plan_not_approvable")
        prepared = snapshot("after-prepared", container)
        need(prepared == before, "business_write_before_approval")
        plan = copy.deepcopy(case["plan"])
        case_id, plan_id = case["case_id"], plan["plan_id"]
        report.update(case_id=case_id, plan_id=plan_id, task=launcher["TASK"], container=container)
        stage("prepared_without_business_write", tables_unchanged=True)
        lost = LostResponses()
        with patch.object(client, "product", lost.issue_once), \
                patch.object(client, "apply", lost.apply_then_lose), \
                patch.object(client, "reconcile", lost.reconcile_observed):
            case = service.approve(case_id, uid, plan_id, comment="Authorized product recovery acceptance: discard successful transport returns.")
            save_case("after-apply-response-loss", case)
            need(case["state"] == "execution_unknown" and not case["result"]["verified"], "lost_apply_not_unknown")
            need(lost.apply_calls == 1 and lost.apply_return is not None and lost.reconcile_calls == 0,
                 "unexpected_apply_dispatch")
            after_apply = snapshot("after-apply-response-loss", container)
            stage("actual_apply_return_discarded", apply_calls=lost.apply_calls,
                  authorization_requests=lost.authorization_requests, actual_apply=lost.apply_return,
                  product_state=case["state"], verified=case["result"]["verified"])
            replay = service.approve(case_id, uid, plan_id)
            need(replay == case and lost.apply_calls == 1, "approval_replay_redispatched")
            stage("same_approval_replay", unchanged=True, apply_calls=lost.apply_calls)
            try:
                service.reconcile(case_id, uid)
            except ProductCaseError as error:
                need(error.code == "reconciliation_unavailable", "unexpected_recovery_error")
            else:
                raise RuntimeError("first_reconcile_response_was_not_lost")
            pending = service.get(case_id, uid)
            save_case("after-reconcile-response-loss", pending)
            need(pending == case and lost.reconcile_calls == 1 and len(lost.reconcile_returns) == 1,
                 "lost_reconciliation_changed_product_state")
            first_tables = snapshot("after-reconcile-response-loss", container)
            need(first_tables == after_apply, "reconciliation_changed_business_tables")
            stage("actual_reconcile_return_discarded", reconcile_calls=lost.reconcile_calls,
                  tables_unchanged=True, original_receipt=lost.reconcile_returns[0])
            final = service.reconcile(case_id, uid)
            save_case("recovered", final)
            need(lost.reconcile_calls == 2 and lost.reconcile_returns[0] == lost.reconcile_returns[1],
                 "reconciliation_did_not_reuse_identical_receipt")
            need(final["plan"] == plan and final["approval"] == case["approval"], "original_decision_changed")
            need(final["result"]["verified"] is True and final["state"] == "succeeded", "original_strict_verification_not_passed")
            final_tables = snapshot("after-cached-reconciliation", container)
            need(final_tables == after_apply, "cached_reconciliation_changed_business_tables")
            need(lost.apply_calls == 1 and lost.authorization_requests == 1, "duplicate_execution_or_authorization_request")
            issued_tokens = len(list((sandbox / "approvals").glob("approval-*.json")))
            consumed_tokens = len(list((sandbox / "approvals").glob("approval-*.used")))
            need(issued_tokens == consumed_tokens == 1, "unexpected_approval_token_count")
            need(sources() == report["source_sha256"], "acceptance_source_changed")
            stage("cached_original_receipt_recovered", apply_calls=lost.apply_calls,
                  authorization_requests=lost.authorization_requests, reconcile_calls=lost.reconcile_calls,
                  same_receipt=True, tables_unchanged=True, verified=True,
                  issued_token_count=issued_tokens, consumed_token_count=consumed_tokens,
                  original_receipt=lost.reconcile_returns[1])
        report["result"] = "passed"
    except Exception as error:
        code = getattr(error, "code", str(error))
        report.update(result="failed", error=code if re.fullmatch(r"[a-z0-9_]{1,100}", code) else type(error).__name__)
        print(json.dumps({"result": "failed", "error": report["error"]}), flush=True)
    report["finished_at"] = datetime.now(UTC).isoformat()
    save()
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
