from __future__ import annotations

import json

import httpx
import pytest

from forgeflow.model_probe import FINAL_MARKER, PROBE_TOKEN, TOOL_NAME, run_model_probe

MODEL = "nvidia/Nemotron-3.5-Super-VL"
API_KEY = "test-nebius-secret"
ENV = {"NEBIUS_API_KEY": API_KEY, "NEBIUS_MODEL": MODEL}


def completion(message, finish_reason="stop"):
    return {"choices": [{"message": {"role": "assistant", **message}, "finish_reason": finish_reason}]}


def tool_completion(**changes):
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": TOOL_NAME, "arguments": json.dumps({"token": PROBE_TOKEN})},
    }
    call.update(changes)
    return completion({"content": None, "tool_calls": [call]}, finish_reason="tool_calls")


def run_with_responses(tmp_path, responses, **kwargs):
    requests = []

    def handler(request):
        requests.append(request)
        result = responses[len(requests) - 1]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    report = run_model_probe(
        tmp_path,
        "evidence",
        environ=kwargs.pop("environ", ENV),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    return report, requests


def test_model_and_tool_exchange_records_real_sequence_without_claiming_erp_success(tmp_path):
    report, requests = run_with_responses(
        tmp_path,
        [
            {"data": [{"id": MODEL}]},
            tool_completion(),
            completion({"content": FINAL_MARKER}),
        ],
    )
    assert report["status"] == "passed"
    assert report["model_listed"] is True
    assert report["tool_calls_executed"] == 1
    assert report["final_marker_received"] is True
    assert report["benchmark_run_performed"] is False
    assert report["benchmark_success"] is None
    assert len(requests) == 3
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/models"
    assert requests[0].headers["Authorization"] == f"Bearer {API_KEY}"
    first_chat = json.loads(requests[1].content)
    final_chat = json.loads(requests[2].content)
    assert first_chat["model"] == MODEL
    for payload in (first_chat, final_chat):
        assert "max_tokens" not in payload
        assert "max_completion_tokens" not in payload
    assert report["budget"]["max_tokens_per_chat_request"] is None
    assert report["budget"]["max_tokens_source"] == "provider_default"
    assert report["budget"]["output_limit_request_fields"] == "omitted"
    assert len(first_chat["messages"]) == 2
    assert len(report["exchanges"][1]["request_body"]["messages"]) == 2
    assert final_chat["messages"][-1]["role"] == "tool"
    assert final_chat["messages"][-1]["tool_call_id"] == "call_1"
    assert json.loads(final_chat["messages"][-1]["content"]) == {
        "ok": True,
        "token": PROBE_TOKEN,
    }
    assert final_chat["tool_choice"] == "none"
    assert "Authorization" not in json.dumps(report)
    evidence = open(report["evidence_path"], encoding="utf-8").read()
    assert API_KEY not in evidence
    assert json.loads(evidence) == report


@pytest.mark.parametrize("max_tokens", [1, 8192])
def test_explicit_output_budget_is_sent_on_both_chat_requests(tmp_path, max_tokens):
    report, requests = run_with_responses(
        tmp_path,
        [{"data": [{"id": MODEL}]}, tool_completion(), completion({"content": FINAL_MARKER})],
        max_tokens=max_tokens,
    )
    assert report["status"] == "passed"
    for request in requests[1:]:
        payload = json.loads(request.content)
        assert payload["max_tokens"] == max_tokens
        assert "max_completion_tokens" not in payload
    assert report["budget"]["max_tokens_per_chat_request"] == max_tokens
    assert report["budget"]["max_tokens_source"] == "explicit"
    assert report["budget"]["output_limit_request_fields"] == "included"


@pytest.mark.parametrize("max_tokens", [True, 0, -1, 8193, 2048.0, "2048"])
def test_invalid_explicit_budget_stops_before_any_provider_request(tmp_path, max_tokens):
    def unexpected_request(request):
        pytest.fail("Invalid output budgets must not make provider requests.")

    with pytest.raises(ValueError, match="integer between 1 and 8192"):
        run_model_probe(
            tmp_path, "evidence", max_tokens=max_tokens, environ=ENV,
            transport=httpx.MockTransport(unexpected_request),
        )
    assert not (tmp_path / "evidence").exists()


def test_unlisted_model_stops_before_billable_chat(tmp_path):
    report, requests = run_with_responses(tmp_path, [{"data": [{"id": "another-model"}]}])
    assert report["failure"]["category"] == "model_not_listed"
    assert report["tool_calls_executed"] == 0
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("response", "category"),
    [
        ({"choices": []}, "invalid_completion"),
        (completion({"content": FINAL_MARKER}), "missing_tool_call"),
        (
            tool_completion(function={"name": "delete_records", "arguments": "{}"}),
            "unexpected_tool",
        ),
        (
            tool_completion(function={"name": TOOL_NAME, "arguments": "not-json"}),
            "invalid_tool_arguments",
        ),
        (
            tool_completion(function={"name": TOOL_NAME, "arguments": '{"token":"wrong"}'}),
            "invalid_tool_arguments",
        ),
        (
            tool_completion(function={"name": TOOL_NAME, "arguments": {"token": PROBE_TOKEN}}),
            "invalid_tool_arguments",
        ),
        (tool_completion(id=""), "invalid_tool_call"),
        (completion({"content": ""}, finish_reason="length"), "token_budget_exhausted"),
    ],
)
def test_classifies_invalid_or_missing_tool_calls_and_does_not_execute_them(
    tmp_path, response, category
):
    report, requests = run_with_responses(tmp_path, [{"data": [{"id": MODEL}]}, response])
    assert report["status"] == "failed"
    assert report["failure"]["category"] == category
    assert report["failure"]["stage"] == "tool_request"
    assert report["tool_calls_executed"] == 0
    assert len(requests) == 2


