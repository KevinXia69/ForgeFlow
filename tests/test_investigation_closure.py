"""Real aggregate/worker closure controls; no model, network or ERP writes."""
import threading

import pytest
from investigation_support import EVENT
from test_continuation_host import wait
from test_continuation_worker import runner_for
from test_investigation_continuation import confirm, make_system, proposed, waiting

from forgeflow.investigation.cases import RESOURCE_FIELDS, CaseError, InvestigationCases
from forgeflow.investigation.continuation import ContinuationCoordinator
from forgeflow.investigation.host import ContinuationHost
from forgeflow.investigation.intake import task_template
from forgeflow.recovery.storage import atomic_json, read_json


def setup(tmp_path, *, expires=False, runner=None):
    system = make_system(tmp_path / "seed")
    clock = [100]
    cases = InvestigationCases(tmp_path / "cases", source_factory=system["factory"],
        generation=lambda: system["environment"]["generation"], currency_probe=system["cases"].currency_probe,
        clock=lambda: clock[0])
    template = task_template(system["contract"])
    if expires:
        template["fields"]["expires_at_utc"] = "1970-01-01T00:02:30+00:00"
    view = cases.create("owner", operation_id="create-1", template=template, policy=system["policy"], event=EVENT)
    system.update(cases=cases, view=view)
    calls = []
    coordinator = ContinuationCoordinator(cases, episode_runner=runner or runner_for(system, calls), reply_interpreter=None)
    host = ContinuationHost(cases=cases, coordinator=coordinator, product=None, create_inputs=lambda _: {})
    return system, host, calls, clock


def cancel(host, view, operation="close"):
    return host.request_closure(view["case_id"], "owner", operation_id=operation,
                                expected_revision=view["revision"])


def test_expired_waiting_closes_and_next_valid_case_admits(tmp_path):
    system, host, calls, clock = setup(tmp_path, expires=True)
    view = waiting(system)
    cases = system["cases"]
    clock[0] = 200
    before = (cases._directory(view["case_id"]) / "case.json").read_bytes()
    assert cases.get(view["case_id"], "owner")["policy_expired"] is True
    assert (cases._directory(view["case_id"]) / "case.json").read_bytes() == before
    closed = host.expire(view["case_id"], "owner")
    assert closed["status"] == "closed"
    assert closed["closure_intent"]["reason"] == "policy_expired"
    assert closed["closure_intent"]["phase"] == "closed"
    receipt = closed["environment_released"]
    other = cases.create("owner", operation_id="next", template=task_template(system["contract"]),
                         policy=system["policy"], event=EVENT)
    assert other["status"] == "investigating"
    assert host.reconcile_closure(view["case_id"], "owner")["environment_released"] == receipt
    assert read_json(cases.root / "environment.json")["active_case"] == other["case_id"]
    assert calls == []


def test_authenticated_cancel_after_expiry_and_cas_replay(tmp_path):
    system, host, _, clock = setup(tmp_path, expires=True)
    view = waiting(system)
    clock[0] = 200
    with pytest.raises(CaseError, match="case_not_found"):
        host.request_closure(view["case_id"], "stranger", operation_id="close", expected_revision=view["revision"])
    closed = cancel(host, view)
    assert closed["closure_intent"]["reason"] == "user_cancelled"
    assert cancel(host, view)["environment_released"] == closed["environment_released"]
    with pytest.raises(CaseError, match="operation_input_conflict"):
        host.request_closure(view["case_id"], "owner", operation_id="close",
                             expected_revision=view["revision"], reason="policy_expired")


def test_false_expiry_and_old_generation_never_clear_claim(tmp_path):
    system, host, _, _ = setup(tmp_path)
    view = waiting(system)
    with pytest.raises(CaseError, match="closure_expiry_unproven"):
        host.expire(view["case_id"], "owner")
    system["cases"].request_closure(view["case_id"], "owner", operation_id="close", expected_revision=view["revision"])
    system["environment"]["generation"] = "generation-2"
    blocked = host.reconcile_closure(view["case_id"], "owner")
    assert blocked["closure_intent"]["blockers"] == ["environment_generation_changed"]
    assert blocked["environment_released"] is None
    assert read_json(system["cases"].root / "environment.json")["active_case"] == view["case_id"]


