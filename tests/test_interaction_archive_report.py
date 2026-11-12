"""Exact private archives and honest unfinished batch reports, no providers."""
import importlib
import json
import sys
import tarfile
from pathlib import Path

import pytest

from forgeflow.recovery.storage import atomic_json, digest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
archive = importlib.import_module("interaction_archive")
reporter = importlib.import_module("interaction_report")
schedule = importlib.import_module("interaction_schedule")


def test_archive_retains_exact_visible_replies_and_excludes_bearer_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    root = tmp_path / "evidence"
    worker = root / "worker"
    worker.mkdir(parents=True)
    visible = b'{"text":"raw exact input","reply_sha256":"binding"}\n'
    (worker / "visible-replies.json").write_bytes(visible)
    (worker / "runtime-inputs").mkdir()
    (worker / "runtime-inputs/bridge.json").write_text('{"token":"private-episode-bearer"}')
    output = tmp_path / "packaged/evidence.tar.gz"
    result = archive.archive(root, output, secrets=(b"private-episode-bearer",))
    assert result["private_only"] is True
    with tarfile.open(output) as packed:
        assert packed.extractfile("worker/visible-replies.json").read() == visible
        assert not any("bridge.json" in name for name in packed.getnames())


def test_archive_rejects_credentials_without_rewriting_originals(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    root = tmp_path / "evidence"
    root.mkdir()
    sensitive = root / "trace.json"
    sensitive.write_text('{"accidental":"NEVER_PUBLISH_THIS_VALUE"}')
    with pytest.raises(ValueError, match="credential_in_evidence"):
        archive.archive(root, tmp_path / "out.tar.gz", secrets=(b"NEVER_PUBLISH_THIS_VALUE",))
    assert "NEVER_PUBLISH_THIS_VALUE" in sensitive.read_text()
    assert not (tmp_path / "out.tar.gz").exists()


def test_report_keeps_unstarted_goals_and_null_total_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    atomic_json(tmp_path / "suite.json", {"cases": [{"case_id": "k", "expected": {"terminal": "prepared"}}]})
    batch = {"jobs": schedule.layout("example", ["c"], ["k"]), "suites": {"chain": {"file": "suite.json"}}}
    result = reporter.report(batch, tmp_path / "attempts")
    fixed = result["groups"]["component:fixed"]
    assert fixed["planned_denominator"] == 2 and fixed["outcomes"]["not_started"] == 2
    assert fixed["tokens"] is None and fixed["known_token_prefix"] == 0
    assert fixed["latency_seconds"]["complete"] is False
    assert result["groups"]["chain:fixed"]["goals"]["prepared"] == {"planned": 1, "success": 0}
    assert json.dumps(result)


def _usage(tokens=125, *, unknown=False, cache_unknown=False):
    return {"tokens": None if unknown else tokens, "known_token_prefix": tokens,
        "unknown_workers": ["original-reply"] if unknown else [],
        "provider_workers_dispatched": 1, "provider_responses": None if unknown else 1,
        "known_provider_responses": 0 if unknown else 1,
        "known_usage_by_field": {"input": tokens if unknown else 100, "output": 0 if unknown else 25, "cacheRead": 0, "cacheWrite": 0},
        "usage_by_field": {"input": None if unknown else 100, "output": None if unknown else 25,
                           "cacheRead": None if unknown or cache_unknown else 0, "cacheWrite": None if unknown else 0},
        "workers": [{"worker": "original-reply", "seconds": None if unknown else 3}]}


def _stored_result(root, batch, job, result, trace=None):
    directory = root / job["attempt_id"]
    run = directory / "run"
    run.mkdir(parents=True)
    atomic_json(run / "result.json", {**result, **{key: job[key] for key in ("layer", "case_id", "arm")}})
    if trace is not None:
        atomic_json(run / "interaction-trace.json", trace)
    atomic_json(directory / "started.json", {"job": job, "batch_sha256": digest(batch)})
    atomic_json(directory / "receipt.json", {"job": job, "result_sha256": schedule.file_sha(run / "result.json")})
    return run / "result.json"


def test_segment_totals_preserve_unknown_reply_prefix_without_hiding_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    atomic_json(tmp_path / "suite.json", {"cases": [{"case_id": "k", "expected": {"terminal": "prepared"}}]})
    job = {"attempt_id": "chain-fixed", "layer": "chain", "case_id": "k", "arm": "fixed"}
    batch = {"jobs": [job], "suites": {"chain": {"file": "suite.json"}}}
    zero = {"tokens": 0, "known_token_prefix": 0, "unknown_workers": [], "workers": [],
            "provider_workers_dispatched": 0, "provider_responses": 0, "known_provider_responses": 0,
            "known_usage_by_field": dict.fromkeys(reporter.TOKEN_FIELDS, 0), "usage_by_field": dict.fromkeys(reporter.TOKEN_FIELDS, 0)}
    result = {"status": "unknown", "elapsed_seconds": 8, "actual_confirmations": 0,
              "actual_children": 0, "actual_prepared": 0,
              "stages": {"parent": {"status": "success"}, "reply": {"status": "unknown"}, "child": {"status": "not_reached"}},
              "usage_by_stage": {"parent": _usage(), "reply": _usage(10, unknown=True),
                                 "child": zero, "product_prepare": zero}}
    root = tmp_path / "attempts"
    original = _stored_result(root, batch, job, result, trace=[])
    signature = schedule.file_sha(original)
    output = reporter.report(batch, root)["groups"]["chain:fixed"]
    assert output["tokens"] is None and output["known_token_prefix"] == 135
    parent, reply = output["usage_segments"]["parent"], output["usage_segments"]["reply"]
    assert parent["tokens"] == 125 and parent["usage_by_field"] == {"input": 100, "output": 25, "cacheRead": 0, "cacheWrite": 0}
    assert parent["provider_workers_dispatched"] == parent["provider_responses"] == 1
    assert reply["tokens"] is None and reply["known_token_prefix"] == 10
    assert reply["provider_workers_dispatched"] is None and reply["known_provider_workers_dispatched"] == 1
    assert reply["provider_responses"] is None and reply["worker_seconds"] is None
    assert reply["field_missing_ranges"]["input"] == [{"attempt_id": "chain-fixed", "segment": "reply", "reason": "field_usage_unknown"}]
    assert reply["unknown_workers"] == [{"attempt_id": "chain-fixed", "segment": "reply", "worker": "original-reply"}]
    assert output["usage_segments"]["component"] == {"applicable": False}
    assert output["usage_segments"]["child"]["tokens"] == 0
    assert output["stages"]["child"]["not_reached"] == 1
    assert schedule.file_sha(original) == signature


def test_missing_cache_count_does_not_invent_zero_or_erase_known_total():
    totals = reporter.usage_totals([{"attempt_id": "one", "segment": "component", "usage": _usage(cache_unknown=True)}], [])
    assert totals["tokens"] == 125
    assert totals["usage_by_field"]["input"] == 100
    assert totals["usage_by_field"]["cacheRead"] is None
    assert totals["known_usage_by_field"]["cacheRead"] == 0
    assert totals["field_missing_ranges"]["cacheRead"][0]["attempt_id"] == "one"


def test_all_proposals_and_last_proposal_are_reported_separately(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    atomic_json(tmp_path / "suite.json", {"cases": []})
    job = {"attempt_id": "component-interactive", "layer": "component", "case_id": "c", "arm": "interactive"}
    batch = {"jobs": [job], "suites": {"chain": {"file": "suite.json"}}}
    def audit(status, reason):
        return {"status": status, "action": {"status": status, "reason": reason},
                "language": {"status": "not_reached", "reason": "no_followup_question"}}
    audits = [audit("observed_failure", "wrong_object"), audit("observed_failure", "unnecessary_question"), audit("success", "independent_action_match")]
    admissions = [{"independent": item, "sources": {"status": "success"},
                   "host_false_rejection": False, "host_false_acceptance": False,
                   "model_error_blocked": i < 2} for i, item in enumerate(audits)]
    result = {"status": "success", "elapsed_seconds": 1, "usage": _usage(),
              "raw_proposals": [{"kind": "first"}, {"kind": "second"}, {"kind": "final"}],
              "raw_proposal_audits": audits, "admission_audits": admissions,
              "audit": {"model": audits[-1]}, "fallback": False}
    root = tmp_path / "attempts"
    original = _stored_result(root, batch, job, result)
    signature = schedule.file_sha(original)
    output = reporter.report(batch, root)["groups"]["component:interactive"]
    diagnostics = output["diagnostics"]
    assert diagnostics["last_raw_action_errors"] == 0
    assert diagnostics["all_proposals"]["submitted_proposals"] == 3
    assert diagnostics["all_proposals"]["action_errors"] == 2
    assert diagnostics["all_proposals"]["error_categories"]["wrong_object"] == 1
    assert diagnostics["all_proposals"]["error_categories"]["unnecessary_question"] == 1
    assert output["usage_segments"]["parent"] == {"applicable": False}
    assert output["usage_segments"]["component"]["tokens"] == 125
    assert output["outcomes"]["success"] == 1 and schedule.file_sha(original) == signature
