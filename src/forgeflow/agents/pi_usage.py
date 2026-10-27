"""Account for the pinned Pi JSON event stream without replaying a model run.

The stream is the only accounting source: session-file compaction entries and
message updates repeat the same usage and must not be added a second time.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

ACCOUNTING_VERSION = "pi-events-v1"
TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite")
ISSUES = {
    "log_missing", "log_unreadable", "malformed_event", "no_usage_events",
    "message_usage_missing", "compaction_usage_missing", "invalid_token_count",
    "token_total_mismatch", "message_interrupted", "compaction_interrupted",
    "message_unfinished", "compaction_unfinished",
    "summarization_retry_usage_missing", "zero_usage_unconfirmed",
}


def accounting_view(value: Any) -> dict[str, Any]:
    """Bound metadata exposed by summaries; old results remain unverified."""
    if not isinstance(value, dict) or value.get("version") != ACCOUNTING_VERSION:
        return {"version": None, "status": "unknown", "issues": [],
                "cost_status": "unknown"}
    status = value.get("status")
    cost_status = value.get("cost_status")
    result = {
        "version": ACCOUNTING_VERSION,
        "status": status if isinstance(status, str)
        and status in {"complete", "incomplete"} else "unknown",
        "cost_status": cost_status if isinstance(cost_status, str) and cost_status in {
            "complete", "incomplete", "not_reported"
        } else "unknown",
        "issues": sorted(set(value.get("issues", [])) & ISSUES)
        if isinstance(value.get("issues"), list)
        and all(isinstance(issue, str) for issue in value["issues"]) else [],
    }
    for key in ("message_events", "compaction_events", "usage_events",
                "cache_write_tokens"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            result[key] = number
    return result


def collect_pi_usage(lines: Iterable[str]) -> dict[str, Any]:
    """Sum observed counters and label missing/interrupted coverage explicitly.

    Pi's input, cacheRead and cacheWrite are disjoint input categories. Output
    already contains reasoning tokens; totalTokens is a check, never an addend.
    Missing counters leave a recorded subtotal, not an asserted complete total.
    """
    totals = dict.fromkeys(TOKEN_FIELDS, 0)
    observed: set[str] = set()
    issues: set[str] = set()
    messages = compactions = usages = costs = 0
    pending_messages = pending_compactions = 0
    total_cost = 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            # Pi stdout may also contain ordinary process diagnostics.
            if line.startswith(("{", "[")):
                issues.add("malformed_event")
            continue
        if not isinstance(event, dict):
            issues.add("malformed_event")
            continue
        kind = event.get("type")
        if kind in {"summarization_retry_scheduled", "summarization_retry_attempt_start",
                    "summarization_retry_finished"}:
            # The pinned retry loop returns only the final attempt's usage.
            # Earlier attempts may consume tokens but these events carry no
            # counters; retain the observed subtotal without guessing them.
            issues.add("summarization_retry_usage_missing")
            continue
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                pending_messages += 1
            continue
        if kind == "compaction_start":
            pending_compactions += 1
            continue
        if kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                issues.add("malformed_event")
                continue
            if message.get("role") != "assistant":
                continue
            messages += 1
            pending_messages = max(0, pending_messages - 1)
            if message.get("stopReason") in ("aborted", "error"):
                issues.add("message_interrupted")
            usage = message.get("usage")
            missing = "message_usage_missing"
        elif kind == "compaction_end":
            compactions += 1
            pending_compactions = max(0, pending_compactions - 1)
            result = event.get("result")
            if event.get("aborted") is not False or event.get("errorMessage"):
                issues.add("compaction_interrupted")
            usage = result.get("usage") if isinstance(result, dict) else None
            missing = "compaction_usage_missing"
        else:
            continue
        if not isinstance(usage, dict):
            issues.add(missing)
            continue
        usages += 1
        valid = {}
        for field in TOKEN_FIELDS:
            count = usage.get(field)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                totals[field] += count
                valid[field] = count
                observed.add(field)
            else:
                issues.add("invalid_token_count")
        if len(valid) == len(TOKEN_FIELDS) and not any(valid.values()):
            # Pi initializes all counters to zero before any provider usage
            # arrives. An all-zero success cannot prove usage was reported.
            issues.add("zero_usage_unconfirmed")
        if "totalTokens" in usage:
            count = usage["totalTokens"]
            if (not isinstance(count, int) or isinstance(count, bool) or count < 0
                    or (len(valid) == len(TOKEN_FIELDS) and count != sum(valid.values()))):
                issues.add("token_total_mismatch")
        cost = usage.get("cost")
        cost = cost.get("total") if isinstance(cost, dict) else None
        # This adapter has no price table: zero-valued provider rates do not
        # establish that an actual model request was free.
        if (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                and math.isfinite(cost) and cost > 0):
            costs += 1
            total_cost += cost
    if pending_messages:
        issues.add("message_unfinished")
    if pending_compactions:
        issues.add("compaction_unfinished")
    if not messages and not compactions:
        issues.add("no_usage_events")
    complete = not issues
    metadata = {
        "version": ACCOUNTING_VERSION,
        "status": "complete" if complete else "incomplete",
        "issues": sorted(issues),
        "message_events": messages,
        "compaction_events": compactions,
        "usage_events": usages,
        "cache_write_tokens": totals["cacheWrite"] if "cacheWrite" in observed else None,
        "cost_status": ("complete" if complete and costs == usages and costs
                        else "incomplete" if costs else "not_reported"),
    }
    return {
        "n_input_tokens": totals["input"] + totals["cacheRead"] + totals["cacheWrite"]
        if observed & {"input", "cacheRead", "cacheWrite"} else None,
        "n_output_tokens": totals["output"] if "output" in observed else None,
        "n_cache_tokens": totals["cacheRead"] if "cacheRead" in observed else None,
        "cost_usd": total_cost if costs else None,
        "usage_accounting": metadata,
    }


def collect_pi_usage_file(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            return collect_pi_usage(lines)
    except OSError as error:
        result = collect_pi_usage(())
        result["usage_accounting"]["issues"] = [
            "log_missing" if isinstance(error, FileNotFoundError) else "log_unreadable"
        ]
        return result