def test_confirm_wins_then_closure_keeps_child_history_but_no_dispatch(tmp_path):
    system, host, calls, _ = setup(tmp_path)
    view = proposed(system)
    decision = confirm(system["cases"], view)
    current = system["cases"].get(view["case_id"], "owner")
    child_policy = system["cases"]._directory(view["case_id"]) / "episodes" / decision["child_episode"] / "policy.json"
    before = child_policy.read_bytes()
    closed = cancel(host, current)
    assert closed["confirmation"] is not None and len(closed["episodes"]) == 2
    assert child_policy.read_bytes() == before
    with pytest.raises(CaseError, match="case_environment_released"):
        host.schedule("child", view["case_id"], "owner", "confirm-1")
    assert calls == []


def test_closure_wins_confirmation_cannot_authorize(tmp_path):
    system, host, _, _ = setup(tmp_path)
    view = proposed(system)
    closed = cancel(host, view)
    with pytest.raises(CaseError, match="case_revision_conflict"):
        confirm(system["cases"], view)
    assert closed["confirmation"] is None and len(closed["episodes"]) == 1


def test_queued_job_gets_not_dispatched_proof(tmp_path):
    system, host, calls, _ = setup(tmp_path)
    view = system["view"]
    job = host._job("parent", view["case_id"], "owner", "create-1")
    directory = host.root / job["job_id"]
    directory.mkdir(parents=True)
    atomic_json(directory / "intent.json", job)
    assert cancel(host, view)["status"] == "closed"
    assert read_json(directory / "closure.json")["outcome"] == "not_dispatched"
    host._run_serial(directory)
    assert not (directory / "started.json").exists() and calls == []


def test_original_unknown_worker_only_reconciles_actual_receipt(tmp_path):
    calls = []
    def fail(**kwargs):
        calls.append(1)
        raise RuntimeError("lost original worker")
    system, host, _, _ = setup(tmp_path, runner=fail)
    identity = system["view"]["case_id"]
    job = host.schedule("parent", identity, "owner", "create-1")
    assert wait(host, job)["status"] == "reconciliation_required"
    blocked = cancel(host, system["cases"].get(identity, "owner"))
    assert blocked["status"] == "closing" and blocked["usage_complete"] is False
    assert blocked["closure_intent"]["blockers"] == ["worker_result_unknown"]
    with pytest.raises(CaseError, match="environment_chain_active"):
        system["cases"].create("owner", operation_id="next", template=task_template(system["contract"]),
                               policy=system["policy"], event=EVENT)
    closed = host.reconcile(job["job_id"], recover_worker=lambda claim, root: {
        "original_worker_verified": True, "quiescence": "confirmed",
        "usage": {"tokens": 123, "seconds": 2, "queries": 0, "validations": 0, "prepares": 0}})
    assert closed["status"] == "closed" and closed["usage"]["tokens"] == 123
    assert calls == [1] and closed["ticket"] is None


