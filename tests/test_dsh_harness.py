from __future__ import annotations

import json
from pathlib import Path

from forgeflow.api.dsh_harness import (
    MAX_RESULT_SUMMARY,
    DshHarness,
    DshUnavailable,
    _result_summary,
    _tool_input,
)


def _events(*items: dict) -> str:
    return "\n".join(json.dumps(item) for item in items) + "\n"


def _harness(tmp_path: Path) -> DshHarness:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('x')\n", encoding="utf-8")
    cli = tmp_path / "dsh.js"
    cli.write_text("", encoding="utf-8")
    patch = tmp_path / "patch.yml"
    patch.write_text("[]\n", encoding="utf-8")
    return DshHarness(
        cli=cli, patch=patch, home=tmp_path / "home", workspace=workspace,
        sessions_file=tmp_path / "sessions.json",
    )


def test_general_answer_is_not_forced_to_cite(tmp_path: Path):
    harness = _harness(tmp_path)
    reply = harness._parse(_events(
        {"type": "session", "sessionId": "s-1"},
        {"type": "thinking", "text": "This is a normal greeting."},
        {"type": "final", "text": "Hello — nice to meet you."},
    ))
    assert reply.answer == "Hello — nice to meet you."
    assert reply.citations == []
    assert reply.scope == "general"
    assert reply.session_id == "s-1"


def test_project_answer_keeps_code_citations_and_activity(tmp_path: Path):
    harness = _harness(tmp_path)
    reply = harness._parse(_events(
        {"type": "session", "sessionId": "s-2"},
        {"type": "tool_call", "callId": "c1", "tool": "read", "input": {"file_path": "app.py"}},
        {"type": "tool_result", "callId": "c1", "status": "completed", "result": "..."},
        {"type": "final", "text": "See app.py:1 for the entry point."},
    ))
    assert reply.citations == ["app.py:1"]
    assert reply.scope == "project"
    assert any(step["kind"] == "read" and step["status"] == "done" for step in reply.activity)


def test_ask_resumes_saved_session_and_uses_read_only_env(tmp_path: Path, monkeypatch):
    harness = _harness(tmp_path)
    harness._sessions["conv-1"] = "session-abc"
    harness._save_sessions()
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert kwargs["env"]["DSH_PERMISSION_MODE"] == "read-only"
        return type("Done", (), {
            "returncode": 0,
            "stdout": _events(
                {"type": "session", "sessionId": "session-abc"},
                {"type": "final", "text": "resumed"},
            ),
            "stderr": "private reasoning must not escape",
        })()

    monkeypatch.setattr("forgeflow.api.dsh_harness.subprocess.run", fake_run)
    reply = harness.ask("follow up", "conv-1")
    assert reply.answer == "resumed"
    assert calls[0][-4:] == ["--session-id", "session-abc", "--", "follow up"]


def test_missing_final_is_a_stable_error(tmp_path: Path):
    harness = _harness(tmp_path)
    try:
        harness._parse(_events({"type": "status", "phase": "turn_start"}))
    except DshUnavailable as error:
        assert error.code == "dsh_no_final"
    else:
        raise AssertionError("expected DshUnavailable")


def test_tool_input_extracts_evidence_ids_paths_and_offset():
    assert _tool_input({"case_id": "case-1"}) == "case-1"
    assert _tool_input({"run_id": "run-1"}) == "run-1"
    assert _tool_input({"query": "verifier"}) == "verifier"
    assert _tool_input({"path": "src/app.py"}) == "src/app.py"
    assert _tool_input({"file_path": "app.py", "offset": 40}) == "app.py:40"
    assert _tool_input({"path": "src/app.py", "offset": 7}) == "src/app.py:7"
    assert _tool_input({"offset": 12}) == "offset 12"
    assert _tool_input({"pattern": "class Foo", "path": "src"}) == "class Foo in src"
    # Existing single-key precedence and rendering stay intact.
    assert _tool_input({"file_path": "app.py"}) == "app.py"
    assert _tool_input({"pattern": "class Foo"}) == "class Foo"
    assert _tool_input({"command": "ls"}) == "ls"
    assert _tool_input({"description": "read docs"}) == "read docs"
    assert _tool_input(None) == ""


def test_mcp_tools_get_human_labels_and_keep_the_raw_kind(tmp_path: Path):
    harness = _harness(tmp_path)
    reply = harness._parse(_events(
        {"type": "tool_call", "callId": "c1", "tool": "mcp__forgeflow__get_case",
         "input": {"case_id": "case-1"}},
        {"type": "tool_result", "callId": "c1", "status": "completed",
         "result": json.dumps({"state": "awaiting_approval"})},
        {"type": "tool_call", "callId": "c2", "tool": "mcp__forgeflow__list_runs",
         "input": {}},
        {"type": "tool_result", "callId": "c2", "status": "completed",
         "result": json.dumps([{"run_id": "run-1"}])},
        {"type": "tool_call", "callId": "c3", "tool": "mcp__forgeflow__search_evidence",
         "input": {"query": "verifier"}},
        {"type": "tool_result", "callId": "c3", "status": "completed",
         "result": json.dumps([])},
        {"type": "final", "text": "done"},
    ))
    steps = {step["kind"]: step for step in reply.activity}
    assert steps["mcp__forgeflow__get_case"]["label"] == "case · case-1"
    assert steps["mcp__forgeflow__get_case"]["detail"] == "awaiting_approval"
    assert steps["mcp__forgeflow__list_runs"]["label"] == "run"
    assert steps["mcp__forgeflow__search_evidence"]["label"] == "evidence search · verifier"


