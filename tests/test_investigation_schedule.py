"""Freeze and scheduling guards using only already-exposed development records."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("investigation_schedule", ROOT / "scripts/investigation_schedule.py")
scheduler = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(ROOT / "scripts"))
try:
    SPEC.loader.exec_module(scheduler)
finally:
    sys.path.pop(0)


def test_predeclared_layout_balances_cases_replicates_and_ablation():
    jobs = scheduler.job_layout("unit")
    assert len(jobs) == len({job["run_id"] for job in jobs}) == 64
    assert Counter(job["method"] for job in jobs) == {
        "rules": 8, "baseline": 24, "investigation": 24, "single-draft": 8}
    for position in range(8):
        assert Counter(job["method"] for job in jobs if job["case_position"] == position) == {
            "rules": 1, "baseline": 3, "investigation": 3, "single-draft": 1}
    assert "single-prepare" not in {job["method"] for job in jobs}
    with pytest.raises(ValueError):
        scheduler.job_layout("../escape")


@pytest.fixture
def local_freeze(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "ROOT", tmp_path)
    monkeypatch.setattr(scheduler, "implementation_hashes", lambda: {
        "runtime.py": "a" * 64, "src/forgeflow/investigation/grading.py": "d" * 64})
    monkeypatch.setattr(scheduler, "model_configuration", lambda: {
        "model_id": "unit-model", "context_window": 1048576, "max_output_tokens": 1048576})
    freeze = scheduler.freeze_manifest("unit", authorize_generation=True)
    path = tmp_path / "freeze.json"
    scheduler.write_json(path, freeze)
    return path, freeze


def exposed_suite(freeze_path):
    from forgeflow.investigation.fixtures import supplier_cancellation_control

    case = supplier_cancellation_control()
    return {"suite_schema": "forgeflow.investigation_suite", "version": 1,
            "split": "heldout", "suite_id": "unit-reused-development",
            "freeze_sha256": scheduler.digest_file(freeze_path),
            "cases": [{"case_id": f"exposed-control-{i}", "event": case.event,
                       "records": case.records, "policy": case.policy.payload(), "oracle": case.oracle}
                      for i in range(8)]}


def test_pre_data_freeze_then_bound_schedule_has_no_circular_dependency(local_freeze, tmp_path):
    freeze_path, freeze = local_freeze
    assert len(freeze["jobs"]) == 64
    suite = tmp_path / "exposed-development.json"
    scheduler.write_json(suite, exposed_suite(freeze_path))
    plan = scheduler.make_plan(freeze_path, suite, tmp_path / "batch")
    scheduler.check_plan(plan)
    assert len(plan["jobs"]) == 64
    assert plan["jobs"][0]["case_id"] == "exposed-control-0"
    command = scheduler.command_for(plan, plan["jobs"][-1])
    assert "--single-draft" in command
    assert "--single-prepare" not in command
    assert command[command.index("--max-total-tokens") + 1] == "2000000"
    assert command[command.index("--seconds") + 1] == "3600"
    assert command[command.index("--max-output-tokens") + 1] == "1048576"
    assert not any("oracle" in argument for argument in command)
    suite.write_text(suite.read_text() + "\n")
    with pytest.raises(ValueError, match="dataset_bytes_changed"):
        scheduler.check_plan(plan)


@pytest.mark.parametrize("field,value", [
    ("source_files_sha256", {"runtime.py": "b" * 64}),
    ("model_configuration", {"model_id": "changed"}),
    ("jobs", []),
    ("max_concurrency", 3),
    ("automatic_business_retries", True),
    ("budgets", {"seconds": 600}),
])
def test_freeze_rejects_runtime_model_design_or_budget_drift(local_freeze, field, value):
    _, freeze = local_freeze
    altered = copy.deepcopy(freeze)
    altered[field] = value
    with pytest.raises(ValueError):
        scheduler.check_freeze(altered)


def test_generation_requires_explicit_freeze_authorization(local_freeze, tmp_path):
    freeze_path, freeze = local_freeze
    freeze["heldout_generation_authorized"] = False
    scheduler.write_json(freeze_path, freeze)
    suite = tmp_path / "exposed-development.json"
    scheduler.write_json(suite, exposed_suite(freeze_path))
    with pytest.raises(ValueError, match="not_authorized"):
        scheduler.make_plan(freeze_path, suite, tmp_path / "batch")


def test_resume_refuses_to_repeat_a_running_or_interrupted_job(local_freeze, tmp_path, monkeypatch):
    freeze_path, _ = local_freeze
    suite = tmp_path / "exposed-development.json"
    scheduler.write_json(suite, exposed_suite(freeze_path))
    plan = scheduler.make_plan(freeze_path, suite, tmp_path / "batch")
    plan_path = tmp_path / "plan.json"
    scheduler.write_json(plan_path, plan)
    scheduler.write_json(tmp_path / "batch/status.json", {
        "plan_sha256": scheduler.digest_file(plan_path), "state": "running",
        "jobs": {job["run_id"]: {"state": "running", "launch_count": 1} for job in plan["jobs"]}})
    launched = []
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *a, **kw: launched.append(a))
    with pytest.raises(ValueError, match="no_automatic_retry"):
        scheduler.run_batch(plan_path)
    assert launched == []
    assert json.loads((tmp_path / "batch/status.json").read_text())["state"] == "running"


@pytest.mark.parametrize("job_index", [0, 56])
def test_launch_failure_is_scored_archived_and_never_automatically_repeated(
        local_freeze, tmp_path, monkeypatch, job_index):
    freeze_path, _ = local_freeze
    suite = tmp_path / "exposed-development.json"
    scheduler.write_json(suite, exposed_suite(freeze_path))
    plan = scheduler.make_plan(freeze_path, suite, tmp_path / "batch")
    plan_path = tmp_path / "plan.json"
    scheduler.write_json(plan_path, plan)
    rows = {job["run_id"]: {"state": "finished", "launch_count": 1} for job in plan["jobs"]}
    identity = plan["jobs"][job_index]["run_id"]
    rows[identity] = {"state": "pending", "launch_count": 0}
    scheduler.write_json(tmp_path / "batch/status.json", {
        "plan_sha256": scheduler.digest_file(plan_path), "state": "stopped", "jobs": rows})
    launches, archives = [], []

    def fail_launch(*args, **kwargs):
        launches.append(args)
        raise FileNotFoundError("missing_runtime")

    def archive(out, destination, **kwargs):
        archives.append((out, destination))
        return {"archive_sha256": "c" * 64}

    monkeypatch.setattr(scheduler.subprocess, "Popen", fail_launch)
    monkeypatch.setitem(sys.modules, "investigation_archive",
                        SimpleNamespace(export_episode=archive, secret_values=lambda: ()))
    state = scheduler.run_batch(plan_path, concurrency=2)
    assert state["state"] == "finished"
    assert state["jobs"][identity]["state"] == "launch_failed"
    assert state["jobs"][identity]["launch_count"] == 1
    summary = json.loads((tmp_path / "batch/episodes" / identity / "summary.json").read_text())
    assert summary["status"] == "technical_failure"
    assert summary["score"] == 0
    assert summary["tokens"] is None
    assert summary["outcome_reason"] == "launcher_failed"
    assert summary["wall_seconds"] >= 0
    spec = importlib.util.spec_from_file_location("investigation_results", ROOT / "scripts/investigation_results.py")
    results = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(results)
    aggregate = results.summarize([tmp_path / "batch/episodes" / identity], grader_sha256="d" * 64)
    group = "heldout/ablation-single-draft" if job_index == 56 else "heldout/rules"
    assert aggregate["groups"][group]["business_success"] == {"numerator": 0, "denominator": 1, "rate": 0}
    assert aggregate["groups"][group]["request_correct"]["denominator"] == 1
    assert aggregate["groups"][group]["unknown_token_episodes"] == 1
    assert aggregate["groups"][group]["unknown_query_episodes"] == 1
    assert aggregate["groups"][group]["zero_write_episodes"] == 0
    assert aggregate["groups"][group]["cost_usd"] is None
    assert len(archives) == len(launches) == 1
    scheduler.run_batch(plan_path, concurrency=1)
    assert len(archives) == len(launches) == 1
