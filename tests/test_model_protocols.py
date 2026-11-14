"""Wire-level protocol fixtures: wrong endpoints, replay and secrets fail closed."""

from __future__ import annotations

import json

import httpx
import pytest

from forgeflow.api.model_settings import (
    ModelSettingsError,
    assistant_model,
    build_settings,
    platform_settings,
)
from forgeflow.local_workbench.providers import connection_environment, normalize_connection
from forgeflow.model_transport import ModelTransport, TransportError
from forgeflow.superagent.model_runners import HttpModelRunner


@pytest.mark.parametrize(
    "api,suffix,header,payload",
    [
        (
            "openai-completions",
            "/v1/chat/completions",
            "authorization",
            {
                "choices": [{"message": {"content": "complete"}}],
                "usage": {"total_tokens": 12, "prompt_tokens": 8, "completion_tokens": 4},
            },
        ),
        (
            "openai-responses",
            "/v1/responses",
            "authorization",
            {
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "complete"}]}
                ],
                "usage": {"total_tokens": 12, "input_tokens": 8, "output_tokens": 4},
            },
        ),
        (
            "anthropic-messages",
            "/v1/messages",
            "x-api-key",
            {
                "content": [{"type": "text", "text": "complete"}],
                "usage": {"input_tokens": 6, "output_tokens": 4, "cache_read_input_tokens": 2},
            },
        ),
        (
            "google-generative-ai",
            "/v1/models/model:generateContent",
            "x-goog-api-key",
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "hidden", "thought": True}, {"text": "complete"}]
                        }
                    }
                ],
                "usageMetadata": {
                    "totalTokenCount": 12,
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 3,
                    "thoughtsTokenCount": 1,
                },
            },
        ),
    ],
)
def test_provider_wire_contract_and_usage(api, suffix, header, payload):
    def handle(request):
        assert request.url.path == suffix
        assert request.headers[header].endswith("synthetic-credential")
        assert "synthetic-credential" not in str(request.url)
        body = json.loads(request.content)
        if api == "anthropic-messages":
            assert body["system"] == "system" and body["max_tokens"] > 0
        if api == "google-generative-ai":
            assert body["contents"][0]["parts"] == [{"text": "hello"}]
        return httpx.Response(200, json=payload)

    transport = ModelTransport(
        base_url="https://provider.invalid/v1",
        api_key="synthetic-credential",
        model="model",
        api=api,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)),
    )
    result = transport.complete(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]
    )
    assert result.text == "complete"
    assert result.usage["tokens"] == 12 and result.usage["completion_tokens"] == 4
    assert result.usage["complete"] is True


@pytest.mark.parametrize(
    "api", ["openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"]
)
def test_connection_probe_replays_native_tool_result(api):
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            if api == "openai-completions":
                payload = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": "opaque-replay",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "forgeflow_connection_check",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            elif api == "openai-responses":
                payload = {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "forgeflow_connection_check",
                            "arguments": "{}",
                        }
                    ]
                }
            elif api == "anthropic-messages":
                payload = {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "forgeflow_connection_check",
                            "input": {},
                        }
                    ]
                }
            else:
                payload = {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "forgeflow_connection_check",
                                            "args": {},
                                        },
                                        "thoughtSignature": "opaque-signature",
                                    }
                                ],
                            }
                        }
                    ]
                }
        elif api == "openai-completions":
            assert body["messages"][-2]["reasoning_content"] == "opaque-replay"
            payload = {"choices": [{"message": {"content": body["messages"][-1]["content"]}}]}
        elif api == "openai-responses":
            assert body["input"][-1]["call_id"] == "call-1"
            payload = {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": body["input"][-1]["output"]}],
                    }
                ]
            }
        elif api == "anthropic-messages":
            tool = body["messages"][-1]["content"][0]
            assert tool["tool_use_id"] == "call-1"
            payload = {"content": [{"type": "text", "text": tool["content"]}]}
        else:
            assert body["contents"][-2]["parts"][0]["thoughtSignature"] == "opaque-signature"
            payload = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": body["contents"][-1]["parts"][0]["functionResponse"][
                                        "response"
                                    ]["nonce"]
                                }
                            ]
                        }
                    }
                ]
            }
        return httpx.Response(200, json=payload)

    ModelTransport(
        base_url="https://provider.invalid/v1",
        api_key="synthetic-credential",
        model="model",
        api=api,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle)),
    ).probe_tools()
    assert len(seen) == 2


def test_redirect_is_never_followed_or_provider_error_echoed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "https://untrusted.invalid"}, text="synthetic-credential"
        )

    transport = ModelTransport(
        base_url="https://provider.invalid",
        api_key="synthetic-credential",
        model="m",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ),
    )
    with pytest.raises(TransportError, match="^model_runner_error$"):
        transport.complete([{"role": "user", "content": "test"}])
    assert len(requests) == 1


def test_protocol_usage_does_not_invent_absent_counts():
    transport = ModelTransport(
        base_url="https://provider.invalid",
        api_key="synthetic-credential",
        model="m",
        api="anthropic-messages",
    )
    result = transport.parse(
        {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3}}
    )
    assert result.usage["complete"] is False
    assert result.usage["tokens"] == 0


def test_local_model_binding_controls_chat_and_workers_without_deepseek_fallback():
    connection = normalize_connection({"preset": "google"})
    env = connection_environment(connection, "synthetic-credential")
    env["DEEPSEEK_API_KEY"] = "independent-search-credential"
    chat = assistant_model(env)
    platform = platform_settings(env)
    worker = HttpModelRunner.from_env(env)
    assert chat["provider"] == platform.provider == worker.provider == "google"
    assert chat["api"] == platform.api == worker.api == "google-generative-ai"
    assert platform.api_key == worker.api_key == "synthetic-credential"
    assert chat["api_key_env"] == "FORGEFLOW_CHAT_API_KEY"
    assert chat["catalog_native"] is True


def test_unknown_native_protocol_is_rejected():
    with pytest.raises(ModelSettingsError, match="invalid_model_protocol"):
        build_settings(
            "custom",
            "https://provider.invalid",
            "m",
            "synthetic-credential",
            api="google-generative-ai",
            provider="unknown",
        )


def test_deepseek_worker_uses_max_without_affecting_other_providers():
    deepseek = ModelTransport(
        base_url="https://api.deepseek.com/v1",
        api_key="synthetic-credential",
        model="deepseek-flash",
        provider="deepseek",
    )
    assert deepseek.body([{"role": "user", "content": "hello"}])["reasoning_effort"] == "max"
    assert deepseek.body([{"role": "user", "content": "hello"}], probe=True)["thinking"] == {
        "type": "disabled"
    }
    custom = ModelTransport(
        base_url="https://provider.invalid", api_key="synthetic-credential", model="m"
    )
    assert "reasoning_effort" not in custom.body([{"role": "user", "content": "hello"}])
