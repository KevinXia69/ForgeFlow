from __future__ import annotations

import json

import pytest

from forgeflow.ui import run_detail
from forgeflow.ui_trace import _safe_excerpt, parse_pi_events


def start(call_id="call_1", tool="bash", args=None):
    return {
        "type": "tool_execution_start", "toolCallId": call_id, "toolName": tool,
        "args": {"command": "printf 'ready'"} if args is None else args,
    }


def end(call_id="call_1", tool="bash", text="ready", is_error=False):
    return {
        "type": "tool_execution_end", "toolCallId": call_id, "toolName": tool,
        "result": {"content": [{"type": "text", "text": text}], "details": {}},
        "isError": is_error,
    }


def events(*items):
    return "\n".join(json.dumps(item) for item in items)


def test_replays_real_pi_event_shape_in_start_order_and_does_not_invent_timings():
    result = parse_pi_events(events(
        {"type": "session", "version": 3, "timestamp": "2026-10-20T13:00:00Z"},
        {"type": "message_update", "thinking": "do-not-display-model-reasoning"},
        start(),
        start("call_2", "read", {"path": "/workspace/order.json"}),
        end("call_2", "read", "file unavailable", True),
        end(),
        {"type": "agent_settled"},
    ))
    assert [step["tool"] for step in result["steps"]] == ["bash", "read"]
    assert [step["status"] for step in result["steps"]] == ["succeeded", "failed"]
    assert all(step["duration_seconds"] is None for step in result["steps"])
    assert result["steps"][0]["result_summary"] == "ready"
    assert "do-not-display-model-reasoning" not in json.dumps(result)


def test_credentials_sensitive_reads_and_write_contents_are_not_exposed():
    result = parse_pi_events(events(
        start(tool="read", args={"path": "/workspace/.env"}),
        end(tool="read", text="short-secret-with-no-label"),
        start("call_2", "write", {"path": "/workspace/notes.txt", "content": "private-file-content"}),
        end("call_2", "write", "Successfully wrote file"),
        start("call_3"),
        end("call_3", text=(
            '{"password": "tiny-secret", "API_KEY": "other-secret"}\n'
            "Authorization: Bearer credential-value\n"
            "https://user:password-value@example.test\n"
            "abcdef1234567890abcdef1234567890abcdef1234567890"
        )),
    ))
    encoded = json.dumps(result)
    for secret in (
        "short-secret-with-no-label", "private-file-content", "tiny-secret",
        "other-secret", "credential-value", "password-value",
        "abcdef1234567890abcdef1234567890abcdef1234567890",
    ):
        assert secret not in encoded
    assert "notes.txt" in encoded
    assert "[REDACTED" in encoded


def test_incomplete_or_malformed_events_remain_unknown_instead_of_successful():
    result = parse_pi_events(
        events(start(), start("call_2"), end("call_2", is_error="false"))
        + "\nnot JSON\n"
        + events({"type": "tool_execution_end", "toolName": "bash"})
    )
    assert [step["status"] for step in result["steps"]] == ["incomplete", "unknown"]
    assert result["invalid_line_count"] == 2
    assert result["steps"][0]["result_summary"] is None


def test_missing_start_hides_result_and_cannot_duplicate_a_tool_operation():
    result = parse_pi_events(events(
        end(text="unlabelled-secret"),
        start(tool="bash", args={"command": "cat /workspace/.env"}),
        end(text="another-unlabelled-secret"),
    ))
    assert len(result["steps"]) == 1
    assert "unlabelled-secret" not in json.dumps(result)
    assert result["invalid_line_count"] == 1


@pytest.mark.parametrize("reason", ["length", "error", "aborted"])
def test_final_response_exposes_only_stop_reason_and_numeric_usage(reason):
    result = parse_pi_events(events(
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse"}},
        {"type": "message_end", "message": {
            "role": "assistant", "stopReason": reason,
            "content": [{"type": "thinking", "thinking": "private-reasoning"}],
            "errorMessage": "credential-secret", "rawStopReason": "private-provider-text",
            "usage": {"input": 17029, "output": 16384, "reasoning": 16384,
                      "cacheRead": True, "cacheWrite": -1, "totalTokens": "secret-token",
                      "cost": {"total": 0.25}, "unexpected": "private-usage"},
        }},
        {"type": "message_end", "message": {"role": "toolResult", "stopReason": "stop"}},
    ))
    assert result["final_response"] == {
        "stop_reason": reason,
        "usage": {"input": 17029, "output": 16384, "reasoning": 16384,
                  "cacheRead": None, "cacheWrite": None, "totalTokens": None},
    }
    encoded = json.dumps(result)
    for private in ("private-reasoning", "credential-secret", "private-provider-text",
                    "secret-token", "private-usage"):
        assert private not in encoded


