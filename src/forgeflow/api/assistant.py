"""Optional OpenAI-compatible answer generation over retrieved passages.

Generation is local-only: the caller supplies a saved :class:`ModelSettings`
and the module never runs on the hosted review deployment. Every failure is
mapped to a short, stable code that never includes the API key or the raw
provider response.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .model_settings import ModelSettings, ModelSettingsStore

MAX_PASSAGES = 5
MAX_PASSAGE_CHARS = 1500
MAX_ANSWER_CHARS = 8000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SYSTEM_PROMPT = (
    "You are ForgeFlow's plain-spoken project assistant. Assume the reader "
    "knows nothing about the case or the project. Start with one sentence that "
    "says what happened or answers the question in everyday words. Then use "
    "short paragraphs or lists that address every part of the question, retain "
    "the necessary evidence-backed numbers and document identifiers, and "
    "explain any technical term you must use. Use only the supplied evidence "
    "passages and cite exact lines as `path:start-end`. Clearly separate what "
    "is planned, applied, verified and unknown. Never open with an ID, hash, "
    "field name, heading tree or table. Do not use stock AI phrases such as "
    "'firstly', 'moreover', 'in conclusion', 'it is worth noting', 'if you need "
    "further assistance' or 'as an AI'. If evidence is missing, say so plainly. "
    "Ask a follow-up only when necessary evidence cannot resolve the question."
)


class AssistantError(Exception):
    """A stable model-generation failure code with a short, safe reason."""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(reason or code)
        self.code = code
        self.reason = reason


def _citation(passage: Any) -> str:
    if not isinstance(passage, dict):
        return ""
    path = str(passage.get("path", ""))
    start = passage.get("start_line")
    end = passage.get("end_line")
    if path and isinstance(start, int) and isinstance(end, int):
        return f"{path}:{start}-{end}"
    return path


def _user_content(question: str, passages: Sequence[Any]) -> str:
    lines = [f"Question: {question}", "", "Evidence passages:"]
    selected = list(passages)[:MAX_PASSAGES]
    if not selected:
        lines.append("(none supplied)")
    for number, passage in enumerate(selected, 1):
        if not isinstance(passage, dict):
            continue
        text = passage.get("text", "")
        text = text if isinstance(text, str) else str(text)
        lines.append(f"[{number}] {_citation(passage)}")
        lines.append(text[:MAX_PASSAGE_CHARS])
    return "\n".join(lines)


def _post(settings: ModelSettings, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = settings.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.api_key}",
    }
    try:
        request = urllib.request.Request(
            url, data=data, headers=headers, method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if callable(getcode) else 200
            if status != 200:
                raise AssistantError("model_http_error", f"HTTP {status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except AssistantError:
        raise
    except urllib.error.HTTPError as error:
        raise AssistantError("model_http_error", f"HTTP {error.code}") from None
    except (TimeoutError, socket.timeout):
        raise AssistantError("model_timeout", "request timed out") from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise AssistantError("model_timeout", "request timed out") from None
        raise AssistantError("model_unavailable", "connection failed") from None
    except (OSError, ValueError):
        raise AssistantError("model_unavailable", "connection failed") from None
    if not isinstance(raw, bytes):
        raise AssistantError("model_invalid_response", "invalid response")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AssistantError("model_invalid_response", "response too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AssistantError("model_invalid_response", "invalid JSON") from None
    if not isinstance(payload, dict):
        raise AssistantError("model_invalid_response", "invalid JSON")
    return payload


def _retrieval_query(question: str, history: Sequence[dict[str, str]] | None) -> str:
    """Use the previous user turn only to resolve follow-up references."""
    if not history:
        return question
    previous = next(
        (turn["content"] for turn in reversed(history) if turn.get("role") == "user"),
        "",
    )
    previous = previous.strip()[:200]
    return f"{question} {previous}" if previous else question


def generate(
    question: str,
    passages: Sequence[Any],
    settings: ModelSettings,
    timeout: float = 30.0,
    history: Sequence[dict[str, str]] | None = None,
    locale: str = "zh",
) -> str:
    """Generate an answer from numbered passages via an OpenAI-compatible API."""
    if not settings.api_key:
        raise AssistantError("not_configured", "no API key")
    system = SYSTEM_PROMPT
    if locale == "en":
        system += " Answer in English only; never quote Chinese passages as English evidence."
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in history or []:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": _user_content(question, passages)})
    body = {"model": settings.model, "messages": messages, "temperature": 0.2}
    payload = _post(settings, body, timeout)
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AssistantError("model_invalid_response", "missing content") from None
    if not isinstance(content, str) or not content.strip():
        raise AssistantError("model_invalid_response", "empty content")
    return content.strip()


def answer_with_settings(
    question: str,
    root: str | Path,
    model: ModelSettings | None,
    limit: int = 5,
    history: Sequence[dict[str, str]] | None = None,
    locale: str = "zh",
) -> dict[str, Any]:
    """Return cited retrieval, optionally replaced by a grounded model answer."""
    from ..knowledge.answer import answer as retrieve

    retrieval = retrieve(
        _retrieval_query(question, history), root, limit=limit, locale=locale,
    )
    if model is None or not model.api_key:
        return {**retrieval, "mode": "retrieval"}
    try:
        text = generate(
            question, retrieval.get("passages", []), model,
            history=history, locale=locale,
        )
        if not isinstance(text, str) or not text.strip():
            raise AssistantError("model_invalid_response", "empty content")
    except AssistantError as error:
        return {**retrieval, "mode": "retrieval", "warning": error.code}
    return {
        **retrieval,
        "mode": "model",
        "answer": text.strip()[:MAX_ANSWER_CHARS],
    }


def answer_with_model(
    question: str,
    root: str | Path,
    store: ModelSettingsStore,
    limit: int = 5,
) -> dict[str, Any]:
    """Compatibility wrapper that reads the server-side store."""
    return answer_with_settings(question, root, store.load(), limit=limit)
