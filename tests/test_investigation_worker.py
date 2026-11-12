"""Short-lived worker identity, grant and accounting boundaries; no model calls."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from forgeflow.investigation import worker as module
from forgeflow.investigation.worker import PiCaseWorker, usage_receipt
from forgeflow.recovery.storage import atomic_json, digest


def worker(tmp_path):
    config = SimpleNamespace(base_url="http://127.0.0.1/unused", model="runtime-test", api_key="unused-local-test")
    return PiCaseWorker(config=config, source_factory=lambda _: None, connection_factory=lambda: None,
                        bundle=tmp_path / "bundle")


def identity(**changes):
    return {"name": "owned-name", "container_id": "a" * 64, "worker_id": "owner-token",
            "image_id": "image-bound", **changes}


def inspection(**changes):
    return {"Id": "a" * 64, "Image": "image-bound", "State": {"Running": False},
            "Config": {"Labels": {"forgeflow.worker": "owner-token"}, "User": "1000:1000"},
            "Mounts": [], **changes}


@pytest.mark.parametrize("change", [
    {"Id": "b" * 64}, {"Image": "other-image"}, {"Config": {"Labels": {"forgeflow.worker": "other"}}},
])
def test_identity_mismatch_is_never_stopped_copied_or_removed(tmp_path, monkeypatch, change):
    w = worker(tmp_path)
    calls = []

    def docker(*args, **_):
        calls.append(args)
        assert args[0] == "inspect"
        return json.dumps([inspection(**change)])

    monkeypatch.setattr(w, "docker", docker)
    runtime = identity()
    w._settle_container(runtime, tmp_path)
    assert runtime["container_quiescence"] == "unknown"
    assert all(call[0] == "inspect" for call in calls)


def test_failed_log_export_keeps_original_stopped_container(tmp_path, monkeypatch):
    w = worker(tmp_path)
    calls = []

    def docker(*args, **_):
        calls.append(args)
        if args[0] == "inspect":
            return json.dumps([inspection()])
        if args[0] == "cp":
            raise RuntimeError("copy_failed")
        pytest.fail("An original container with uncopied logs must not be removed")

    monkeypatch.setattr(w, "docker", docker)
    runtime = identity()
    w._settle_container(runtime, tmp_path)
    assert runtime["container_quiescence"] == "confirmed"
    assert not runtime.get("container_removed")
    assert not runtime.get("logs_exported")
    assert next(call for call in calls if call[0] == "cp")[1].startswith("a" * 64 + ":")


def test_successful_cleanup_uses_id_and_copies_before_removal(tmp_path, monkeypatch):
    w = worker(tmp_path)
    calls = []
    running = [True]

    def docker(*args, **_):
        calls.append(args)
        if args[0] == "inspect":
            return json.dumps([inspection(State={"Running": running[0]})])
        if args[0] == "stop":
            running[0] = False
        return ""

    monkeypatch.setattr(w, "docker", docker)
    runtime = identity()
    w._settle_container(runtime, tmp_path)
    assert runtime["logs_exported"] and runtime["container_removed"]
    assert [c[0] for c in calls] == ["inspect", "stop", "inspect", "cp", "inspect", "rm"]
    assert "owned-name" not in str(calls)


def test_expired_setup_grant_never_dispatches_even_one_provider_process(tmp_path, monkeypatch):
    w = worker(tmp_path)
    times = iter([0, 2, 3])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    server = SimpleNamespace(server_port=12345, dispatch_lock=threading.Lock(), shutdown=lambda: None,
                             server_close=lambda: None)
    monkeypatch.setattr(module, "start_bridge", lambda *_: (server, "local-bridge"))
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: pytest.fail("Expired grant dispatched a model"))

    def docker(*args, **_):
        if args[0] == "create":
            return "a" * 64
        if args[0] == "inspect":
            runtime = json.loads((tmp_path / "work/runtime.json").read_text())
            return json.dumps([inspection(Config={"User": "1000:1000", "Labels": {
                "forgeflow.worker": runtime["worker_id"]}})])
        if args[0] == "exec" and "cat" in args:
            return "100 1"
        if args[0] == "exec" and "--input-type=module" in args:
            return "[]"
        return ""

    monkeypatch.setattr(w, "docker", docker)
    runtime, receipt = w._model(directory=tmp_path / "work", allowance={"seconds": 1, "tokens": 2000000},
                                mode="reply", prompt="local", dispatch=lambda *_: {}, strategy="fixed")
    assert runtime["dispatch_started"] is False
    assert receipt["tokens"] == 0 and receipt["status"] == "no_provider_called"
    assert runtime["error"]["type"] == "RuntimeError"


def test_rules_reply_receives_the_coordinators_currency_string(tmp_path):
    result = worker(tmp_path).reply(ticket={"kind": "set_missing_order_budget", "status": "open",
        "order_id": "1", "order_name": "SO-1", "question": "What budget is authorized?"},
        reply={"text": "I authorize a budget of 1000 USD for SO-1."}, currency="USD", history=[],
        strategy="rules", allowance={}, work_directory=tmp_path)
    assert result["proposal"]["amount"] == "1000.00"
    assert result["usage"]["tokens"] == 0


@pytest.mark.parametrize("budget", [[], {"provider_requests": 1, "completed_responses": 0,
    "reported_total_tokens": 0, "in_flight": True, "partial_usage_unknown": True}])
def test_malformed_or_partial_usage_stays_unknown(tmp_path, budget):
    (tmp_path / "agent-logs").mkdir()
    (tmp_path / "agent-logs/token-budget.json").write_text(json.dumps(budget))
    (tmp_path / "pi.jsonl").write_text('[]\n{"type":"message_end","message":[]}\n')
    assert usage_receipt(tmp_path)["tokens"] is None


def test_complete_crash_receipt_recovery_is_bound_and_never_redispatches(tmp_path, monkeypatch):
    from test_investigation_service import RUN_ID, setup_service

    case = tmp_path / "case"
    directory = case / "workers/operation-1"
    directory.mkdir(parents=True)
    service, _, _ = setup_service(tmp_path / "source")
    service.store = case / "episodes" / RUN_ID / "store"
    service.root = service.store / "investigation"
    service.dispatch(RUN_ID, "finish", {"status": "needs_clarification", "reason": "budget_missing",
                                       "question": "What budget cap is authorized?"})
    binding = {"run_id": RUN_ID, "policy_sha256": service.policy.digest(),
               "event_sha256": service.event["sha256"]}
    claim = {"kind": "parent", "episode_id": RUN_ID,
             "input_sha256": digest({"episode_id": RUN_ID, "policy_sha256": service.policy.digest()})}
    runtime = identity(mode="episode", phase="finished", started_at=time.time() - 2,
        dispatch_started=True, claim_sha256=digest(claim), business_binding=binding,
        runtime_valid=True, container_removed=True, quiescence="confirmed", container_quiescence="confirmed")
    atomic_json(directory / "claim.json", claim)
    atomic_json(directory / "runtime.json", runtime)
    (directory / "agent-logs").mkdir()
    atomic_json(directory / "agent-logs/token-budget.json", {"provider_requests": 1, "completed_responses": 1,
        "reported_total_tokens": 100, "in_flight": False, "partial_usage_unknown": False, "stop_reason": None})
    (directory / "pi.jsonl").write_text(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "toolUse", "usage": {"totalTokens": 100}}}) + "\n")
    atomic_json(directory / "expected-registry.json", [{"name": "only-tool"}])
    (directory / "agent-logs/case-registry.jsonl").write_text(json.dumps({"active": ["only-tool"],
        "expected": ["only-tool"], "registered": [{"name": "only-tool"}]}) + "\n")
    w = worker(tmp_path)
    calls = []

    def docker(*args, **_):
        calls.append(args)
        assert args[0] == "ps"
        return ""

    monkeypatch.setattr(w, "docker", docker)
    outcome = w.recover(claim, directory)
    assert outcome["original_worker_verified"] and outcome["runtime_valid"]
    assert outcome["usage"]["tokens"] == 100
    assert outcome["status"] == "needs_clarification"
    assert "not_model_active_time" in outcome["wall_seconds_basis"]
    assert w.recover(claim, directory) == outcome
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match="identity_unproven"):
        w.recover({**claim, "kind": "child"}, directory)
