"""Budget accounting and the fixture runtime's fail-closed boundary."""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fixture_evaluate", ROOT / "scripts/investigation_evaluate.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def usage(total):
    return {"usage": {"totalTokens": total}, "stop_reason": "toolUse"}


def budget(**changes):
    return {"reported_total_tokens": 300, "provider_requests": 2, "completed_responses": 2,
            "in_flight": False, "partial_usage_unknown": False, "stop_reason": None, **changes}


def test_complete_provider_and_guard_usage_must_agree():
    assert runner.token_usage_projection(budget(), [usage(100), usage(200)]) == {
        "tokens": 300, "token_status": "within_budget"}
    assert runner.token_usage_projection(budget(stop_reason="token_budget_exhausted"),
                                         [usage(100), usage(200)]) == {
        "tokens": 300, "token_status": "token_budget_exhausted"}


@pytest.mark.parametrize("report,messages", [
    (None, [usage(100)]),
    (budget(reported_total_tokens=0, provider_requests=1, completed_responses=0, in_flight=True), []),
    (budget(provider_requests=3), [usage(100), usage(200)]),
    (budget(in_flight=True), [usage(100), usage(200)]),
    (budget(partial_usage_unknown=True), [usage(100), usage(200)]),
    (budget(reported_total_tokens=301), [usage(100), usage(200)]),
    (budget(), [usage(100)]),
    (budget(reported_total_tokens=200), [usage(0), usage(200)]),
    (budget(), [usage(100), {"usage": {"totalTokens": 200}, "stop_reason": "aborted"}]),
])
def test_interrupted_or_inconsistent_usage_stays_unknown(report, messages):
    assert runner.token_usage_projection(report, messages) == {
        "tokens": None, "token_status": "token_usage_unknown"}


def test_guard_wall_timeout_takes_precedence_without_claiming_known_usage():
    assert runner.execution_outcome("prepared", {"exit_code": 124}, "token_usage_unknown") == (
        "budget_exhausted", "wall_time_limit")
    assert runner.execution_outcome("prepared", {"host_timeout": True}, "within_budget") == (
        "budget_exhausted", "wall_time_limit")
    assert runner.execution_outcome("prepared", {}, "token_budget_exhausted") == (
        "budget_exhausted", "token_budget_exhausted")
    assert runner.execution_outcome("prepared", {"registry_verified": False}, "within_budget") == (
        "technical_failure", "tool_registry_unverified")


def test_host_requires_full_active_registry_schema(tmp_path):
    expected = [{"name": "bounded_query", "description": "Read", "parameters": {"type": "object"}}]
    a, b = tmp_path / "expected.json", tmp_path / "actual.jsonl"
    a.write_text(json.dumps(expected))
    assert runner.registry_verified(a, b) is False
    record = {"active": ["bounded_query"], "expected": ["bounded_query"], "registered": expected}
    b.write_text(json.dumps(record) + "\n")
    assert runner.registry_verified(a, b) is True
    record["active"].append("bash")
    b.write_text(json.dumps(record) + "\n")
    assert runner.registry_verified(a, b) is False
    record["active"].remove("bash")
    record["registered"][0]["parameters"]["type"] = "string"
    b.write_text(json.dumps(record) + "\n")
    assert runner.registry_verified(a, b) is False


def test_bridge_registry_mismatch_exits_even_when_extension_errors_are_caught(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the actual extension boundary")
    for name in ("native_investigation.js", "native_planner.js"):
        shutil.copyfile(ROOT / "src/forgeflow/agents" / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    (tmp_path / "bridge.json").write_text('{"arm":"investigation","url":"unused","token":"unused"}')
    text = runner.BRIDGE.replace("/opt/forgeflow/", str(tmp_path) + "/").replace(
        "/home/forgeflow/logs/fixture-registry.jsonl", str(tmp_path / "registry.jsonl"))
    (tmp_path / "bridge.js").write_text(text)
    invocation = """import extension from './bridge.js';
const callbacks=[], tools=[];
extension({registerTool:t=>tools.push(t),on:(_event,cb)=>callbacks.push(cb),
getActiveTools:()=>['bash'],getAllTools:()=>tools});
for(const callback of callbacks){try{callback()}catch{/* Pi catches extension exceptions. */}}
process.exit(0);"""
    result = subprocess.run([node, "--input-type=module", "-e", invocation], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 78, result.stderr


def test_common_deadline_includes_time_before_first_model_tool(tmp_path):
    from forgeflow.investigation.fixtures import supplier_cancellation_control
    from forgeflow.recovery.storage import read_json

    episode = runner.Episode(supplier_cancellation_control(), "investigation", tmp_path, 10, 180000)
    assert episode.started is None
    episode.begin()
    state = read_json(episode.service.root / "state.json")
    assert state["started_at"] == episode.started_wall
    assert state["usage"] == {"queries": 0, "prepares": 0, "validations": 0}
    episode.service.clock = lambda: episode.started_wall + 11
    result = episode.dispatch("query", {"kind": "policy", "arguments": {}})
    assert result["status"] == "budget_exhausted"
    assert result["usage"]["queries"] == 0


def development_suite():
    """Reuse already-public development inputs; never instantiate hidden recipes."""
    from forgeflow.investigation.fixtures import supplier_cancellation_control

    case = supplier_cancellation_control()
    return {"suite_schema": "forgeflow.investigation_suite", "version": 1,
            "split": "development", "suite_id": "unit-development",
            "freeze_sha256": "a" * 64, "cases": [{"case_id": case.case_id,
                "event": case.event, "records": case.records, "policy": case.policy.payload(),
                "oracle": case.oracle}]}


def test_explicit_host_suite_roundtrip_and_byte_binding(tmp_path):
    value = development_suite()
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(value))
    suite, signature = runner.load_suite(path, split="development")
    case = runner.suite_case(suite, value["cases"][0]["case_id"])
    assert case.policy.payload() == value["cases"][0]["policy"]
    assert case.oracle == value["cases"][0]["oracle"]
    assert suite["freeze_sha256"] == "a" * 64
    assert runner.load_suite(path, split="development", expected_sha256=signature)[1] == signature
    path.write_text(json.dumps(value) + "\n")
    with pytest.raises(ValueError, match="suite_bytes_changed"):
        runner.load_suite(path, split="development", expected_sha256=signature)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(split="heldout"),
    lambda value: value.update(version=True),
    lambda value: value["cases"].append(copy.deepcopy(value["cases"][0])),
    lambda value: value["cases"][0].update(case_id="../escape"),
    lambda value: value["cases"][0].update(saved_plan={}),
    lambda value: value["cases"][0]["oracle"].update(expected_status="success"),
])
def test_host_suite_rejects_wrong_split_duplicate_identity_and_unknown_payload(tmp_path, mutation):
    value = development_suite()
    mutation(value)
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        runner.load_suite(path, split="development")


