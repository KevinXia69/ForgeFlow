"""Worker replay/restart boundaries using a real persistent investigation service."""
import pytest
from investigation_support import EVENT
from test_investigation_continuation import make_system

from forgeflow.investigation.cases import CaseError
from forgeflow.investigation.continuation import ContinuationCoordinator
from forgeflow.investigation.service import InvestigationService
from forgeflow.recovery.storage import StorageError, read_json


@pytest.fixture
def system(tmp_path):
    return make_system(tmp_path)


def runner_for(system, calls):
    def run(*, config, allowance, parent_context, work_directory):
        calls.append(config["run_id"])
        service = InvestigationService(source=system["factory"](config["policy"]), contract=config["policy"],
            event=EVENT, store=config["store"], bundle=config["store"] / "unused", connection_factory=lambda: None)
        service.dispatch(config["run_id"], "finish", {"status": "needs_clarification",
            "reason": "Missing budget", "question": "What is the authorized budget for SO-30?"})
        return {"usage": {"tokens": 0, "seconds": 0.1, "queries": 0, "validations": 0, "prepares": 0}}
    return run


def test_completed_parent_replayed_without_another_worker(system):
    calls = []
    c = ContinuationCoordinator(system["cases"], episode_runner=runner_for(system, calls), reply_interpreter=None)
    view = system["view"]
    first = c.run_episode(view["case_id"], "owner")
    assert first["status"] == "awaiting_human"
    assert c.run_episode(view["case_id"], "owner") == first
    assert calls == [view["current_episode"]]


def test_started_worker_is_not_relaunched_after_process_failure(system):
    calls = []

    def failing(**kwargs):
        calls.append(1)
        raise RuntimeError("host disappeared")

    c = ContinuationCoordinator(system["cases"], episode_runner=failing, reply_interpreter=None)
    view = system["view"]
    with pytest.raises(RuntimeError, match="host disappeared"):
        c.run_episode(view["case_id"], "owner")
    with pytest.raises(CaseError, match="worker_result_unknown"):
        c.run_episode(view["case_id"], "owner")
    assert calls == [1]
    current = system["cases"].get(view["case_id"], "owner")
    assert current["usage"]["tokens"] is None
    assert current["usage_complete"] is False


def test_parallel_invocation_cannot_claim_same_worker(system):
    calls = []
    inner = runner_for(system, calls)
    view = system["view"]

    def nested(**kwargs):
        with pytest.raises(StorageError, match="another_recovery_operation_running"):
            c.run_episode(view["case_id"], "owner")
        return inner(**kwargs)

    c = ContinuationCoordinator(system["cases"], episode_runner=nested, reply_interpreter=None)
    c.run_episode(view["case_id"], "owner")
    assert len(calls) == 1


def test_result_receipt_recovers_crash_before_case_accounting(system, monkeypatch):
    calls = []
    cases = system["cases"]
    c = ContinuationCoordinator(cases, episode_runner=runner_for(system, calls), reply_interpreter=None)
    view = system["view"]
    original = cases.finish_work
    monkeypatch.setattr(cases, "finish_work", lambda *a, **k: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        c.run_episode(view["case_id"], "owner")
    monkeypatch.setattr(cases, "finish_work", original)
    current = c.run_episode(view["case_id"], "owner")
    assert current["status"] == "awaiting_human"
    assert current["usage"]["seconds"] == 0.1
    assert len(calls) == 1


def test_reply_interpretation_uses_same_case_meter_and_no_erp(system):
    calls = []
    def interpret(**arguments):
        assert set(arguments) == {"ticket", "reply", "currency", "history", "strategy", "allowance", "work_directory"}
        return {"proposal": {"kind": "budget_authorization_proposal", "order_id": "30", "amount": "800", "currency": "USD"},
                "usage": {"tokens": 1500, "seconds": 1, "queries": 0, "validations": 0, "prepares": 0}}
    cases = system["cases"]
    c = ContinuationCoordinator(cases, episode_runner=runner_for(system, calls), reply_interpreter=interpret)
    view = c.run_episode(system["view"]["case_id"], "owner")
    reply = cases.submit_reply(view["case_id"], "owner", operation_id="answer", expected_revision=view["revision"], text="Budget 800 USD for SO-30")
    result = c.interpret_reply(view["case_id"], "owner", reply["reply_id"], strategy="fixed")
    assert result["usage"]["tokens"] == 1500
    assert result["confirmation"] is None and len(result["episodes"]) == 1
    assert c.interpret_reply(view["case_id"], "owner", reply["reply_id"], strategy="fixed") == result
    with pytest.raises(CaseError, match="worker_receipt_mismatch"):
        c.interpret_reply(view["case_id"], "owner", reply["reply_id"], strategy="interactive")


def test_unproven_reconciliation_keeps_original_claim(system):
    c = ContinuationCoordinator(system["cases"], episode_runner=lambda **k: (_ for _ in ()).throw(RuntimeError()), reply_interpreter=None)
    view = system["view"]
    with pytest.raises(RuntimeError):
        c.run_episode(view["case_id"], "owner")
    operation_id = "episode-" + view["current_episode"]
    with pytest.raises(CaseError, match="worker_result_unknown"):
        c.reconcile_worker(view["case_id"], "owner", operation_id,
                           recover=lambda claim, root: {"quiescence": "unknown", "original_worker_verified": True})
    state = read_json(system["cases"]._directory(view["case_id"]) / "case.json")
    assert state["work"][operation_id]["phase"] == "reserved"


def test_invalid_runtime_keeps_known_usage_but_never_continues(system):
    calls = []
    ordinary = runner_for(system, calls)
    def rejected(**kwargs):
        result = ordinary(**kwargs)
        result['runtime_valid'] = False
        result['usage']['tokens'] = 1234
        return result
    coordinator = ContinuationCoordinator(system['cases'], episode_runner=rejected, reply_interpreter=None)
    view = coordinator.run_episode(system['view']['case_id'], 'owner')
    assert view['status'] == 'stopped' and view['terminal']['reason'] == 'runtime_validation_failed'
    assert view['usage']['tokens'] == 1234 and view['usage_complete']
    assert view['ticket'] is None
    assert coordinator.run_episode(view['case_id'], 'owner') == view
    assert len(calls) == 1


def test_zero_token_product_grant_still_requires_prepare_allowance(system):
    c = ContinuationCoordinator(system['cases'], episode_runner=None, reply_interpreter=None)
    view = system['view']
    desired = {'tokens': 0, 'seconds': 3600, 'queries': 0, 'validations': 0, 'prepares': 1}
    assert c._grant(view, desired)['tokens'] == 0
    view['usage']['prepares'] = view['limits']['prepares']
    with pytest.raises(CaseError, match='case_budget_exhausted'):
        c._grant(view, desired)
