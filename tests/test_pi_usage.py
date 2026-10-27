import json

import pytest

from forgeflow.agents.pi_usage import (
    ACCOUNTING_VERSION,
    accounting_view,
    collect_pi_usage,
    collect_pi_usage_file,
)


def usage(input=100, output=20, cache_read=30, cache_write=5, cost=0.1):
    return {"input": input, "output": output, "cacheRead": cache_read,
            "cacheWrite": cache_write, "reasoning": output // 2,
            "totalTokens": input + output + cache_read + cache_write,
            "cost": {"total": cost}}


def message(data=None, **extra):
    return {"type": "message_end", "message": {
        "role": "assistant", "usage": usage() if data is None else data, **extra}}


def compaction(data=None, **extra):
    return {"type": "compaction_end", "aborted": False,
            "result": {"usage": usage(50, 10, 7, 3, .02) if data is None else data},
            **extra}


def collect(*events):
    return collect_pi_usage(json.dumps(event) for event in events)


def test_regular_and_compaction_usage_share_one_disjoint_cache_basis():
    result = collect(message(), compaction())
    assert result["n_input_tokens"] == 195  # 150 fresh + 37 read + 8 write
    assert result["n_output_tokens"] == 30  # reasoning is already included
    assert result["n_cache_tokens"] == 37
    assert result["cost_usd"] == pytest.approx(.12)
    assert result["usage_accounting"] == {
        "version": ACCOUNTING_VERSION, "status": "complete", "issues": [],
        "message_events": 1, "compaction_events": 1, "usage_events": 2,
        "cache_write_tokens": 8, "cost_status": "complete",
    }


def test_no_compaction_and_nonfinal_copies_do_not_double_count():
    data = usage()
    result = collect(
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "message_update", "message": {"role": "assistant", "usage": data}},
        message(data), {"type": "message_end", "message": {"role": "toolResult"}},
        # Session entries are not a second accounting source.
        {"type": "compaction", "usage": data},
    )
    assert result["n_input_tokens"] == 135
    assert result["n_output_tokens"] == 20
    assert result["usage_accounting"]["status"] == "complete"
    assert result["usage_accounting"]["compaction_events"] == 0


@pytest.mark.parametrize("event,issue", [
    ({"type": "compaction_end", "aborted": True}, "compaction_interrupted"),
    ({"type": "compaction_end", "aborted": False, "errorMessage": "failed"},
     "compaction_interrupted"),
    (compaction(result={}), "compaction_usage_missing"),
    ({"type": "message_end", "message": {"role": "assistant"}},
     "message_usage_missing"),
    (message(stopReason="aborted"), "message_interrupted"),
    ({"type": "compaction_start"}, "compaction_unfinished"),
    ({"type": "message_start", "message": {"role": "assistant"}}, "message_unfinished"),
])
def test_missing_or_interrupted_usage_keeps_only_observed_subtotals(event, issue):
    result = collect(message(), event)
    assert result["usage_accounting"]["status"] == "incomplete"
    assert issue in result["usage_accounting"]["issues"]
    assert result["usage_accounting"]["cost_status"] == "incomplete"


def test_interrupted_compaction_preserves_any_reported_usage_without_asserting_completeness():
    result = collect(message(), compaction(aborted=True))
    assert result["n_input_tokens"] == 195
    assert result["usage_accounting"]["status"] == "incomplete"


@pytest.mark.parametrize("bad", [None, True, -1, 1.5, "20"])
def test_invalid_or_missing_token_counter_does_not_become_complete(bad):
    data = usage()
    data["input"] = bad
    result = collect(message(data))
    assert result["n_input_tokens"] == 35  # only recorded cache counters
    assert "invalid_token_count" in result["usage_accounting"]["issues"]


def test_empty_usage_or_mismatched_total_is_not_complete():
    empty = collect(message({}))
    assert empty["usage_accounting"]["status"] == "incomplete"
    assert empty["n_input_tokens"] is None
    assert empty["n_output_tokens"] is None
    assert empty["n_cache_tokens"] is None
    data = usage()
    data["totalTokens"] += 1
    assert "token_total_mismatch" in collect(message(data))["usage_accounting"]["issues"]


def test_zero_unreported_cost_does_not_become_a_free_model_claim():
    result = collect(message(usage(cost=0)), compaction(usage(cost=0)))
    assert result["cost_usd"] is None
    assert result["usage_accounting"]["status"] == "complete"
    assert result["usage_accounting"]["cost_status"] == "not_reported"


def test_success_after_compaction_retry_keeps_only_final_attempt_usage_and_is_incomplete():
    result = collect(
        message(), {"type": "compaction_start", "reason": "threshold"},
        {"type": "summarization_retry_scheduled", "attempt": 1, "maxAttempts": 3,
         "delayMs": 1000, "errorMessage": "terminated"},
        {"type": "summarization_retry_attempt_start", "source": "compaction",
         "reason": "threshold"},
        {"type": "summarization_retry_finished"}, compaction(),
    )
    assert result["n_input_tokens"] == 195
    assert result["n_output_tokens"] == 30
    assert result["cost_usd"] == pytest.approx(.12)
    assert result["usage_accounting"]["status"] == "incomplete"
    assert result["usage_accounting"]["cost_status"] == "incomplete"
    assert result["usage_accounting"]["issues"] == ["summarization_retry_usage_missing"]
    assert accounting_view(result["usage_accounting"])["issues"] == ["summarization_retry_usage_missing"]


@pytest.mark.parametrize("kind", [
    "summarization_retry_scheduled", "summarization_retry_attempt_start",
    "summarization_retry_finished",
])
def test_any_recorded_summary_retry_marks_unreported_attempt_usage(kind):
    result = collect({"type": kind}, compaction())
    assert "summarization_retry_usage_missing" in result["usage_accounting"]["issues"]
    assert result["usage_accounting"]["status"] == "incomplete"


@pytest.mark.parametrize("event", [message, compaction])
def test_all_zero_provider_default_counters_do_not_establish_complete_usage(event):
    result = collect(event(usage(0, 0, 0, 0, 0)))
    assert result["n_input_tokens"] == result["n_output_tokens"] == 0
    assert result["usage_accounting"]["status"] == "incomplete"
    assert result["usage_accounting"]["issues"] == ["zero_usage_unconfirmed"]
    assert result["cost_usd"] is None


def test_nonzero_input_with_zero_output_is_still_valid_reported_usage():
    result = collect(message(usage(input=10, output=0, cache_read=0, cache_write=0, cost=0)))
    assert result["n_input_tokens"] == 10
    assert result["n_output_tokens"] == 0
    assert result["usage_accounting"]["status"] == "complete"
    assert result["usage_accounting"]["issues"] == []


def test_missing_log_and_truncated_json_are_explicit_and_reading_is_idempotent(tmp_path):
    path = tmp_path / "pi.txt"
    missing = collect_pi_usage_file(path)
    assert missing["n_input_tokens"] is None
    assert missing["usage_accounting"]["issues"] == ["log_missing"]
    path.write_text("diagnostic\n" + json.dumps(message()) + '\n{"type":')
    first = collect_pi_usage_file(path)
    assert first == collect_pi_usage_file(path)
    assert first["n_input_tokens"] == 135
    assert "malformed_event" in first["usage_accounting"]["issues"]


def test_old_or_malformed_metadata_never_asserts_complete():
    assert accounting_view(None)["status"] == "unknown"
    assert accounting_view({"status": "complete"})["status"] == "unknown"
    assert accounting_view({"version": ACCOUNTING_VERSION, "status": [],
                            "cost_status": {}, "issues": [{}]})["status"] == "unknown"
