"""Controlled-fault driver tests use synthetic responses and no sandbox access."""
import importlib.util
from pathlib import Path

import pytest

from forgeflow.recovery import client


@pytest.fixture
def driver():
    spec = importlib.util.spec_from_file_location(
        "product_recovery_acceptance_test", Path(__file__).resolve().parents[1] / "scripts/verify_product_recovery.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apply_is_lost_only_after_actual_success_and_cannot_dispatch_twice(driver, monkeypatch):
    calls = []

    def apply(*args, **kwargs):
        calls.append(True)
        return {"state": "succeeded", "execution_validated": True, "verified_steps": 7,
                "private_payload": "must not enter report"}

    monkeypatch.setattr(client, "apply", apply)
    lost = driver.LostResponses()
    with pytest.raises(client.ExecutorError, match="controlled response loss"):
        lost.apply_then_lose()
    assert len(calls) == 1
    assert lost.apply_return == {"state": "succeeded", "execution_validated": True, "verified_steps": 7}
    with pytest.raises(RuntimeError, match="unexpected_second_apply"):
        lost.apply_then_lose()
    assert len(calls) == 1


def test_real_transport_failure_is_not_recorded_as_successful_response_loss(driver, monkeypatch):
    def apply(*args, **kwargs):
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "apply", apply)
    lost = driver.LostResponses()
    with pytest.raises(client.ExecutorError):
        lost.apply_then_lose()
    assert lost.apply_calls == 1 and lost.apply_return is None


def test_two_read_requests_observe_identical_receipt_and_first_response_is_discarded(driver, monkeypatch):
    calls = []
    receipt = {"execution_validated": True, "receipt_sha256": "a" * 64,
               "verified_at": "2026-10-23T00:00:00+00:00", "verifier": {
                   "strict_success": True, "evaluation_valid": True,
                   "verification_attempt": "same-root-attempt", "raw_original_reward": "private",
               }}

    def reconcile(*args, **kwargs):
        calls.append(True)
        return receipt

    monkeypatch.setattr(client, "reconcile", reconcile)
    lost = driver.LostResponses()
    with pytest.raises(client.ExecutorError, match="controlled response loss"):
        lost.reconcile_observed()
    assert len(calls) == 1 and len(lost.reconcile_returns) == 1
    assert lost.reconcile_observed() == receipt
    assert len(calls) == 2 and lost.reconcile_returns[0] == lost.reconcile_returns[1]
    assert "private" not in str(lost.reconcile_returns)
    with pytest.raises(RuntimeError, match="unexpected_additional_reconciliation"):
        lost.reconcile_observed()
    assert len(calls) == 2


def test_approval_driver_requires_explicit_opt_in_before_loading_environment(driver, monkeypatch, tmp_path):
    monkeypatch.setattr(driver.sys, "argv", ["driver", "--output", str(tmp_path / "evidence")])
    monkeypatch.setattr(driver.runpy, "run_path", lambda *args, **kwargs: pytest.fail("launcher accessed without authorization"))
    with pytest.raises(SystemExit) as error:
        driver.main()
    assert error.value.code == 2
    assert not (tmp_path / "evidence").exists()


def test_driver_will_not_send_a_second_authorization_request(driver, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "product", lambda *a, **kw: calls.append(True))
    lost = driver.LostResponses()
    lost.issue_once()
    with pytest.raises(RuntimeError, match="unexpected_second_authorization_request"):
        lost.issue_once()
    assert calls == [True]
