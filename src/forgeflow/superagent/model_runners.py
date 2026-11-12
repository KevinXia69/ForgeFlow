"""Model runners that report supplier usage honestly.

A worker's token budget is only meaningful if usage comes from the provider.  Two
runners are provided:

* :class:`HttpModelRunner` reads ``usage.total_tokens`` from an OpenAI-compatible
  response -- a real receipt.
* :class:`PiJsonRunner` parses Pi's machine-readable event stream, which carries
  per-message usage; when the stream has no usage record the reply is marked
  ``usage_complete=False`` instead of being estimated.

:class:`~forgeflow.superagent.workers.CallableModelRunner` remains the seam for
tests; it reports estimated usage and says so.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .workers import ModelReply


class ModelRunnerError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class RunnerUsage:
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None
    provider: str | None = None
    request_id: str | None = None
    complete: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tokens": self.tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "complete": self.complete,
        }
        for key in ("model", "provider", "request_id"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        payload.update(self.extra)
        return payload


class HttpModelRunner:
    """OpenAI-compatible chat completion runner with a real usage receipt."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 120.0,
                 provider: str = "openai-compatible", temperature: float = 0.2,
                 client_factory: Callable[..., httpx.Client] | None = None) -> None:
        if not base_url or not api_key or not model:
            raise ValueError("model_runner_configuration_required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        self.provider = provider
        self.temperature = float(temperature)
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=self.timeout, trust_env=False))

    @classmethod
    def from_env(cls, environ=None):
        """Build the runner from deployment configuration, or return None.

        The endpoint, key and model are deployment-owned; no request parameter
        can choose them.  Returning None keeps the heuristic router in place
        instead of silently calling a model with missing credentials.
        """
        env = os.environ if environ is None else environ
        base = env.get("FORGEFLOW_MODEL_BASE_URL") or env.get("DEEPSEEK_BASE_URL")
        key = env.get("FORGEFLOW_MODEL_API_KEY") or env.get("DEEPSEEK_API_KEY")
        model = env.get("FORGEFLOW_MODEL") or env.get("DEEPSEEK_MODEL")
        if not (base and key and model):
            return None
        return cls(base_url=str(base), api_key=str(key), model=str(model))

    def complete(self, prompt: str, *, run_token: str | None = None) -> ModelReply:
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        if run_token:
            headers["X-ForgeFlow-Run-Token"] = str(run_token)[:64]
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        client = self._client_factory()
        try:
            response = client.post(f"{self.base_url}/chat/completions", headers=headers,
                                   json=body)
        except httpx.HTTPError:
            raise ModelRunnerError("model_runner_unavailable") from None
        finally:
            client.close()
        if response.status_code in (401, 403):
            raise ModelRunnerError("model_runner_unauthorized")
        if response.status_code == 429:
            raise ModelRunnerError("model_runner_rate_limited")
        if response.status_code != 200:
            raise ModelRunnerError("model_runner_error", str(response.status_code))
        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelRunnerError("model_runner_invalid_response") from None
        usage = payload.get("usage") or {}
        receipt = RunnerUsage(
            tokens=int(usage.get("total_tokens") or 0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=str(payload.get("model") or self.model),
            provider=self.provider,
            request_id=str(payload.get("id") or "") or None,
            complete=bool(usage.get("total_tokens")),
        )
        return ModelReply(str(text), receipt.as_dict(), receipt.complete)


def parse_pi_usage(stream: str) -> RunnerUsage:
    """Sum per-message usage from Pi's ``--mode json`` event stream.

    Pi emits one JSON object per line.  Usage lives on the ``message_end`` (or
    assistant-message) events.  Anything unparsable is skipped, and the absence of
    any usage record leaves ``complete`` false.
    """
    usage = RunnerUsage()
    found = False
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type", ""))
        if kind not in {"message_end", "assistant", "message"}:
            continue
        record = event.get("usage") or (event.get("message") or {}).get("usage") or {}
        if not isinstance(record, dict):
            continue
        total = record.get("total_tokens", record.get("totalTokens"))
        prompt = record.get("prompt_tokens", record.get("inputTokens", 0))
        completion = record.get("completion_tokens", record.get("outputTokens", 0))
        if total is None:
            continue
        usage.tokens += int(total or 0)
        usage.prompt_tokens += int(prompt or 0)
        usage.completion_tokens += int(completion or 0)
        found = True
    usage.complete = found
    return usage


class PiJsonRunner:
    """Runs Pi and returns its final message plus a parsed usage receipt."""

    def __init__(self, run: Callable[[str], str], *, provider: str = "pi") -> None:
        self._run = run
        self.provider = provider

    def complete(self, prompt: str, *, run_token: str | None = None) -> ModelReply:
        del run_token
        try:
            stream = self._run(prompt)
        except Exception as error:  # noqa: BLE001 - surfaced as a typed runner error
            raise ModelRunnerError("model_runner_failed", type(error).__name__) from None
        usage = parse_pi_usage(stream)
        usage.provider = self.provider
        return ModelReply(_final_text(stream), usage.as_dict(), usage.complete)


def _final_text(stream: str) -> str:
    """Prefer the last assistant message; fall back to the raw stream."""
    candidate = ""
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if str(event.get("type", "")) not in {"message_end", "assistant", "message"}:
            continue
        message = event.get("message")
        text = ""
        if isinstance(message, dict):
            text = _content_text(message.get("content"))
        elif event.get("content"):
            text = _content_text(event.get("content"))
        if text.strip():
            candidate = text
    return candidate or stream


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


class FallbackModelRunner:
    """Try a primary runner, then a secondary one, reporting which was used."""

    def __init__(self, primary, secondary, *, secondary_name: str = "fallback") -> None:
        self.primary = primary
        self.secondary = secondary
        self.secondary_name = secondary_name

    def complete(self, prompt: str, *, run_token: str | None = None) -> ModelReply:
        try:
            return self.primary.complete(prompt, run_token=run_token)
        except ModelRunnerError as error:
            if error.code in {"model_runner_unauthorized", "model_runner_invalid_response"}:
                raise
            reply = self.secondary.complete(prompt, run_token=run_token)
            usage = dict(reply.usage or {})
            usage["failover_from"] = error.code
            return ModelReply(reply.text, usage, reply.usage_complete)
