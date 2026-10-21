"""Small, conservative projection of Pi's recorded JSONL tool events."""

from __future__ import annotations

import json
import re
from typing import Any

MAX_TRACE_BYTES = 8 * 1024 * 1024
MAX_TOOL_STEPS = 200
MAX_SUMMARY_CHARS = 220
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}")
_STOP_REASONS = {"pending", "stop", "length", "toolUse", "error", "aborted", "deferred"}
_USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens")

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
_SECRET_FORMATS = re.compile(
    r"""(?ix)
    (
        sk-(?:proj-)?[A-Za-z0-9_-]{16,}
      | github_pat_[A-Za-z0-9_]{20,}
      | gh[pousr]_[A-Za-z0-9]{20,}
      | glpat-[A-Za-z0-9_-]{20,}
      | npm_[A-Za-z0-9]{20,}
      | hf_[A-Za-z0-9]{20,}
      | AIza[0-9A-Za-z_-]{30,}
      | (?:AKIA|ASIA)[A-Z0-9]{12,}
      | LTAI[A-Za-z0-9]{12,}
      | xox[abprs]-[A-Za-z0-9-]{10,}
      | (?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}
      | whsec_[A-Za-z0-9]{10,}
      | eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}
    )
    """
)

_SENSITIVE_INPUT = re.compile(
    r"(?i)(?:\.env\b|auth\.json|models\.json|credentials|/run/secrets|"
    r"api[_-]?key|authorization|password|passwd|private[_ -]?key|"
    r"access[_-]?token|refresh[_-]?token|xmlrpc|authenticate\s*\()"
)


def _safe_excerpt(value: str) -> str:
    # Redact before truncation, including quoted JSON values and shell variables.
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.DOTALL,
    )
    value = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>,;]+", "[REDACTED AUTH]", value)
    value = re.sub(
        r"""(?ix)
        (["']?(?:
            [A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET)
            |api[-_]?key|authorization|password|passwd|secret|token
        )["']?\s*[:=]\s*)
        (?:"[^"]*"|'[^']*'|[^\s,;}\]]+)
        """,
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@", r"\1[REDACTED]@", value)
    value = _SECRET_FORMATS.sub("[REDACTED SECRET]", value)

    def long_value(match: re.Match[str]) -> str:
        token = match.group(0)
        # UUIDs and Git commit hashes are identifiers, not credentials.
        if _UUID.fullmatch(token) or _COMMIT.fullmatch(token):
            return token
        return "[REDACTED LONG VALUE]"

    value = re.sub(r"\b[A-Za-z0-9_./+=%~-]{32,}\b", long_value, value)
    value = " ".join(value.split())
    return value[:MAX_SUMMARY_CHARS] + ("…" if len(value) > MAX_SUMMARY_CHARS else "")


def _argument_summary(tool_name: str, args: object) -> tuple[str, bool]:
    if not isinstance(args, dict):
        return "参数未记录", False
    if _SENSITIVE_INPUT.search(json.dumps(args, ensure_ascii=False)):
        return "涉及凭证或认证配置的参数已隐藏", True
    if tool_name == "bash" and isinstance(args.get("command"), str):
        return "命令：" + _safe_excerpt(args["command"]), False
    if tool_name in ("read", "write", "edit", "ls", "find", "grep") and isinstance(args.get("path"), str):
        return "路径：" + _safe_excerpt(args["path"]), False
    names = [name for name in args if isinstance(name, str) and _NAME.fullmatch(name)]
    return ("参数：" + "、".join(names[:8])) if names else "无参数", False


def _result_summary(result: object, hide: bool) -> str:
    if hide:
        return "该操作涉及凭证或认证配置，结果摘要已隐藏"
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return "未记录可展示的文本结果"
    text_parts = [
        part["text"] for part in result["content"]
        if isinstance(part, dict) and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]
    if not text_parts:
        return "工具返回无文本内容"
    return _safe_excerpt("\n".join(text_parts)) or "工具返回空文本"


def _response_metadata(message: dict[str, Any]) -> dict[str, Any]:
    reason = message.get("stopReason")
    usage = message.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {
        "stop_reason": reason if isinstance(reason, str) and reason in _STOP_REASONS else None,
        "usage": {
            name: value if type(value := usage.get(name)) is int and 0 <= value <= 2**53 - 1 else None
            for name in _USAGE_FIELDS
        },
    }


def parse_pi_events(text: str, *, truncated: bool = False) -> dict[str, Any]:
    """Match actual Pi 0.85.1 start/end events; do not infer timings or actions.

    Only the final assistant response's stop reason and numeric usage are retained;
    message content, thinking, error text and streaming tool output are omitted.
    The source version has no timestamp on tool events, so individual tool
    duration remains unknown even when surrounding messages have timestamps.
    """
    steps: list[dict[str, Any]] = []
    indexed: dict[str, dict[str, Any]] = {}
    hidden_results: set[str] = set()
    invalid_lines = 0
    source_truncated = truncated
    final_response = None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            invalid_lines += 1
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            if event.get("type") == "message_start":
                # A later response without its end must not inherit a prior stop.
                final_response = None
            elif event.get("type") == "message_end":
                final_response = _response_metadata(message)
        if event.get("type") not in (
            "tool_execution_start", "tool_execution_end"
        ):
            continue
        call_id, tool_name = event.get("toolCallId"), event.get("toolName")
        if (
            not isinstance(call_id, str) or not 0 < len(call_id) <= 200
            or not isinstance(tool_name, str) or not _NAME.fullmatch(tool_name)
        ):
            invalid_lines += 1
            continue
        if call_id not in indexed:
            if len(steps) == MAX_TOOL_STEPS:
                truncated = True
                continue
            step = {
                "sequence": len(steps) + 1,
                "tool": tool_name,
                "status": "incomplete",
                "start_recorded": False,
                "duration_seconds": None,
                "argument_summary": "未记录开始事件",
                "result_summary": None,
            }
            indexed[call_id] = step
            steps.append(step)
        step = indexed[call_id]
        if step["tool"] != tool_name:
            invalid_lines += 1
            continue
        if event["type"] == "tool_execution_start":
            if step["start_recorded"]:
                invalid_lines += 1
                continue
            step["start_recorded"] = True
            summary, hide = _argument_summary(tool_name, event.get("args"))
            step["argument_summary"] = summary
            if hide:
                hidden_results.add(call_id)
                if step["result_summary"] is not None:
                    step["result_summary"] = "该操作涉及凭证或认证配置，结果摘要已隐藏"
        else:
            if step["status"] != "incomplete":
                invalid_lines += 1
                continue
            step["status"] = (
                "failed" if event.get("isError") is True
                else "succeeded" if event.get("isError") is False
                else "unknown"
            )
            step["result_summary"] = (
                _result_summary(event.get("result"), call_id in hidden_results)
                if step["start_recorded"] else "缺少开始事件，结果摘要已隐藏"
            )
    return {
        "status": "available" if steps else "no_tool_events",
        "steps": steps,
        "truncated": truncated,
        "invalid_line_count": invalid_lines,
        "timing_note": "固定 Pi 版本的工具事件没有逐工具时间戳，耗时未记录。",
        "final_response": None if source_truncated else final_response,
    }
