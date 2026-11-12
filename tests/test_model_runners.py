"""Model runners must report provider usage or say it is unknown."""
from __future__ import annotations

import json

import httpx
import pytest

from forgeflow.superagent import (
    HttpModelRunner,
    ModelRunnerError,
    PiJsonRunner,
    parse_pi_usage,
)


def _client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def test_http_runner_returns_the_provider_usage_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={
            "id": "req-1", "model": "deepseek-flash",
            "choices": [{"message": {"content": '{"status":"completed"}'}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })

    runner = HttpModelRunner(base_url="https://api.example/v1", api_key="secret",
                             model="deepseek-flash", client_factory=_client(handler))
    reply = runner.complete("hello")
    assert reply.text == '{"status":"completed"}'
    assert reply.usage["tokens"] == 18
    assert reply.usage["complete"] is True and reply.usage_complete is True
    assert reply.usage["request_id"] == "req-1"


def test_http_runner_without_a_usage_block_is_marked_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    runner = HttpModelRunner(base_url="https://api.example/v1", api_key="secret",
                             model="m", client_factory=_client(handler))
    reply = runner.complete("hello")
    assert reply.usage_complete is False


@pytest.mark.parametrize(("status", "code"), [
    (401, "model_runner_unauthorized"),
    (429, "model_runner_rate_limited"),
    (500, "model_runner_error"),
])
def test_http_runner_surfaces_provider_failures(status, code) -> None:
    runner = HttpModelRunner(base_url="https://api.example/v1", api_key="secret",
                             model="m",
                             client_factory=_client(lambda request: httpx.Response(status)))
    with pytest.raises(ModelRunnerError, match=code):
        runner.complete("hello")


def test_pi_stream_usage_is_summed_per_message() -> None:
    stream = "\n".join([
        json.dumps({"type": "message_end", "message": {"content": "first"},
                    "usage": {"total_tokens": 100, "inputTokens": 60, "outputTokens": 40}}),
        json.dumps({"type": "message_end", "message": {"content": '{"a":1}'},
                    "usage": {"total_tokens": 25}}),
    ])
    usage = parse_pi_usage(stream)
    assert usage.tokens == 125 and usage.prompt_tokens == 60
    assert usage.complete is True


def test_pi_stream_without_usage_is_explicitly_unknown() -> None:
    stream = json.dumps({"type": "message_end", "message": {"content": "hi"}})
    assert parse_pi_usage(stream).complete is False


def test_pi_json_runner_prefers_the_last_assistant_message() -> None:
    stream = "\n".join([
        json.dumps({"type": "message_end", "message": {"content": "draft"},
                    "usage": {"total_tokens": 5}}),
        json.dumps({"type": "message_end",
                    "message": {"content": [{"type": "text", "text": '{"final":true}'}]},
                    "usage": {"total_tokens": 6}}),
    ])
    reply = PiJsonRunner(lambda prompt: stream).complete("prompt")
    assert reply.text.strip() == '{"final":true}'
    assert reply.usage["tokens"] == 11 and reply.usage_complete is True


def test_pi_docker_runner_asks_for_the_json_stream(monkeypatch) -> None:
    import subprocess

    from forgeflow.superagent import PiDockerRunner

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        payload = json.dumps({"type": "message_end",
                              "message": {"content": '{"status":"completed"}'},
                              "usage": {"total_tokens": 42}})
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = PiDockerRunner(docker_host="unix:///tmp/d.sock", image="forgeflow-pi:test",
                            api_key="secret", timeout=5)
    reply = runner.complete("prompt")
    assert "--mode" in captured["command"] and "json" in captured["command"]
    assert reply.usage["tokens"] == 42 and reply.usage_complete is True