def test_tool_result_summary_is_bounded_and_keeps_metadata(tmp_path: Path):
    harness = _harness(tmp_path)
    payload = {
        "status": "awaiting_approval",
        "count": 3,
        "truncated": True,
        "total": 10,
        "records": [{"private": "x" * 5000}],
    }
    reply = harness._parse(_events(
        {"type": "tool_call", "callId": "c1", "tool": "mcp__forgeflow__list_cases",
         "input": {}},
        {"type": "tool_result", "callId": "c1", "status": "completed",
         "result": json.dumps(payload)},
        {"type": "final", "text": "done"},
    ))
    step = reply.activity[0]
    assert step["detail"] == "awaiting_approval · 3 items · truncated · total 10"
    assert len(step["detail"]) <= MAX_RESULT_SUMMARY + 1
    assert "x" * 100 not in step["detail"]


def test_failed_tool_result_keeps_only_the_first_error_line(tmp_path: Path):
    harness = _harness(tmp_path)
    reply = harness._parse(_events(
        {"type": "tool_call", "callId": "c1", "tool": "read",
         "input": {"file_path": "app.py"}},
        {"type": "tool_result", "callId": "c1", "status": "error",
         "result": "case_not_found\n  at private/path.py:9\nsecret"},
        {"type": "final", "text": "done"},
    ))
    step = reply.activity[0]
    assert step["status"] == "failed"
    assert step["detail"] == "error: case_not_found"
    assert "private/path.py" not in step["detail"]


def test_result_summary_reads_lists_and_structured_errors():
    assert _result_summary({
        "status": "completed",
        "result": json.dumps({"results": [1, 2], "total": 9}),
    }) == "2 items · total 9"
    assert _result_summary({
        "status": "error",
        "result": json.dumps({"error": {"code": "case_not_found", "message": "missing"}}),
    }) == "error: missing"


def test_result_summary_caps_a_long_first_line():
    summary = _result_summary({"status": "completed", "result": "a" * 5000})
    assert len(summary) == MAX_RESULT_SUMMARY + 1
    assert summary.endswith("…")


def test_search_evidence_result_is_structured_for_dsh_search_block(tmp_path: Path):
    harness = _harness(tmp_path)
    reply = harness._parse(_events(
        {"type": "tool_call", "callId": "c1", "tool": "mcp__forgeflow__search_evidence", "input": {"query": "approval"}},
        {"type": "tool_result", "callId": "c1", "status": "completed", "result": json.dumps([
            {"kind": "case", "id": "case-1", "excerpt": "approval waiting"},
            {"kind": "run", "id": "run-1", "excerpt": "verified"},
        ])},
        {"type": "final", "text": "Two evidence rows."},
    ))
    step = next(item for item in reply.activity if item.get("kind") == "mcp__forgeflow__search_evidence")
    assert step["evidence"][0]["id"] == "case-1"
    assert step["evidence"][1]["kind"] == "run"


def test_case_brief_payload_is_bounded_for_json_tree(tmp_path: Path):
    harness = _harness(tmp_path)
    reply = harness._parse(_events(
        {"type": "tool_call", "callId": "c1", "tool": "mcp__forgeflow__case_brief", "input": {"case_id": "case-1"}},
        {"type": "tool_result", "callId": "c1", "status": "completed", "result": json.dumps({"case_id": "case-1", "state": "awaiting_approval"})},
        {"type": "final", "text": "Case is waiting."},
    ))
    step = next(item for item in reply.activity if item.get("kind") == "mcp__forgeflow__case_brief")
    assert step["payload"]["state"] == "awaiting_approval"


def test_scoped_cli_keeps_legacy_cost_semantics_in_one_existing_prompt(tmp_path):
    from forgeflow.api.app import _case_question_prompt
    from forgeflow.api.case_qa import PLANNED_COST_CONTEXT

    harness = _harness(tmp_path)
    harness.evidence_path = tmp_path / "scoped-evidence.json"
    for question in (
        "What does the current case cost?",
        _case_question_prompt({"plan": {"new_spend": 133394.07}}, "Explain this amount."),
    ):
        command = harness._command(question, "retained-session")
        assert command[-4:-1] == ["--session-id", "retained-session", "--"]
        assert command[-1].count(PLANNED_COST_CONTEXT) == 1
        assert command[-1].endswith(question)
