from __future__ import annotations

import json
from pathlib import Path

from forgeflow.cli import main


def make_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "synthetic-run"
    trial = run_dir / "harbor/job/trial"
    (trial / "verifier").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "task": "2280_medium_repair_plan_medium",
        "agent": "pi",
        "model": {"model": "nvidia/test"},
    }))
    (trial / "result.json").write_text(json.dumps({
        "started_at": "2026-10-20T13:00:00+00:00",
        "finished_at": "2026-10-20T13:02:00+00:00",
        "verifier_result": {"rewards": {"reward": 100}},
    }))
    (trial / "verifier/reward.json").write_text(json.dumps({"overall_score": 100}))
    (trial / "verifier/reward.txt").write_text("100")
    return run_dir


def test_sign_then_verify_a_recorded_run(tmp_path, capsys):
    run_dir = make_run(tmp_path)
    key = tmp_path / "key.pem"
    assert main(["attest", "--run-dir", str(run_dir), "--key", str(key), "--generate-key"]) == 0
    signed = json.loads(capsys.readouterr().out)
    assert signed["verified"] is True
    assert (run_dir / "attestation/verifier.json").is_file()
    assert (run_dir / "attestation/verifier.sig").is_file()

    assert main(["attest", "--run-dir", str(run_dir), "--verify"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked == {"run_id": "synthetic-run", "verified": True, "reason": None}


def test_a_tampered_record_fails_verification(tmp_path, capsys):
    run_dir = make_run(tmp_path)
    key = tmp_path / "key.pem"
    assert main(["attest", "--run-dir", str(run_dir), "--key", str(key), "--generate-key"]) == 0
    capsys.readouterr()
    path = run_dir / "attestation/verifier.json"
    payload = json.loads(path.read_text())
    payload["task"] = "another-task"
    path.write_text(json.dumps(payload))
    assert main(["attest", "--run-dir", str(run_dir), "--verify"]) == 1
    checked = json.loads(capsys.readouterr().out)
    assert checked["verified"] is False
    assert checked["reason"]


def test_missing_record_is_not_verified(tmp_path, capsys):
    run_dir = make_run(tmp_path)
    assert main(["attest", "--run-dir", str(run_dir), "--verify"]) == 1
    checked = json.loads(capsys.readouterr().out)
    assert checked["verified"] is False


def test_key_is_required_to_sign(tmp_path, capsys):
    run_dir = make_run(tmp_path)
    assert main(["attest", "--run-dir", str(run_dir)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "configuration_error"
