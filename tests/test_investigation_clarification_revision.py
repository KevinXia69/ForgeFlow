"""A stopped investigation can close once with a question, without resuming work."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from investigation_support import EVENT
from test_investigation_service import RUN_ID, setup_service, state, validate

from forgeflow.investigation.service import InvestigationError, InvestigationService

QUESTION = {"status": "needs_clarification", "reason": "The authorized order budget is missing",
            "question": "What budget cap does the authorized decision-maker approve for order 30?"}


def stopped_service(tmp_path):
    service, source, _ = setup_service(tmp_path)
    draft = {"request": {}, "evidence": []}
    assert validate(service, draft)["status"] == "investigating"
    result = validate(service, draft)
    assert result["status"] == "technical_failure"
    assert result["terminal"]["stop_origin"] == "invalid_draft_guard"
    return service, source


def restart(service, source, *, clock=lambda: 1001):
    return InvestigationService(source=source, contract=service.contract, event=EVENT,
        store=service.store, bundle=service.bundle, connection_factory=lambda: object(), clock=clock)


def test_trusted_stop_allows_one_question_across_restart_without_new_business_actions(tmp_path, monkeypatch):
    service, source = stopped_service(tmp_path)
    before = state(service)
    monkeypatch.setattr(source, "query", lambda *_: pytest.fail("A stopped episode cannot query"))
    restarted = restart(service, source)
    for action, payload in (("query", {"kind": "policy", "arguments": {}}),
                            ("validate", {"draft": {"request": {}, "evidence": []}}),
                            ("prepare", {"draft_id": before["drafts"][0]["draft_id"]})):
        assert restarted.dispatch(RUN_ID, action, payload)["status"] == "technical_failure"
        assert state(restarted) == before
    closed = restarted.dispatch(RUN_ID, "finish", QUESTION)
    assert closed["status"] == "needs_clarification" and closed["prepared"] is None
    assert closed["terminal"]["basis"] == "agent_reported"
    assert closed["terminal"]["preceding_stop"] == {
        "status": before["status"], "terminal": before["terminal"],
        "usage": before["usage"], "draft_revision": len(before["drafts"])}
    after = state(restarted)
    for key in ("usage", "observations", "drafts", "attempts", "events", "started_at", "limits"):
        assert after[key] == before[key]
    again = restart(restarted, source)
    for action, payload in (("finish", {**QUESTION, "question": "A different question?"}),
                            ("query", {"kind": "policy", "arguments": {}}),
                            ("validate", {"draft": {}}), ("prepare", {"draft_id": "invented"})):
        assert again.dispatch(RUN_ID, action, payload) == closed
        assert state(again) == after


@pytest.mark.parametrize("payload", [
    {**QUESTION, "reason": "x" * 201}, {**QUESTION, "question": " "},
    {**QUESTION, "question": "x" * 1201}, {"status": "needs_clarification", "reason": "Missing budget"},
    {**QUESTION, "extra": True},
])
def test_invalid_closing_arguments_do_not_consume_the_one_legal_close(tmp_path, payload):
    service, _ = stopped_service(tmp_path)
    before = state(service)
    with pytest.raises(InvestigationError):
        service.dispatch(RUN_ID, "finish", payload)
    assert state(service) == before
    assert service.dispatch(RUN_ID, "finish", QUESTION)["status"] == "needs_clarification"


@pytest.mark.parametrize("clock", [lambda: 4600, lambda: 4601, lambda: 999])
def test_deadline_or_clock_rollback_prevents_closure_and_preserves_stop_provenance(tmp_path, clock):
    service, source = stopped_service(tmp_path)
    before = state(service)
    service = restart(service, source, clock=clock)
    closed = service.dispatch(RUN_ID, "finish", QUESTION)
    assert closed["status"] == "budget_exhausted"
    assert closed["terminal"]["reason"] == "time_limit"
    assert closed["terminal"]["preceding_stop"]["terminal"] == before["terminal"]
    assert closed["terminal"]["preceding_stop"]["usage"] == before["usage"]
    assert state(service)["events"] == before["events"]
    assert restart(service, source).dispatch(RUN_ID, "finish", QUESTION) == closed


@pytest.mark.parametrize("status,reason", [
    ("prepared", "validated_preparation"), ("infeasible", "planning_model_infeasible"),
    ("unsupported", "unsupported_recovery_scope"), ("budget_exhausted", "validations_limit"),
    ("technical_failure", "preparation_interrupted"),
    ("technical_failure", "repeated_failure_without_new_evidence"),
])
def test_other_terminal_states_stay_sticky_even_with_a_prior_guard_event(tmp_path, status, reason):
    service, _ = stopped_service(tmp_path)
    saved = state(service)
    service._terminal(saved, status, reason)
    before = state(service)
    assert service.dispatch(RUN_ID, "finish", QUESTION)["status"] == status
    assert state(service) == before


def test_model_reported_guard_reason_cannot_unlock_clarification(tmp_path):
    service, _, _ = setup_service(tmp_path)
    result = service.dispatch(RUN_ID, "finish", {
        "status": "technical_failure", "reason": "repeated_invalid_draft_without_new_evidence", "question": ""})
    assert result["terminal"]["basis"] == "agent_reported"
    before = state(service)
    assert service.dispatch(RUN_ID, "finish", QUESTION) == result
    assert state(service) == before


def test_unbound_marker_cannot_unlock_clarification(tmp_path):
    service, _ = stopped_service(tmp_path)
    saved = state(service)
    saved["terminal"]["input_sha256"] = "0" * 64
    service._save(saved)
    assert service.dispatch(RUN_ID, "finish", QUESTION)["status"] == "technical_failure"
    assert state(service) == saved


def test_non_clarification_finish_cannot_change_stop_or_consume_the_question_close(tmp_path):
    service, _ = stopped_service(tmp_path)
    before = state(service)
    for status in ("unsupported", "technical_failure", "prepared"):
        result = service.dispatch(RUN_ID, "finish", {**QUESTION, "status": status})
        assert result["status"] == "technical_failure"
        assert state(service) == before
    assert service.dispatch(RUN_ID, "finish", QUESTION)["status"] == "needs_clarification"


def test_actual_tools_keep_five_actions_and_explain_missing_budget_authority():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to load the real tool descriptions")
    source = Path(__file__).resolve().parents[1] / "src/forgeflow/agents/native_investigation.js"
    script = "import{pathToFileURL}from'node:url';const{TOOLS}=await import(pathToFileURL(process.argv[1]));console.log(JSON.stringify(TOOLS));"
    response = subprocess.run([node, "--input-type=module", "-e", script, str(source)],
                              check=True, capture_output=True, text=True, timeout=10)
    tools = {tool["action"]: tool for tool in json.loads(response.stdout)}
    assert set(tools) == {"query", "validate", "prepare", "status", "finish"}
    for action in ("query", "finish"):
        description = tools[action]["description"]
        assert "budget_cap" in description and "null" in description and "missing" in description
        assert "not unlimited" in description and "authorized decision-maker" in description
        assert "600" not in description and "650" not in description and "dev-" not in description
    assert "one valid needs_clarification" in tools["finish"]["description"]