def test_missing_or_partial_final_response_is_unknown():
    completed = {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
    assert parse_pi_events(events(start(), end()))["final_response"] is None
    assert parse_pi_events(events(completed), truncated=True)["final_response"] is None
    assert parse_pi_events(events(
        completed, {"type": "message_start", "message": {"role": "assistant"}}
    ))["final_response"] is None
    result = parse_pi_events(events({
        "type": "message_end", "message": {"role": "assistant", "stopReason": "unknown-secret"}
    }))
    assert result["final_response"]["stop_reason"] is None
    assert "unknown-secret" not in json.dumps(result)


def write_agent_run(tmp_path, *, finished):
    run = tmp_path / "runs/recorded"
    trial = run / "harbor/job/trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({
        "task_name": "agentic-labs/2280_medium_repair_plan_medium",
        "exception_info": {"exception_type": "AgentFailed"},
    }))
    (run / "manifest.json").write_text(json.dumps({
        "agent": "pi", "task": "2280_medium_repair_plan_medium",
        "state": "failed" if finished else "running",
        "finished_at_utc": "2026-10-20T13:00:00+00:00" if finished else None,
        "harbor_returncode": 1 if finished else None,
    }))
    return trial / "agent/pi.txt"


def test_live_trace_is_not_read_until_runner_finalization(tmp_path):
    trace = write_agent_run(tmp_path, finished=False)
    trace.write_text(events(start(), end(text="not-yet-sanitized-output")))
    detail = run_detail(tmp_path, "recorded")
    assert detail["trace"] == {"status": "waiting", "steps": []}
    assert "not-yet-sanitized-output" not in json.dumps(detail)


def test_finalized_run_reads_only_fixed_pi_trace_and_reports_absence(tmp_path):
    trace = write_agent_run(tmp_path, finished=True)
    assert run_detail(tmp_path, "recorded")["trace"]["status"] == "unavailable"
    trace.write_text(events(
        start(tool="forgeflow_smoke_read", args={}),
        end(tool="forgeflow_smoke_read", text="completed"),
    ))
    detail = run_detail(tmp_path, "recorded")
    assert detail["trace"]["status"] == "available"
    assert detail["trace"]["steps"][0]["tool"] == "forgeflow_smoke_read"
    assert detail["trace"]["evidence"] == "runs/recorded/harbor/job/trial/agent/pi.txt"
    assert detail["strict_success"] is None


def test_short_vendor_prefixed_secrets_are_redacted():
    samples = {
        "openai-short": "sk-" + "a" * 20,
        "openai-project": "sk-proj-" + "a1B2" * 8,
        "github": "ghp_" + "A1b2" * 10,
        "github-pat": "github_pat_" + "A1b2" * 10,
        "gitlab": "glpat-" + "A1b2" * 10,
        "npm": "npm_" + "A1b2" * 10,
        "huggingface": "hf_" + "A1b2" * 10,
        "google": "AIza" + "A1b2" * 8,
        "aws": "AKIA" + "A1B2" * 5,
        "slack": "xoxb-" + "1234567890" * 2,
        "stripe": "sk_live_" + "A1b2" * 8,
        "webhook": "whsec_" + "A1b2" * 8,
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123456",
    }
    for name, secret in samples.items():
        rendered = _safe_excerpt(f"tool output: {secret}")
        assert secret not in rendered, name
        assert "[REDACTED" in rendered, name


def test_ordinary_identifiers_are_not_secret_redacted():
    for value in ("order-12345", "PROJECT_MEMORY.md", "case_384585ef", "2026-10-22T10:00:00Z"):
        assert _safe_excerpt(value) == value
