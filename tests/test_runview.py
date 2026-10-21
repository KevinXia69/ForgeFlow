from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from forgeflow.runview import build_run_view, legacy_record, rule_projection
from forgeflow.ui import make_server

REPO = Path(__file__).resolve().parents[1]
RECORDED_RUNS = REPO / "runs"
PLAN_ID = "a" * 64
OTHER_PLAN_ID = "b" * 64


def write_run(root, name="trial", score=100, *, rules=None, agent="pi", state="completed"):
    run_dir = root / "runs" / name
    run_dir.mkdir(parents=True)
    manifest = {
        "agent": agent,
        "task": "2280_medium_repair_plan_medium",
        "run_id": name,
        "state": state,
        "harbor_returncode": 0,
        "started_at_utc": "2026-10-20T13:00:00+00:00",
        "finished_at_utc": "2026-10-20T13:01:30+00:00",
        "model": {"model": "nvidia/test"},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    if score is not None:
        trial = run_dir / "harbor/job/one"
        (trial / "verifier").mkdir(parents=True)
        default = [{"status": "PASS", "applicable": True, "expr": "check_order_links"}]
        reward = {
            "overall_score": score,
            "passed": score >= 99,
            "rules": {"by_dimension": {"constraint": rules if rules is not None else default}},
        }
        (trial / "verifier/reward.json").write_text(json.dumps(reward))
        (trial / "result.json").write_text(json.dumps({
            "task_name": "agentic-labs/2280_medium_repair_plan_medium",
            "verifier_result": {"rewards": {"reward": score}},
        }))
    return run_dir


def add_recovery(run_dir, *, plan_id=PLAN_ID, steps=7, validated=True,
                 prepared_id=None, success_id=None):
    plan_dir = run_dir / "harbor/job/one/agent/recovery" / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "prepared.json").write_text(json.dumps({
        "ok": True,
        "odoo_writes": False,
        "plan_id": prepared_id or plan_id,
        "operation_counts": {"cancel": 1, "create": 1, "keep": 9, "update": 0},
        "step_count": steps,
        "expected_new_spend": 4794.94,
    }))
    (plan_dir / "success.json").write_text(json.dumps({
        "ok": True,
        "odoo_writes": True,
        "plan_id": success_id or plan_id,
        "verified_steps": steps,
        "execution_validated": validated,
        "benchmark_success": None,
    }))
    return plan_dir


def test_business_outcome_and_tool_execution_are_reported_separately(tmp_path):
    run_dir = write_run(tmp_path, score=99, rules=[
        {"status": "FAIL", "applicable": True, "expr": "check_budget"},
    ])
    add_recovery(run_dir)
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["evaluation"]["original_passed"] is True
    assert view["evaluation"]["strict_success"] is False
    assert view["recovery"]["execution_validated"] is True
    assert view["recovery"]["status"] == "executed"


def test_cost_is_null_not_zero_and_named_as_missing(tmp_path):
    run_dir = write_run(tmp_path)
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["usage"]["cost"]["usd"] is None
    assert view["usage"]["cost"]["source"] == "not_reported"
    assert "usage.cost.usd" in view["missing"]
    assert view["usage"]["input_tokens"] is None


def test_recorded_usage_is_totalled_without_inventing_a_cost(tmp_path):
    run_dir = write_run(tmp_path)
    trial = run_dir / "harbor/job/one"
    (trial / "result.json").write_text(json.dumps({
        "task_name": "agentic-labs/2280_medium_repair_plan_medium",
        "verifier_result": {"rewards": {"reward": 100}},
        "agent_result": {
            "n_input_tokens": 1000, "n_output_tokens": 40,
            "n_cache_tokens": 900, "cost_usd": None,
        },
    }))
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["usage"]["total_tokens"] == 1040
    assert view["usage"]["cache_tokens"] == 900
    assert view["usage"]["cost"]["usd"] is None


def test_recovery_plan_is_bound_and_counted(tmp_path):
    run_dir = write_run(tmp_path)
    add_recovery(run_dir, steps=34)
    view = build_run_view(run_dir, tmp_path / "runs")
    recovery = view["recovery"]
    assert recovery["plan_id"] == PLAN_ID
    assert recovery["consistent"] is True
    assert recovery["planned_step_count"] == 34
    assert recovery["verified_steps"] == 34
    assert recovery["operation_counts"] == {"keep": 9, "update": 0, "cancel": 1, "create": 1}
    assert recovery["expected_new_spend"] == 4794.94
    assert recovery["odoo_writes_planned"] is False
    assert recovery["odoo_writes_executed"] is True
    assert recovery["audited_erp_writes"] is None


