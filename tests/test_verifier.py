import json

import pytest
from harbor.models.trial.paths import TrialPaths
from harbor.verifier.verifier import VerifierOutputParseError

from forgeflow.verifier import ERPBenchVerifier


def make_verifier(tmp_path, payload, text=None):
    verifier = object.__new__(ERPBenchVerifier)
    verifier.trial_paths = TrialPaths(tmp_path)
    verifier.trial_paths.verifier_dir.mkdir()
    verifier.trial_paths.reward_json_path.write_text(json.dumps(payload))
    if text is not None:
        verifier.trial_paths.reward_text_path.write_text(str(text))
    return verifier


@pytest.mark.parametrize("score", [0, 24.5, 98.99, 99, 100])
def test_preserves_original_reward_and_scale(tmp_path, score):
    payload = {
        "overall_score": score,
        "passed": score >= 99,
        "constraint": {"earned": 12, "total": 12},
        "rules": {"by_dimension": {"constraint": [{"status": "PASS"}]}},
    }
    verifier = make_verifier(tmp_path, payload, score)
    before = verifier.trial_paths.reward_json_path.read_bytes()
    assert verifier._parse_reward_json() == {"reward": score}
    assert verifier.trial_paths.reward_json_path.read_bytes() == before


@pytest.mark.parametrize("score", [True, "100", None, -1, 101, float("nan"), float("inf")])
def test_rejects_invalid_score(tmp_path, score):
    verifier = make_verifier(tmp_path, {"overall_score": score, "passed": True})
    with pytest.raises(VerifierOutputParseError):
        verifier._parse_reward_json()


def test_rejects_disagreement_between_original_files(tmp_path):
    verifier = make_verifier(tmp_path, {"overall_score": 100, "passed": True}, 0)
    with pytest.raises(VerifierOutputParseError, match="disagree"):
        verifier._parse_reward_json()


def test_rejects_wrong_passed_flag(tmp_path):
    verifier = make_verifier(tmp_path, {"overall_score": 100, "passed": False}, 100)
    with pytest.raises(VerifierOutputParseError, match="passed"):
        verifier._parse_reward_json()


def test_malformed_json_is_not_silently_replaced_with_text(tmp_path):
    verifier = make_verifier(tmp_path, {"overall_score": 100, "passed": True}, 100)
    verifier.trial_paths.reward_json_path.write_text("{bad")
    with pytest.raises(VerifierOutputParseError):
        verifier._parse_reward_json()


def test_text_only_result_still_validates_scale(tmp_path):
    verifier = make_verifier(tmp_path, {}, 100)
    verifier.trial_paths.reward_json_path.unlink()
    assert verifier._parse_reward_text() == {"reward": 100}
    verifier.trial_paths.reward_text_path.write_text("101")
    with pytest.raises(VerifierOutputParseError):
        verifier._parse_reward_text()


def test_verification_records_separate_host_phase_without_changing_result(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from harbor.verifier.verifier import Verifier

    result = object()
    calls = []
    async def original(instance):
        calls.append(instance)
        return result
    monkeypatch.setattr(Verifier, 'verify', original)
    verifier = make_verifier(tmp_path, {'overall_score': 100, 'passed': True}, 100)
    verifier.task = SimpleNamespace(config=SimpleNamespace(verifier=SimpleNamespace(timeout_sec=300)))
    before = verifier.trial_paths.reward_json_path.read_bytes()
    assert asyncio.run(verifier.verify()) is result
    assert calls == [verifier]
    assert verifier.trial_paths.reward_json_path.read_bytes() == before
    record = json.loads((tmp_path / 'host-verifier-budget.json').read_text())
    assert set(record['phases']) == {'verification'}
    phase = record['phases']['verification']
    assert phase['status'] == 'completed'
    assert phase['limit_sec'] == 300
    assert phase['elapsed']['continuous_sec'] >= 0
    assert not (verifier.trial_paths.verifier_dir / 'host-verifier-budget.json').exists()


def test_verification_timeout_is_bounded_and_preserves_original_reward(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from harbor.verifier.verifier import Verifier

    from forgeflow.budget import BudgetExceeded

    async def original(instance):
        await asyncio.sleep(30)
    monkeypatch.setattr(Verifier, 'verify', original)
    verifier = make_verifier(tmp_path, {'overall_score': 0, 'passed': False}, 0)
    verifier.task = SimpleNamespace(config=SimpleNamespace(verifier=SimpleNamespace(timeout_sec=0.01)))
    before = verifier.trial_paths.reward_json_path.read_bytes()
    with pytest.raises(BudgetExceeded):
        asyncio.run(verifier.verify())
    record = json.loads((tmp_path / 'host-verifier-budget.json').read_text())
    assert record['phases']['verification']['status'] == 'timed_out'
    assert verifier.trial_paths.reward_json_path.read_bytes() == before
