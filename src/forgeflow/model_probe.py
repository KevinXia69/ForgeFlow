"""A bounded Nebius tool-call smoke test, separate from ERP task evaluation."""

from __future__ import annotations

import json
import math
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, quote_plus

import httpx

from .config import ConfigError, load_config

PROBE_TOKEN = "forgeflow-nebius-tool-probe"
FINAL_MARKER = "FORGEFLOW_PROBE_OK"
TOOL_NAME = "check_probe"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Check a fixed test token without changing any external state.",
            "parameters": {
                "type": "object",
                "properties": {"token": {"type": "string", "enum": [PROBE_TOKEN]}},
                "required": ["token"],
                "additionalProperties": False,
            },
        },
    }
]
SENSITIVE_FIELDS = {
    "authorization",
    "api_key",
    "apikey",
    "nebius_api_key",
    "openai_api_key",
    "access_token",
    "password",
    "secret",
}


class ProbeFailure(Exception):
    """A safe, classified failure whose message never uses provider text."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _redact(value: Any, api_key: str) -> Any:
    if isinstance(value, dict):
        return {
            _redact(str(key), api_key): (
                "[REDACTED]"
                if str(key).casefold() in SENSITIVE_FIELDS
                else _redact(item, api_key)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, api_key) for item in value]
    if isinstance(value, str) and api_key:
        for secret in {api_key, quote(api_key, safe=""), quote_plus(api_key)}:
            value = value.replace(secret, "[REDACTED]")
    return value


def _message(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProbeFailure("invalid_completion", "Completion response is not a JSON object.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProbeFailure("invalid_completion", "Completion response has no valid choice.")
    if choices[0].get("finish_reason") == "length":
        raise ProbeFailure("token_budget_exhausted", "Completion reached its output token budget.")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProbeFailure("invalid_completion", "Completion has no valid assistant message.")
    return message


def _validated_tool_call(message: dict[str, Any]) -> dict[str, Any]:
    calls = message.get("tool_calls")
    if not calls:
        raise ProbeFailure("missing_tool_call", "The model did not request a tool.")
    if not isinstance(calls, list) or len(calls) != 1:
        raise ProbeFailure("invalid_tool_call", "Expected one bounded probe tool call.")
    call = calls[0]
    if (
        not isinstance(call, dict)
        or call.get("type") != "function"
        or not isinstance(call.get("id"), str)
        or not call["id"]
        or not isinstance(call.get("function"), dict)
    ):
        raise ProbeFailure("invalid_tool_call", "The probe tool call has invalid metadata.")
    function = call["function"]
    if function.get("name") != TOOL_NAME:
        raise ProbeFailure("unexpected_tool", "The model requested a tool outside the probe.")
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        raise ProbeFailure("invalid_tool_arguments", "Tool arguments are not a JSON string.")
    try:
        parsed = json.loads(arguments)
    except ValueError:
        raise ProbeFailure("invalid_tool_arguments", "Tool arguments are not valid JSON.") from None
    if parsed != {"token": PROBE_TOKEN}:
        raise ProbeFailure("invalid_tool_arguments", "Tool arguments do not match the probe token.")
    return {
        "id": call["id"],
        "type": "function",
        "function": {"name": TOOL_NAME, "arguments": arguments},
    }


def run_model_probe(
    workspace_root: str | Path,
    output_dir: str | Path,
    *,
    timeout_seconds: float = 60.0,
    max_tokens: int | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Check model listing and one real tool/result/final-response exchange.

    The selected model comes only from ``NEBIUS_MODEL``. This performs at most
    three HTTP requests, with no retries or redirect following. ``transport`` is
    an optional HTTP test transport; ordinary callers should leave it unset.
    Every attempted probe gets a unique, redacted JSON evidence file. A passed
    probe confirms this exchange only, never Pi, Odoo, or benchmark success.
    Output-limit fields are omitted unless an explicit token budget is supplied.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
        raise ValueError("Probe timeout must be greater than 0 and at most 120 seconds.")
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= 8192
    ):
        raise ValueError("Probe max_tokens must be an integer between 1 and 8192.")
    output_limit = {} if max_tokens is None else {"max_tokens": max_tokens}
    root = Path(workspace_root).resolve()
    destination = Path(output_dir)
    if not destination.is_absolute():
        destination = root / destination
    destination.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)
    report_path = (
        destination
        / f"model-probe-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
    ).resolve()
    started_clock = time.monotonic()
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "provider_tool_call_smoke_test",
        "started_at": started_at.isoformat(),
        "status": "failed",
        "config": None,
        "budget": {
            "max_http_requests": 3,
            "max_chat_requests": 2,
            "max_tokens_per_chat_request": max_tokens,
            "max_tokens_source": "provider_default" if max_tokens is None else "explicit",
            "output_limit_request_fields": "omitted" if max_tokens is None else "included",
            "timeout_seconds_per_request": timeout_seconds,
        },
        "model_listed": False,
        "tool_calls_executed": 0,
        "final_marker_received": False,
        "provider_call_performed": False,
        "benchmark_run_performed": False,
        "benchmark_success": None,
        "cost": None,
        "exchanges": [],
        "evidence_path": str(report_path),
    }
    api_key = ""
    stage = "configuration"
    try:
        config = load_config(root, environ=environ, require_model=True)
        api_key = config.api_key
        report["config"] = config.public_dict()

        def request(
            client: httpx.Client, method: str, path: str, payload: dict | None = None
        ) -> Any:
            exchange: dict[str, Any] = {"stage": stage, "method": method, "path": path}
            if payload is not None:
                exchange["request_body"] = payload
            report["exchanges"].append(exchange)
            report["provider_call_performed"] = True
            try:
                response = client.request(
                    method,
                    config.base_url.rstrip("/") + path,
                    **({"json": payload} if payload is not None else {}),
                )
            except httpx.TimeoutException:
                raise ProbeFailure("request_timeout", "Provider request timed out.") from None
            except httpx.RequestError:
                raise ProbeFailure("network_error", "Provider request could not complete.") from None
            exchange["http_status"] = response.status_code
            try:
                body = response.json()
            except ValueError:
                exchange["response_text_preview"] = response.text[:4096]
                if not response.is_success:
                    raise ProbeFailure("http_error", "Provider returned an HTTP error.") from None
                raise ProbeFailure("invalid_json", "Provider response is not valid JSON.") from None
            exchange["response_body"] = body
            if not response.is_success:
                raise ProbeFailure("http_error", "Provider returned an HTTP error.")
            return body

        with httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        ) as client:
            stage = "model_listing"
            listing = request(client, "GET", "/models")
            if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
                raise ProbeFailure("invalid_models_response", "Model listing has no data array.")
            if not any(
                isinstance(model, dict) and model.get("id") == config.model
                for model in listing["data"]
            ):
                raise ProbeFailure("model_not_listed", "Configured model was not listed by Nebius.")
            report["model_listed"] = True
            messages = [
                {
                    "role": "system",
                    "content": (
                        "This is a tool protocol check with no external side effects. "
                        f"Call {TOOL_NAME} exactly once with token {PROBE_TOKEN}. "
                        f"After its successful result, reply exactly {FINAL_MARKER}."
                    ),
                },
                {"role": "user", "content": "Run the tool protocol check now."},
            ]
            stage = "tool_request"
            completion = request(
                client,
                "POST",
                "/chat/completions",
                {
                    "model": config.model,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
                    **output_limit,
                    "stream": False,
                },
            )
            assistant = _message(completion)
            call = _validated_tool_call(assistant)
            tool_result = {"ok": True, "token": PROBE_TOKEN}
            report["tool_calls_executed"] = 1
            report["tool_result"] = tool_result
            assistant_for_request: dict[str, Any] = {
                "role": "assistant",
                "content": assistant.get("content"),
                "tool_calls": [call],
            }
            if isinstance(assistant.get("reasoning_content"), str):
                assistant_for_request["reasoning_content"] = assistant["reasoning_content"]
            # Build a fresh list so the first request's evidence remains unchanged.
            final_messages = [
                *messages,
                assistant_for_request,
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(tool_result, separators=(",", ":")),
                },
            ]
            stage = "final_response"
            final = request(
                client,
                "POST",
                "/chat/completions",
                {
                    "model": config.model,
                    "messages": final_messages,
                    "tools": TOOLS,
                    "tool_choice": "none",
                    **output_limit,
                    "stream": False,
                },
            )
            final_message = _message(final)
            if final_message.get("tool_calls"):
                raise ProbeFailure("unexpected_tool_call", "The final reply requested another tool.")
            content = final_message.get("content")
            if not isinstance(content, str) or content.strip() != FINAL_MARKER:
                raise ProbeFailure(
                    "unexpected_final_response", "The final reply did not contain the expected marker."
                )
            report["final_marker_received"] = True
            report["status"] = "passed"
    except ConfigError as error:
        report["failure"] = {"stage": stage, "category": "configuration", "message": str(error)}
    except ProbeFailure as error:
        report["failure"] = {
            "stage": stage,
            "category": error.category,
            "message": str(error),
        }
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["failure"] = {
            "stage": stage,
            "category": "interrupted",
            "message": "The model probe was interrupted before completion.",
        }
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["elapsed_seconds"] = round(time.monotonic() - started_clock, 3)
        report = _redact(report, api_key)
        with report_path.open("x", encoding="utf-8") as evidence_file:
            json.dump(report, evidence_file, indent=2, ensure_ascii=False)
            evidence_file.write("\n")
    return report
