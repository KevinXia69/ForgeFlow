from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeflow import attestation
from forgeflow.runner import attest_run
from forgeflow.runview import build_run_view


def make_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "attested-run"
    trial = run_dir / "harbor/job/trial"
    (trial / "verifier").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "task": "2280_medium_repair_plan_medium", "agent": "pi",
        "model": {"model": "nvidia/test"},
    }))
    (trial / "result.json").write_text(json.dumps({
        "verifier_result": {"rewards": {"reward": 100}},
        "verifier": {"started_at": "2026-10-21T12:00:00Z",
                     "finished_at": "2026-10-21T12:01:00Z"},
    }))
    (trial / "verifier/reward.json").write_text(json.dumps({"overall_score": 100}))
    (trial / "verifier/reward.txt").write_text("100")
    return run_dir


def test_attest_run_signs_the_verifier_artifacts(tmp_path):
    run_dir = make_run(tmp_path)
    key_path = tmp_path / "key.pem"
    attestation.generate_key(key_path)
    result = attest_run(run_dir, key_path)
    assert result["status"] == "verified"
    assert result["artifact_count"] >= 2
    view = build_run_view(run_dir, tmp_path / "runs")
    assert view["attestation"]["status"] == "verified"


def test_attest_run_skips_when_no_trial_exists(tmp_path):
    run_dir = tmp_path / "runs" / "empty"
    run_dir.mkdir(parents=True)
    key_path = tmp_path / "key.pem"
    attestation.generate_key(key_path)
    assert attest_run(run_dir, key_path) == {"status": "skipped", "reason": "trial_missing"}


def test_a_tampered_record_is_reported_as_invalid(tmp_path):
    run_dir = make_run(tmp_path)
    key_path = tmp_path / "key.pem"
    attestation.generate_key(key_path)
    attest_run(run_dir, key_path)
    path = run_dir / "attestation/verifier.json"
    payload = json.loads(path.read_text())
    payload["task"] = "other"
    path.write_text(json.dumps(payload))
    assert build_run_view(run_dir, tmp_path / "runs")["attestation"]["status"] == "invalid"


def test_a_missing_key_raises_and_leaves_no_record(tmp_path):
    run_dir = make_run(tmp_path)
    with pytest.raises(OSError):
        attest_run(run_dir, tmp_path / "absent.pem")
    assert not (run_dir / "attestation").exists()