def test_partial_policy_is_loaded_without_fabricating_authorization(tmp_path):
    from forgeflow.investigation.fixtures import development_cases
    from forgeflow.investigation.policy import PartialPolicy

    partial = next(case for case in development_cases() if isinstance(case.policy, PartialPolicy))
    value = development_suite()
    value["cases"] = [{"case_id": partial.case_id, "event": partial.event, "records": partial.records,
                       "policy": partial.policy.payload(), "oracle": partial.oracle}]
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(value))
    suite, _ = runner.load_suite(path, split="development")
    loaded = runner.suite_case(suite, partial.case_id)
    assert isinstance(loaded.policy, PartialPolicy)
    assert loaded.contract is None


def test_single_draft_blocks_only_the_second_validation_after_actual_failure(tmp_path):
    from forgeflow.investigation.fixtures import supplier_cancellation_control

    episode = runner.Episode(supplier_cancellation_control(), "investigation", tmp_path,
                             3600, 2000000, single_draft=True)
    first = episode.dispatch("validate", {"draft": {}})
    assert first["valid"] is False
    assert first["usage"]["validations"] == 1
    assert episode.blocked_validation_count == 0
    query = episode.dispatch("query", {"kind": "policy", "arguments": {}})
    assert query["records"]
    assert query["status"] == "investigating"
    second = episode.dispatch("validate", {"draft": {"request": {}}})
    assert second["status"] == "technical_failure"
    assert second["terminal"]["reason"] == "ablation_draft_retry_disabled"
    assert second["usage"]["validations"] == 1
    assert episode.blocked_validation_count == 1
    assert episode.conn.write_attempts == []


def test_normal_arm_retains_failed_draft_feedback_revision(tmp_path):
    from forgeflow.investigation.fixtures import supplier_cancellation_control

    episode = runner.Episode(supplier_cancellation_control(), "investigation", tmp_path, 3600, 2000000)
    episode.dispatch("validate", {"draft": {}})
    second = episode.dispatch("validate", {"draft": {"request": {}}})
    assert second["status"] == "investigating"
    assert second["usage"]["validations"] == 2
    assert episode.blocked_validation_count == 0


def test_single_draft_allows_first_valid_draft_and_real_preparation(tmp_path):
    from forgeflow.investigation.fixtures import supplier_cancellation_control
    from forgeflow.investigation.rules import run_rules

    episode = runner.Episode(supplier_cancellation_control(), "investigation", tmp_path,
                             3600, 2000000, single_draft=True)
    result = run_rules(episode.case.event, episode.dispatch)
    assert result["result"]["status"] == "prepared"
    assert episode.blocked_validation_count == 0
    assert episode.draft_retry_blocked is False
    assert episode.conn.fingerprint() == episode.before


def test_finish_arguments_project_like_production_and_can_be_repaired(tmp_path):
    from forgeflow.investigation.fixtures import supplier_cancellation_control

    episode = runner.Episode(supplier_cancellation_control(), "investigation", tmp_path, 3600, 2000000)
    result = episode.dispatch("finish", {"status": "needs_clarification", "reason": "x" * 201,
                                        "question": "What budget is authorized for SO-1?"})
    assert result == {"status": "tool_error", "code": "invalid_field"}
    assert episode.dispatch("status", {})["status"] == "investigating"
    result = episode.dispatch("finish", {"status": "needs_clarification", "reason": "Budget unavailable",
                                        "question": "What budget is authorized for SO-1?"})
    assert result["status"] == "needs_clarification"
    assert result["terminal"]["question"] == "What budget is authorized for SO-1?"


def test_actual_native_finish_schema_rejects_the_observed_overlong_arguments():
    from jsonschema import ValidationError, validate

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to read the actual native tool schema")
    source = ROOT / "src/forgeflow/agents/native_investigation.js"
    invocation = "import{pathToFileURL}from'node:url';const{TOOLS}=await import(pathToFileURL(process.argv[1]));console.log(JSON.stringify(TOOLS.find(t=>t.action==='finish').parameters));"
    result = subprocess.run([node, "--input-type=module", "-e", invocation, str(source)],
                            capture_output=True, text=True, timeout=10, check=True)
    schema = json.loads(result.stdout)
    payload = {"status": "needs_clarification", "reason": "x" * 200, "question": "y" * 1200}
    validate(payload, schema)
    for field, value in (("reason", "x" * 201), ("reason", ""), ("question", "y" * 1201)):
        with pytest.raises(ValidationError):
            validate({**payload, field: value}, schema)