def test_recovery_plan_id_disagreement_is_flagged_not_hidden(tmp_path):
    run_dir = write_run(tmp_path)
    add_recovery(run_dir, success_id=OTHER_PLAN_ID)
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["recovery"]["consistent"] is False
    assert view["recovery"]["plan_id"] is None
    assert view["consistency"]["ok"] is False
    assert "recovery_plan_ids_disagree" in view["consistency"]["issues"]


def test_run_without_recovery_artifacts_is_not_reported_as_zero(tmp_path):
    run_dir = write_run(tmp_path, score=21, rules=[
        {"status": "FAIL", "applicable": True, "expr": "check_budget"},
    ])
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["recovery"]["status"] == "not_used"
    assert view["recovery"]["plan_id"] is None
    assert view["recovery"]["verified_steps"] is None
    assert "recovery.audited_erp_writes" in view["missing"]
    assert view["evaluation"]["strict_success"] is False


def test_na_rules_are_excluded_from_the_applicable_count(tmp_path):
    run_dir = write_run(tmp_path, rules=[
        {"status": "PASS", "applicable": True, "expr": "a"},
        {"status": "PASS", "applicable": True, "expr": "b"},
        {"status": "NA", "applicable": False, "expr": "c"},
    ])
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["evaluation"]["applicable_rule_count"] == 2
    assert view["evaluation"]["passed_rule_count"] == 2
    assert view["evaluation"]["failed_rule_count"] == 0


def test_rule_projection_keeps_unknown_rules_unknown():
    assert rule_projection(None) is None
    assert rule_projection({"rules": {"by_dimension": {}}}) is None
    assert rule_projection({"rules": {"by_dimension": {"x": [{"status": "MAYBE"}]}}}) is None


def test_rule_results_preserve_outcomes_without_verifier_implementation(tmp_path):
    run_dir = write_run(tmp_path, score=98.5, rules=[
        {"rule": "po_origin_traceability", "status": "FAIL", "applicable": True,
         "expr": "private checker expression", "args": ["private argument"]},
        {"rule": "demand_coverage", "status": "PASS", "applicable": True},
        {"rule": "unused_rule", "status": "NA", "applicable": False},
    ])
    evaluation = build_run_view(run_dir, tmp_path / "runs")["evaluation"]
    assert evaluation["rule_results"] == [
        {"dimension": "constraint", "rule": "po_origin_traceability", "status": "FAIL", "applicable": True},
        {"dimension": "constraint", "rule": "demand_coverage", "status": "PASS", "applicable": True},
        {"dimension": "constraint", "rule": "unused_rule", "status": "NA", "applicable": False},
    ]
    assert evaluation["passed_rule_count"] == 1
    assert evaluation["applicable_rule_count"] == 2
    assert "private" not in json.dumps(evaluation)


@pytest.mark.parametrize("rules", [[], [{"status": "MAYBE"}]])
def test_missing_or_unparseable_rule_results_remain_unknown(tmp_path, rules):
    run_dir = write_run(tmp_path, rules=rules)
    evaluation = build_run_view(run_dir, tmp_path / "runs")["evaluation"]
    assert evaluation["rule_results"] is None


def test_rule_results_do_not_repurpose_expressions_as_names(tmp_path):
    run_dir = write_run(tmp_path, rules=[
        {"status": "PASS", "expr": "secret expression"},
        {"status": "FAIL", "rule": "https://private/path?key=secret"},
    ])
    rows = build_run_view(run_dir, tmp_path / "runs")["evaluation"]["rule_results"]
    assert [row["rule"] for row in rows] == [None, None]
    assert [row["status"] for row in rows] == ["PASS", "FAIL"]


def test_legacy_record_keeps_the_console_field_names(tmp_path):
    run_dir = write_run(tmp_path, "flat", score=100, agent="pi")
    view = build_run_view(run_dir, tmp_path / "runs")
    record = legacy_record(view)
    assert record["run_id"] == "flat"
    assert record["task_label"] == "供应商断供"
    assert record["category"] == "agent_evaluation"
    assert record["score"] == 100
    assert record["strict_success"] is True
    assert record["metrics"]["cost_usd"] is None
    assert record["evidence"]["manifest"] == "runs/flat/manifest.json"


def test_runview_rejects_evidence_behind_a_symlink(tmp_path):
    run_dir = write_run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("{}")
    (run_dir / "linked.json").symlink_to(outside / "secret.json")
    assert build_run_view(run_dir, tmp_path / "runs") is None


