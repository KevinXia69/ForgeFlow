"""Regression tests for post-evaluation measurements, independent of business grades."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts/investigation_results.py"
SPEC = importlib.util.spec_from_file_location('results_measurement_review', MODULE)
results = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(results)


def saved(path, *, interruption=False, lower_bound=None, synthetic=False, traces=True, usage=None):
    path.mkdir()
    summary = {'case_id': 'case-a', 'arm': 'investigation', 'split': 'development',
               'status': 'technical_failure', 'score': 0, 'wall_seconds': 1500,
               'oracle': {'expected_status': 'prepared'}, 'experiment_flags': {},
               'synthetic_driver_failure_record': synthetic}
    if interruption:
        summary['interruption'] = {'kind': 'user_pause_interruption'}
    if lower_bound is not None:
        summary['wall_seconds'] = lower_bound
        summary['wall_seconds_basis'] = 'Host-observable model runtime lower bound.'
        if interruption:
            summary['interruption']['host_observable_model_seconds_lower_bound'] = lower_bound
    if usage is not None:
        summary['service_usage'] = usage
    grade = {'case_id': 'case-a', 'actual_status': 'technical_failure', 'expected_status': 'prepared',
             'score': 0, 'request_denominator': 1, 'request_correct': False, 'source_coverage': None,
             'artifact_integrity': None, 'protocol_integrity': None, 'zero_writes': False,
             'needs_manual_review': False, 'grader_source_sha256': 'a' * 64}
    (path/'summary.json').write_text(json.dumps(summary))
    (path/'business-grade.json').write_text(json.dumps(grade))
    if traces:
        (path/'trace.json').write_text(json.dumps([{'action':'query'}]*2 + [{'action':'validate'}]))
        (path/'pi.jsonl').write_text('\n'.join(json.dumps({'type':'tool_execution_start'}) for _ in range(4)))
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    return path, before


@pytest.mark.parametrize('interruption,synthetic', [(True, True), (True, False), (False, True)])
def test_interrupted_call_prefixes_are_never_complete_counts(tmp_path, interruption, synthetic):
    path, before = saved(tmp_path/'episode', interruption=interruption, synthetic=synthetic, lower_bound=112.245)
    row = results.episode_row(path)
    for field in ('queries', 'validations', 'prepares', 'model_tool_calls'):
        assert row[field] is None
    assert row['observed_prefix_queries'] == 2
    assert row['observed_prefix_validations'] == 1
    assert row['observed_prefix_prepares'] == 0
    assert row['observed_prefix_model_tool_calls'] == 4
    assert row['seconds'] is None
    assert row['seconds_measurement'] == 'lower_bound'
    assert row['seconds_lower_bound'] == 112.245
    assert row['recorded_wall_seconds'] == 112.245
    assert row['score'] == 0 and row['request_denominator'] == 1
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before


def test_interrupted_summary_counters_are_only_observed_prefixes(tmp_path):
    path, _ = saved(tmp_path/'episode', interruption=True, usage={'queries':8,'validations':3,'prepares':0})
    row = results.episode_row(path)
    assert row['queries'] is None
    assert row['observed_prefix_queries'] == 8
    assert row['observed_prefix_validations'] == 3
    assert row['seconds'] is None
    assert row['seconds_measurement'] == 'unknown'
    assert row['seconds_lower_bound'] is None


def test_interrupted_missing_logs_are_unknown_prefixes_not_zero(tmp_path):
    path, _ = saved(tmp_path/'episode', interruption=True, traces=False)
    row = results.episode_row(path)
    assert row['observed_prefix_queries'] is None
    assert row['observed_prefix_model_tool_calls'] is None
    group = results.group_metrics([row])
    assert group['query_calls'] is None
    assert group['observed_prefix_query_calls'] is None
    assert group['unknown_prefix_query_episodes'] == 1
    assert group['median_seconds'] is None
    assert group['p95_seconds'] is None
    assert group['latency_unknown_episodes'] == 1
    assert group['business_success'] == results.ratio(0,1)


def test_latency_excludes_bounds_without_changing_business_denominators(tmp_path):
    paths = [saved(tmp_path/'complete')[0],
             saved(tmp_path/'interrupted-a', interruption=True, lower_bound=1558.177)[0],
             saved(tmp_path/'interrupted-b', interruption=True, lower_bound=112.245)[0]]
    rows = [results.episode_row(p) for p in paths]
    assert rows[0]['seconds'] == 1500
    assert rows[0]['queries'] == 2
    group = results.group_metrics(rows)
    assert group['median_seconds'] == group['p95_seconds'] == 1500
    assert group['latency_complete_episodes'] == 1
    assert group['latency_excluded_episodes'] == group['latency_lower_bound_episodes'] == 2
    assert group['latency_unknown_episodes'] == 0
    assert group['latency_lower_bounds'] == [
        {'run':'interrupted-a','seconds':1558.177}, {'run':'interrupted-b','seconds':112.245}]
    assert group['query_calls'] is None
    assert group['known_query_calls'] == 2
    assert group['unknown_query_episodes'] == 2
    assert group['observed_prefix_query_calls'] == 4
    assert group['business_success'] == results.ratio(0,3)
    assert group['qualified_preparation'] == results.ratio(0,3)
    assert group['request_correct'] == results.ratio(0,3)
    assert group['unknown_token_episodes'] == 3


def test_explicit_lower_bound_basis_is_not_a_complete_duration(tmp_path):
    path, _ = saved(tmp_path/'episode', lower_bound=15)
    row = results.episode_row(path)
    assert row['seconds'] is None
    assert row['queries'] == 2
    assert row['observed_prefix_queries'] is None
    group = results.group_metrics([row])
    assert group['latency_lower_bound_episodes'] == 1
    assert group['latency_complete_episodes'] == 0
    assert group['median_seconds'] is None
