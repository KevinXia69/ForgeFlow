"""Failures, missing usage and pending judgments must remain visible in metrics."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "investigation_results", Path(__file__).resolve().parents[1] / "scripts/investigation_results.py")
results = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(results)


def test_failed_and_unscored_attempts_remain_in_all_applicable_denominators():
    common = dict(expected="prepared", request_denominator=1, zero_writes=True,
                  queries=4, validations=1, prepares=0, model_tool_calls=5,
                  ablation_triggered=False, blocked_validation_count=0, cost_usd=None)
    success = dict(common, score=1, status="prepared", service_status="prepared",
                   request_correct=True, source_coverage=True, needs_manual_review=False,
                   tokens=100, seconds=10)
    failure = dict(common, score=0, status="technical_failure", service_status="investigating",
                   request_correct=False, source_coverage=False, needs_manual_review=False,
                   tokens=None, seconds=20)
    pending = dict(common, expected="needs_clarification", request_denominator=0, score=None,
                   status="needs_clarification", service_status="needs_clarification",
                   request_correct=None, source_coverage=None, needs_manual_review=True,
                   tokens=50, seconds=15)
    actual = results.group_metrics([success, failure, pending])
    assert actual["business_success"] == results.ratio(1, 3)
    assert actual["qualified_preparation"] == results.ratio(1, 2)
    assert actual["request_correct"] == results.ratio(1, 2)
    assert actual["necessary_clarification"] == results.ratio(0, 1)
    assert actual["unknown_token_episodes"] == 1
    assert actual["known_total_tokens"] == 150
    assert actual["manual_review_episodes"] == 1
    assert actual["cost_usd"] is None


def saved_episode(path, *, flags=None, trace=None):
    path.mkdir()
    summary = {"case_id": "case-a", "arm": "investigation", "split": "development",
               "status": "technical_failure", "score": 0, "wall_seconds": 3,
               "oracle": {"expected_status": "prepared"}, "experiment_flags": flags or {}}
    grade = dict(case_id="case-a", actual_status="technical_failure", expected_status="prepared",
                 score=0, request_denominator=1, request_correct=False, source_coverage=False,
                 artifact_integrity=False, protocol_integrity=False, zero_writes=True,
                 needs_manual_review=False, grader_source_sha256="a" * 64)
    (path / "summary.json").write_text(json.dumps(summary))
    (path / "business-grade.json").write_text(json.dumps(grade))
    if trace is not None:
        (path / "trace.json").write_text(json.dumps(trace))
    return path, summary


@pytest.mark.parametrize("field,value", [("case_id", "other"), ("status", "prepared"), ("score", 1)])
def test_mismatched_summary_cannot_reuse_another_grade(tmp_path, field, value):
    path, summary = saved_episode(tmp_path / "episode")
    summary[field] = value
    (path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="binding_mismatch"):
        results.episode_row(path)


def test_distinct_ablations_never_share_a_denominator(tmp_path):
    draft, _ = saved_episode(tmp_path / "draft", flags={"single_draft_feedback_retry": True})
    prepare, _ = saved_episode(tmp_path / "prepare", flags={"single_prepare_feedback_retry": True})
    groups = results.summarize([draft, prepare])["groups"]
    assert set(groups) == {"development/ablation-single-draft", "development/ablation-single-prepare"}
    assert all(group["business_success"]["denominator"] == 1 for group in groups.values())


def test_missing_counts_use_trace_or_remain_unknown(tmp_path):
    path, _ = saved_episode(tmp_path / "traced", trace=[{"action": "query"}] * 13)
    traced = results.episode_row(path)
    assert traced["queries"] == 13
    assert traced["prepares"] == 0
    assert traced["model_tool_calls"] is None
    path, _ = saved_episode(tmp_path / "unknown")
    unknown = results.episode_row(path)
    assert unknown["queries"] is None
    metrics = results.group_metrics([traced, unknown])
    assert metrics["query_calls"] is None
    assert metrics["known_query_calls"] == 13
    assert metrics["unknown_query_episodes"] == 1


def test_heldout_requires_the_declared_frozen_grader(tmp_path):
    path, summary = saved_episode(tmp_path / "episode")
    summary["split"] = "heldout"
    (path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="requires_frozen"):
        results.episode_row(path)
    with pytest.raises(ValueError, match="frozen_grader_mismatch"):
        results.episode_row(path, grader_sha256="b" * 64)
    assert results.episode_row(path, grader_sha256="a" * 64)["case_id"] == "case-a"
