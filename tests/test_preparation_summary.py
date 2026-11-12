"""The host verifies prepared artifacts separately from original ERP acceptance."""
import hashlib
import json
import subprocess

import pytest
from test_runner import write_trial

from forgeflow.agents.pi_nebius import _build_log_export_command
from forgeflow.recovery.storage import digest, encoded
from forgeflow.runner import summarize_run


def exported_trial(tmp_path, mode="investigation", status="prepared", *, partial=False, event=None):
    trial = write_trial(tmp_path, score=0, rule_status="FAIL", agent="pi")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest.update(recovery_mode=mode, contract={"sha256": "c" * 64})
    if partial:
        manifest["contract"]["kind"] = "forgeflow.investigation_policy"
    if event:
        manifest["investigation_event"] = {"source_type": "user-event-file", "sha256": event["sha256"]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    source = tmp_path / "trusted"
    recovery = source / "recovery"
    investigation = recovery / "investigation"
    investigation.mkdir(parents=True)
    artifact_names = ("request", "snapshot", "blueprint", "plan", "change-set", "baseline", "binding")
    artifacts = {name: {"synthetic": name} for name in artifact_names}
    bound = {"schema_version": 1, "artifacts": {name: digest(value) for name, value in artifacts.items()}}
    plan_id = digest(bound)
    plan = recovery / plan_id
    plan.mkdir()
    for name, value in {**artifacts, "manifest": bound, "prepared": {
        "ok": True, "odoo_writes": False, "plan_id": plan_id, "policy_sha256": "c" * 64,
    }}.items():
        (plan / f"{name}.json").write_bytes(encoded(value))
    review = {"schema_version": 1, "run_id": tmp_path.name, "contract_sha256": "c" * 64,
              "plan_id": plan_id, "request": artifacts["request"], "request_sha256": digest(artifacts["request"]),
              "bindings_verified": True, "odoo_writes": False, "execution_validated": False,
              "benchmark_success": None}
    if event:
        review.update(event=event, event_sha256=event["sha256"], policy_sha256="c" * 64)
    state = {"schema_version": 1, "run_id": tmp_path.name, "contract_sha256": "c" * 64,
             "status": status, "terminal": {"reason": "synthetic_missing_business_authority"},
             "prepared": {"plan_id": plan_id, "request_sha256": review["request_sha256"],
                          "review_sha256": digest(review), "bindings_verified": True}}
    if partial:
        state.update(policy_sha256="c" * 64, contract_sha256=None)
    if event:
        state.update(event=event, event_sha256=event["sha256"], policy_sha256="c" * 64)
    if mode == "investigation":
        (investigation / "state.json").write_bytes(encoded(state))
        (investigation / "review.json").write_bytes(encoded(review))
        (investigation / "secret-unlisted.json").write_text("must not export")
    destination = trial / "agent"
    destination.mkdir()
    subprocess.run(["bash", "-c", _build_log_export_command(str(source), str(destination))],
                   check=True, capture_output=True, timeout=10)
    return trial, plan_id


def rewrite_exported_artifact(trial, relative, mutate):
    path = trial / "agent/recovery" / relative
    value = json.loads(path.read_text())
    mutate(value)
    path.write_bytes(encoded(value))
    report_path = trial / "agent/recovery-export.json"
    report = json.loads(report_path.read_text())
    for record in report["copied_files"]:
        if record["path"] == relative:
            record.update(bytes=len(path.read_bytes()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    report_path.write_text(json.dumps(report))


@pytest.mark.parametrize("mode", ["prepare-only", "investigation"])
def test_prepared_plan_is_verified_without_claiming_erp_success(tmp_path, mode):
    trial, plan_id = exported_trial(tmp_path, mode)
    summary = summarize_run(tmp_path)
    assert summary["preparation_success"] is True
    assert summary["preparation_bindings_verified"] is True
    assert summary["prepared_plan_ids"] == [plan_id]
    assert summary["score"] == 0
    assert summary["strict_success"] is False
    if mode == "investigation":
        assert (tmp_path / summary["review_path"]).is_file()
    assert not (trial / "agent/recovery/investigation/secret-unlisted.json").exists()


@pytest.mark.parametrize("status", ["needs_clarification", "unsupported", "infeasible"])
def test_supported_terminal_result_is_completed_but_not_prepared(tmp_path, status):
    exported_trial(tmp_path, status=status)
    summary = summarize_run(tmp_path)
    assert summary["investigation_completed"] is True
    assert summary["preparation_success"] is False
    assert summary["preparation_status"] == status


@pytest.mark.parametrize("status", ["needs_clarification", "prepared"])
def test_partial_policy_can_complete_clarification_but_never_prepare(tmp_path, status):
    exported_trial(tmp_path, status=status, partial=True)
    summary = summarize_run(tmp_path)
    assert summary["preparation_success"] is False
    assert summary["investigation_completed"] == (status == "needs_clarification")
    if status == "prepared":
        assert "complete_contract_required_for_preparation" in summary["preparation_issues"]


@pytest.mark.parametrize("changed", [None, "state", "review", "event_text"])
def test_preparation_result_binds_to_the_trusted_event(tmp_path, changed):
    from test_recovery_modes import event_payload

    trial, _ = exported_trial(tmp_path, event=event_payload())
    if changed == "event_text":
        rewrite_exported_artifact(trial, "investigation/state.json",
                                  lambda value: value["event"].update(text="different event"))
    elif changed:
        rewrite_exported_artifact(trial, f"investigation/{changed}.json",
                                  lambda value: value.update(event_sha256="f" * 64))
    summary = summarize_run(tmp_path)
    assert summary["preparation_success"] is (changed is None)
    assert summary["investigation_completed"] is (changed is None)


@pytest.mark.parametrize("tamper", ["request", "review", "state", "report", "symlink"])
def test_tampered_export_never_reports_preparation_success(tmp_path, tamper):
    trial, plan_id = exported_trial(tmp_path)
    logs = trial / "agent"
    if tamper == "report":
        path = logs / "recovery-export.json"
        report = json.loads(path.read_text())
        report["complete"] = False
        path.write_text(json.dumps(report))
    else:
        relative = (f"{plan_id}/request.json" if tamper in {"request", "symlink"}
                    else f"investigation/{tamper}.json")
        path = logs / "recovery" / relative
        if tamper == "symlink":
            target = tmp_path / "outside.json"
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
        else:
            value = json.loads(path.read_text())
            value["run_id" if tamper == "state" else "synthetic"] = "changed"
            path.write_bytes(encoded(value))
            # Recompute the export byte hash: semantic bindings must still fail.
            report_path = logs / "recovery-export.json"
            report = json.loads(report_path.read_text())
            for record in report["copied_files"]:
                if record["path"] == relative:
                    record.update(bytes=len(path.read_bytes()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            report_path.write_text(json.dumps(report))
    summary = summarize_run(tmp_path)
    assert summary["preparation_success"] is False
    assert summary["investigation_completed"] is False
    assert summary["preparation_issues"]


@pytest.mark.parametrize(("change", "status"), [
    ({"reported_total_tokens":3000,"stop_reason":"token_budget_exhausted"},"token_budget_exhausted"),
    ({"partial_usage_unknown":True,"stop_reason":"token_usage_unknown"},"token_usage_unknown"),
    ({"in_flight":True,"provider_requests":2},"token_usage_unknown"),
    (None,"token_usage_unknown"),
    ({},"within_reported_budget"),
])
def test_token_stop_is_distinct_from_bound_plan_and_original_erp_reward(tmp_path, change, status):
    trial, plan_id = exported_trial(tmp_path, mode="prepare-only")
    path=tmp_path/"manifest.json"
    manifest=json.loads(path.read_text())
    manifest["budget"]={"total_tokens":{"mechanism":"reported-usage-gate-v1",
        "max_total_tokens":3000,"min_next_call_tokens":1024}}
    path.write_text(json.dumps(manifest))
    if change is not None:
        record={"schema_version":1,"mechanism":"reported-usage-gate-v1","max_total_tokens":3000,
                "min_next_call_tokens":1024,"reported_total_tokens":1000,"completed_responses":1,
                "provider_requests":1,"in_flight":False,"partial_usage_unknown":False,"stop_reason":None,
                **change}
        (trial/"agent/token-budget.json").write_text(json.dumps(record))
    summary=summarize_run(tmp_path)
    assert summary["token_budget"]["status"] == status
    assert summary["preparation_success"] == (status == "within_reported_budget")
    assert summary["prepared_plan_ids"] == [plan_id]
    assert summary["score"] == 0 and summary["strict_success"] is False
    if status != "within_reported_budget":
        assert summary["preparation_status"] == status
        assert summary["plan_preparation_status"] == "prepared"