def test_preserves_executed_tool_if_final_response_fails(tmp_path):
    report, _ = run_with_responses(
        tmp_path,
        [{"data": [{"id": MODEL}]}, tool_completion(), completion({"content": ""})],
    )
    assert report["status"] == "failed"
    assert report["tool_calls_executed"] == 1
    assert report["failure"]["stage"] == "final_response"
    assert report["failure"]["category"] == "unexpected_final_response"


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (httpx.ReadTimeout(f"private error {API_KEY}"), "request_timeout"),
        (httpx.ConnectError(f"private error {API_KEY}"), "network_error"),
        (
            httpx.Response(
                401,
                json={"error": {"message": f"rejected {API_KEY}", "api_key": "another-secret"}},
            ),
            "http_error",
        ),
        (httpx.Response(200, text=f"not-json {API_KEY}"), "invalid_json"),
    ],
)
def test_provider_failures_are_recorded_without_credentials(tmp_path, failure, category):
    report, requests = run_with_responses(tmp_path, [failure])
    assert report["status"] == "failed"
    assert report["failure"]["category"] == category
    assert len(requests) == 1
    evidence = open(report["evidence_path"], encoding="utf-8").read()
    assert API_KEY not in evidence
    assert "another-secret" not in evidence
    assert "private error" not in evidence


def test_does_not_follow_redirect_or_send_secret_to_a_different_host(tmp_path):
    report, requests = run_with_responses(
        tmp_path,
        [httpx.Response(302, headers={"Location": "https://other.example/models"})],
    )
    assert report["failure"]["category"] == "http_error"
    assert len(requests) == 1


def test_missing_nebius_key_writes_failure_without_using_another_provider_key(tmp_path):
    report, requests = run_with_responses(
        tmp_path,
        [],
        environ={"OPENAI_API_KEY": "unrelated-provider-secret", "NEBIUS_MODEL": MODEL},
    )
    assert report["failure"]["category"] == "configuration"
    assert report["provider_call_performed"] is False
    assert requests == []
    assert "unrelated-provider-secret" not in json.dumps(report)


def test_success_response_reflecting_key_is_redacted_in_report_and_file(tmp_path):
    first_response = tool_completion()
    first_response["choices"][0]["message"]["reasoning_content"] = f"hidden {API_KEY}"
    report, _ = run_with_responses(
        tmp_path,
        [{"data": [{"id": MODEL}]}, first_response, completion({"content": FINAL_MARKER})],
    )
    assert report["status"] == "passed"
    assert API_KEY not in json.dumps(report)
    assert "[REDACTED]" in json.dumps(report)
    assert API_KEY not in open(report["evidence_path"], encoding="utf-8").read()


def test_repeated_probes_preserve_separate_evidence_files(tmp_path):
    first, _ = run_with_responses(tmp_path, [{"data": []}])
    second, _ = run_with_responses(tmp_path, [{"data": []}])
    assert first["evidence_path"] != second["evidence_path"]
    assert len(list((tmp_path / "evidence").glob("*.json"))) == 2


def test_interrupted_probe_saves_redacted_partial_evidence_and_reraises(tmp_path):
    responses = [
        {"data": [{"id": MODEL}]},
        tool_completion(),
    ]

    def handler(request):
        if responses:
            return httpx.Response(200, json=responses.pop(0))
        raise KeyboardInterrupt(f"private interrupt detail {API_KEY}")

    with pytest.raises(KeyboardInterrupt):
        run_model_probe(
            tmp_path,
            "interrupted-evidence",
            environ=ENV,
            transport=httpx.MockTransport(handler),
        )
    files = list((tmp_path / "interrupted-evidence").glob("*.json"))
    assert len(files) == 1
    evidence_text = files[0].read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)
    assert evidence["status"] == "interrupted"
    assert evidence["failure"]["category"] == "interrupted"
    assert evidence["failure"]["stage"] == "final_response"
    assert evidence["tool_calls_executed"] == 1
    assert evidence["final_marker_received"] is False
    assert evidence["benchmark_run_performed"] is False
    assert len(evidence["exchanges"]) == 3
    assert API_KEY not in evidence_text
    assert "private interrupt detail" not in evidence_text