def test_runview_rejects_paths_outside_the_runs_root(tmp_path):
    write_run(tmp_path)
    assert build_run_view(tmp_path / "elsewhere", tmp_path / "runs") is None


def test_api_serves_the_versioned_view(tmp_path):
    run_dir = write_run(tmp_path)
    add_recovery(run_dir)
    with make_server(tmp_path, 0) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = httpx.get(f"{base}/api/runview/trial")
            assert response.status_code == 200
            body = response.json()
            assert body["kind"] == "forgeflow.run_view"
            assert body["schema_version"] == 1
            assert body["recovery"]["plan_id"] == PLAN_ID
            assert httpx.get(f"{base}/api/runview/missing").status_code == 404
            assert httpx.get(f"{base}/api/runview/..%2F.env").status_code == 404
        finally:
            server.shutdown()


@pytest.mark.skipif(not RECORDED_RUNS.is_dir(), reason="recorded runs are not present")
@pytest.mark.parametrize("run_id,score,rules,failed,recovery_status", [
    ("workflow-a-off-20261020-01", 100.0, 51, 0, "executed"),
    ("workflow-b-off-20261020-01", 100.0, 95, 0, "executed"),
    ("recovery-b-off-20261020-01", 21.0, 95, 7, "not_used"),
])
def test_projection_reproduces_the_recorded_runs(run_id, score, rules, failed, recovery_status):
    view = build_run_view(RECORDED_RUNS / run_id, RECORDED_RUNS)
    assert view is not None
    evaluation = view["evaluation"]
    assert evaluation["score"] == score
    assert evaluation["applicable_rule_count"] == rules
    assert evaluation["failed_rule_count"] == failed
    assert evaluation["evaluation_valid"] is True
    assert view["recovery"]["status"] == recovery_status
    assert view["usage"]["cost"]["usd"] is None
    assert view["consistency"]["ok"] is True


def test_security_profile_defaults_to_none_for_historical_runs(tmp_path):
    run_dir = write_run(tmp_path)
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["experiment"]["security_profile"] == {
        "name": "none", "recorded": False, "verifier_env_added": [],
    }
    record = legacy_record(view)
    assert record["security_profile"] == "none"
    assert record["security_profile_recorded"] is False


def test_security_profile_and_added_env_are_exposed(tmp_path):
    run_dir = write_run(tmp_path)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["security_profile"] = {
        "name": "verifier-hardened-v1",
        "verifier_env_added": {"PYTHONSAFEPATH": "1", "UV_NO_CONFIG": "1"},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    view = build_run_view(run_dir, tmp_path / "runs")
    profile = view["experiment"]["security_profile"]
    assert profile["name"] == "verifier-hardened-v1"
    assert profile["recorded"] is True
    assert profile["verifier_env_added"] == ["PYTHONSAFEPATH", "UV_NO_CONFIG"]
    assert legacy_record(view)["security_profile"] == "verifier-hardened-v1"


def _attest(run_dir, tmp_path):
    from forgeflow import attestation

    key = attestation.load_key(attestation.generate_key(tmp_path / "key.pem")["path"])
    record = attestation.build_verifier_record(
        run_id=run_dir.name, task="2280_medium_repair_plan_medium", agent="pi",
        model="nvidia/test", verifier={
            "command": "/tests/test.sh", "effective_uid": 0, "cwd": "/workspace",
            "started_at_utc": "2026-10-20T13:00:00+00:00",
            "finished_at_utc": "2026-10-20T13:01:00+00:00", "exit_code": 0,
        }, artifacts=[],
    )
    sealed, signature = attestation.seal_record(record, key)
    attestation.write_record(run_dir, sealed, signature)


def test_attestation_is_absent_for_historical_runs(tmp_path):
    run_dir = write_run(tmp_path)
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["attestation"] == {"status": "absent", "reason": "record_missing"}
    assert legacy_record(view)["attestation"] == "absent"


def test_a_signed_record_is_reported_as_verified(tmp_path):
    run_dir = write_run(tmp_path)
    _attest(run_dir, tmp_path)
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["attestation"]["status"] == "verified"
    assert legacy_record(view)["attestation"] == "verified"


def test_a_tampered_record_is_reported_as_invalid(tmp_path):
    run_dir = write_run(tmp_path)
    _attest(run_dir, tmp_path)
    path = run_dir / "attestation/verifier.json"
    payload = json.loads(path.read_text())
    payload["run_id"] = "another-run"
    path.write_text(json.dumps(payload))
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["attestation"]["status"] == "invalid"
    assert view["attestation"]["reason"]