def test_completed_worker_receipt_after_expiry_does_not_make_ticket(tmp_path, monkeypatch):
    system, host, calls, clock = setup(tmp_path, expires=True)
    identity = system["view"]["case_id"]
    original = system["cases"].finish_work
    monkeypatch.setattr(system["cases"], "finish_work", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    job = host.schedule("parent", identity, "owner", "create-1")
    assert wait(host, job)["status"] == "reconciliation_required"
    monkeypatch.setattr(system["cases"], "finish_work", original)
    clock[0] = 200
    closed = host.expire(identity, "owner")
    assert closed["status"] == "closed" and closed["usage"]["seconds"] == 0.1
    assert closed["ticket"] is None and len(calls) == 1
    late = system["cases"].record_terminal(identity, "owner", system["view"]["current_episode"])
    assert late["status"] == "closed" and late["ticket"] is None
    assert late["episodes"][0]["status"] == "needs_clarification"


def test_running_worker_late_callback_stays_closed(tmp_path):
    system, host, calls, _ = setup(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = host.coordinator.episode_runner
    def run(**kwargs):
        started.set()
        assert release.wait(3)
        return original(**kwargs)
    host.coordinator.episode_runner = run
    identity = system["view"]["case_id"]
    job = host.schedule("parent", identity, "owner", "create-1")
    assert started.wait(2)
    try:
        assert cancel(host, system["cases"].get(identity, "owner"))["status"] == "closing"
    finally:
        release.set()
    wait(host, job)
    closed = system["cases"].get(identity, "owner")
    assert closed["status"] == "closed" and closed["ticket"] is None
    assert closed["usage_complete"] and len(calls) == 1


def test_partial_usage_cannot_be_refunded_or_release(tmp_path):
    system, host, _, _ = setup(tmp_path)
    cases, identity = system["cases"], system["view"]["case_id"]
    cases.reserve_work(identity, "owner", operation_id="original", kind="parent",
                       allowance={key: 1 for key in RESOURCE_FIELDS})
    cases.finish_work(identity, "owner", operation_id="original",
                      usage={key: None if key == "tokens" else 0 for key in RESOURCE_FIELDS})
    blocked = cancel(host, cases.get(identity, "owner"))
    assert blocked["closure_intent"]["blockers"] == ["case_usage_unknown"]
    assert blocked["usage"]["tokens"] is None and blocked["environment_released"] is None


def test_maintenance_finds_expiry_without_read_mutation_or_new_worker(tmp_path):
    system, host, calls, clock = setup(tmp_path, expires=True)
    waiting(system)
    clock[0] = 200
    assert host.expire_pending()[0]["status"] == "closed"
    assert host.expire_pending() == [] and calls == []


def test_closure_receipt_restart_completes_release_without_rewriting(tmp_path, monkeypatch):
    import forgeflow.investigation.cases as module
    system, host, _, _ = setup(tmp_path)
    view = waiting(system)
    original = module.atomic_json
    def crash(path, value, **kwargs):
        if path.name == "environment.json":
            raise OSError("crash after receipt")
        return original(path, value, **kwargs)
    monkeypatch.setattr(module, "atomic_json", crash)
    closed = cancel(host, view)
    assert closed["environment_released"] is None and closed["status"] == "closing"
    assert read_json(system["cases"].root / "environment.json")["active_case"] == view["case_id"]
    before = closed["closure_intent"]["receipt"]
    monkeypatch.setattr(module, "atomic_json", original)
    resumed = host.reconcile_closure(view["case_id"], "owner")
    assert resumed["closure_intent"]["receipt"] == before
    assert read_json(system["cases"].root / "environment.json")["active_case"] is None


def test_crash_after_claim_clear_before_final_receipt_never_clears_new_case(tmp_path, monkeypatch):
    system, host, _, _ = setup(tmp_path)
    view = waiting(system)
    cases = system["cases"]
    write = cases._write
    def crash(state):
        if state.get("environment_released"):
            raise OSError("crash before final aggregate receipt")
        return write(state)
    monkeypatch.setattr(cases, "_write", crash)
    pending = cancel(host, view)
    assert pending["status"] == "closing" and pending["environment_released"] is None
    assert pending["closure_intent"]["receipt"]
    assert read_json(cases.root / "environment.json")["active_case"] is None
    monkeypatch.setattr(cases, "_write", write)
    newer = cases.create("owner", operation_id="next", template=task_template(system["contract"]),
                         policy=system["policy"], event=EVENT)
    resumed = host.reconcile_closure(view["case_id"], "owner")
    assert resumed["status"] == "closed" and resumed["environment_released"]
    assert resumed["closure_intent"]["receipt"] == pending["closure_intent"]["receipt"]
    assert read_json(cases.root / "environment.json")["active_case"] == newer["case_id"]


def test_late_runtime_failure_preserves_closure_receipt_and_diagnostic(tmp_path):
    system, host, _, _ = setup(tmp_path)
    cases, identity = system["cases"], system["view"]["case_id"]
    cases.reserve_work(identity, "owner", operation_id="original", kind="parent",
                       allowance={key: 1 for key in RESOURCE_FIELDS})
    cases.finish_work(identity, "owner", operation_id="original", usage=dict.fromkeys(RESOURCE_FIELDS, 0))
    closed = cancel(host, cases.get(identity, "owner"))
    late = cases.reject_runtime(identity, "owner", operation_id="original")
    assert late["status"] == "closed" and late["terminal"] == closed["terminal"]
    assert read_json(cases._directory(identity) / "case.json")["runtime_failure"]["operation_id"] == "original"
    assert host.reconcile_closure(identity, "owner")["closure_intent"]["receipt"] == closed["closure_intent"]["receipt"]


def test_generation_changes_after_receipt_before_release_stays_blocked(tmp_path, monkeypatch):
    system, host, _, _ = setup(tmp_path)
    view = waiting(system)
    cases = system["cases"]
    write = cases._write
    def change_generation(state):
        write(state)
        if (state.get("closure_intent") or {}).get("phase") == "releasing":
            system["environment"]["generation"] = "generation-2"
    monkeypatch.setattr(cases, "_write", change_generation)
    blocked = cancel(host, view)
    assert blocked["status"] == "closing" and blocked["environment_released"] is None
    assert blocked["closure_intent"]["blockers"] == ["environment_generation_changed"]
    assert read_json(cases.root / "environment.json")["active_case"] == view["case_id"]


def test_close_wins_after_worker_claim_but_before_dispatch(tmp_path, monkeypatch):
    system, host, calls, _ = setup(tmp_path)
    cases, identity = system["cases"], system["view"]["case_id"]
    dispatch = cases.dispatch_worker
    def close_before_admission(case_id, principal, *, operation_id):
        current = cases.get(case_id, principal)
        cases.request_closure(case_id, principal, operation_id="close-at-boundary",
                              expected_revision=current["revision"])
        return dispatch(case_id, principal, operation_id=operation_id)
    monkeypatch.setattr(cases, "dispatch_worker", close_before_admission)
    current = host.coordinator.run_episode(identity, "owner")
    assert current["status"] == "closing" and current["usage_complete"]
    work = cases._directory(identity) / "workers" / ("episode-" + current["current_episode"])
    result = read_json(work / "result.json")
    assert result["not_dispatched"] is True and result["usage"] == dict.fromkeys(RESOURCE_FIELDS, 0)
    assert calls == [] and host.reconcile_closure(identity, "owner")["status"] == "closed"


def test_reserved_without_claim_has_explicit_unstarted_proof(tmp_path):
    system, host, calls, _ = setup(tmp_path)
    cases, identity = system["cases"], system["view"]["case_id"]
    cases.reserve_work(identity, "owner", operation_id="original", kind="parent",
                       allowance={key: 1 for key in RESOURCE_FIELDS})
    closed = cancel(host, cases.get(identity, "owner"))
    proof = read_json(cases._directory(identity) / "workers" / "original" / "closure.json")
    assert proof["outcome"] == "not_dispatched"
    assert closed["status"] == "closed" and closed["usage_complete"] and calls == []


@pytest.mark.parametrize("first_reason", ["policy_expired", "user_cancelled"])
def test_expiry_cancel_race_keeps_first_reason_and_blocks_stale_cas(tmp_path, first_reason):
    system, host, _, clock = setup(tmp_path, expires=True)
    view = waiting(system)
    clock[0] = 200
    cases = system["cases"]
    cases.request_closure(view["case_id"], "owner", operation_id="winner", expected_revision=view["revision"], reason=first_reason)
    with pytest.raises(CaseError, match="case_revision_conflict"):
        cases.request_closure(view["case_id"], "owner", operation_id="loser", expected_revision=view["revision"],
                              reason="user_cancelled" if first_reason == "policy_expired" else "policy_expired")
    assert host.reconcile_closure(view["case_id"], "owner")["closure_intent"]["reason"] == first_reason


def test_late_reply_callback_never_reopens_closed_case(tmp_path):
    system, host, _, _ = setup(tmp_path)
    view = waiting(system)
    cases, identity = system["cases"], view["case_id"]
    reply = cases.submit_reply(identity, "owner", operation_id="reply", expected_revision=view["revision"], text="800 USD")
    closed = cancel(host, cases.get(identity, "owner"))
    late = cases.propose(identity, "owner", reply_id=reply["reply_id"], proposal={
        "kind": "budget_authorization_proposal", "order_id": "30", "amount": "800", "currency": "USD"})
    assert late == closed and late["draft"] is None and late["confirmation"] is None


def test_authorized_claim_then_config_rejected_proves_no_runner_called(tmp_path, monkeypatch):
    system, host, calls, _ = setup(tmp_path)
    cases, identity = system["cases"], system["view"]["case_id"]
    original = cases.dispatch_worker
    def close_after_admission(case_id, principal, *, operation_id):
        decision = original(case_id, principal, operation_id=operation_id)
        assert decision["authorized"]
        view = cases.get(case_id, principal)
        cases.request_closure(case_id, principal, operation_id="close-after-admission", expected_revision=view["revision"])
        return decision
    monkeypatch.setattr(cases, "dispatch_worker", close_after_admission)
    view = host.coordinator.run_episode(identity, "owner")
    assert calls == [] and view["usage_complete"]
    assert view["usage"] == dict.fromkeys(RESOURCE_FIELDS, 0)
    assert host.reconcile_closure(identity, "owner")["status"] == "closed"


def test_configured_original_worker_recovery_used_by_closure_without_argument(tmp_path):
    calls = []
    def fail(**kwargs):
        calls.append(1)
        raise RuntimeError("original exited before receipt")
    system, host, _, _ = setup(tmp_path, runner=fail)
    identity = system["view"]["case_id"]
    job = host.schedule("parent", identity, "owner", "create-1")
    wait(host, job)
    recovered = []
    def recover(claim, directory):
        recovered.append(claim["operation_id"])
        return {"original_worker_verified": True, "quiescence": "confirmed",
                "usage": {"tokens": 81, "seconds": 1, "queries": 0, "validations": 0, "prepares": 0}}
    host.recover_worker = recover
    closed = cancel(host, system["cases"].get(identity, "owner"))
    assert closed["status"] == "closed" and closed["usage"]["tokens"] == 81
    assert len(recovered) == 1 and calls == [1]


def test_denied_dispatch_survives_crash_before_worker_result(tmp_path, monkeypatch):
    import forgeflow.investigation.continuation as module
    system, host, calls, _ = setup(tmp_path)
    cases, identity = system["cases"], system["view"]["case_id"]
    dispatch = cases.dispatch_worker
    def deny(case_id, principal, *, operation_id):
        view = cases.get(case_id, principal)
        cases.request_closure(case_id, principal, operation_id="close-before-dispatch", expected_revision=view["revision"])
        return dispatch(case_id, principal, operation_id=operation_id)
    monkeypatch.setattr(cases, "dispatch_worker", deny)
    save = module.atomic_json
    def crash(path, value, **kwargs):
        if path.name == "result.json":
            raise OSError("crash before result")
        return save(path, value, **kwargs)
    monkeypatch.setattr(module, "atomic_json", crash)
    with pytest.raises(OSError, match="crash before result"):
        host.coordinator.run_episode(identity, "owner")
    host.recover_worker = lambda *a: pytest.fail("durable denial needs no runtime or provider recovery")
    closed = host.reconcile_closure(identity, "owner")
    assert closed["status"] == "closed" and closed["usage"] == dict.fromkeys(RESOURCE_FIELDS, 0)
    path = cases._directory(identity) / "workers" / ("episode-" + closed["current_episode"])
    assert read_json(path / "closure.json")["claim_sha256"]
    assert not (path / "runtime.json").exists() and calls == []
