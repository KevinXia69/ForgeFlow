import json
import subprocess
from types import SimpleNamespace

import pytest

from forgeflow.product import verifier as module


def test_verifier_only_reads_new_reward_and_keeps_original_source_private(monkeypatch):
    calls = []
    reward = {"overall_score": 100, "passed": True, "rules": {"total": 1, "by_dimension": {
        "constraint": [{"rule": "delivery", "status": "PASS", "passed": True,
                        "applicable": True, "expr": "checker-private-expression", "args": {"test": 1}}],
    }}}

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(reward) if len(calls) == 3 else "")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.OriginalProductVerifier("configured-product-container")()
    assert result["strict_success"] is True
    assert result["passed_rule_count"] == 1
    assert "checker-private-expression" not in json.dumps(result["rules"])
    assert "checker-private-expression" in result["_original_reward_json"]
    assert calls[0][:4] == ["docker", "exec", "configured-product-container", "python3"]
    assert calls[1] == ["docker", "exec", "configured-product-container", "timeout",
                        "--signal=TERM", "--kill-after=10s", "300s", "bash", "/tests/test.sh"]
    assert calls[2][-1] == module._READ_REWARD


@pytest.mark.parametrize("fault", ["rotation", "process", "missing_reward", "timeout", "invalid_json"])
def test_failed_fresh_output_never_reuses_old_pass_or_discloses_subprocess_error(monkeypatch, fault):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if fault == "timeout":
            raise subprocess.TimeoutExpired("private command", 1, output="private output")
        fail_at = {"rotation": 1, "process": 2, "missing_reward": 3}.get(fault)
        return SimpleNamespace(returncode=1 if len(calls) == fail_at else 0,
                               stdout="not json" if len(calls) == 3 else "", stderr="private error")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.OriginalProductVerifier("configured-product-container")()
    assert result["strict_success"] is None
    assert result["evaluation_valid"] is not True
    assert "private" not in json.dumps(result)
    assert len(calls) <= 3


def test_rotation_preserves_prior_reward_exact_bytes_and_requires_new_file(tmp_path):
    output = tmp_path / "verifier"
    output.mkdir()
    reward = output / "reward.json"
    original = b'{"passed":true,"source":"old-case"}'
    reward.write_bytes(original)
    code = module._ROTATE_REWARD.replace("/logs/verifier/reward.json", str(reward))
    subprocess.run(["python3", "-c", code, "testattempt"], check=True, capture_output=True)
    assert not reward.exists()
    assert (output / "product-verification-history/testattempt.json").read_bytes() == original


@pytest.mark.parametrize("container", ["", "--privileged", "foo;bar", "../escape", "name\nother"])
def test_verifier_container_is_launch_configuration_not_shell_input(container):
    with pytest.raises(ValueError, match="invalid_product_verifier_container"):
        module.OriginalProductVerifier(container)


def test_nonzero_process_with_reward_preserves_original_failure(monkeypatch):
    calls = []
    raw = {"overall_score": 20, "passed": False, "rules": {"total": 1, "by_dimension": {
        "constraint": [{"rule": "delivery", "status": "FAIL", "passed": False}],
    }}}

    def run(args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            assert kwargs["timeout"] > 310
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=json.dumps(raw) if len(calls) == 3 else "")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.OriginalProductVerifier("configured-product-container")()
    assert result["status"] == "failed"
    assert result["score"] == 20
    assert result["strict_success"] is False
    assert result["evaluation_valid"] is False
    assert json.loads(result["_original_reward_json"]) == raw
