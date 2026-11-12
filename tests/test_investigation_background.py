"""Owner-only background failure visibility, without changing case accounting."""

import threading

import pytest
from fastapi.testclient import TestClient
from test_continuation_host import setup, wait
from test_investigation_api import (
    OTHER,
    confirmation,
    create,
    login,
    reply,
)
from test_investigation_api import (
    api_system as api_system,
)

from forgeflow.api.app import create_app
from forgeflow.api.auth import verify_cookie
from forgeflow.investigation.cases import CaseError
from forgeflow.investigation.host import ContinuationHost
from forgeflow.recovery.storage import atomic_json, read_json


def save_intent(host, job, *, started=False, result=None):
    directory = host.root / job["job_id"]
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "intent.json", job)
    if started:
        atomic_json(directory / "started.json", {"intent_sha256": "opaque", "pid": 1})
    if result:
        atomic_json(directory / "result.json", result)
    return directory


def test_queued_live_running_and_restart_unknown_are_distinct(tmp_path):
    system, host, calls = setup(tmp_path)
    identity = system["view"]["case_id"]
    job = host._job("parent", identity, "owner", "create-1")
    directory = save_intent(host, job)
    assert host.background(identity, "owner")["status"] == "queued"
    atomic_json(directory / "started.json", {"intent_sha256": "opaque", "pid": 1})
    assert host.background(identity, "owner")["requires_reconciliation"] is True
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(3))
    host._threads[job["job_id"]] = thread
    thread.start()
    try:
        value = host.background(identity, "owner")
        assert value["status"] == "running" and not value["requires_reconciliation"]
        restarted = ContinuationHost(cases=host.cases, coordinator=host.coordinator, product=None, create_inputs=lambda _: {})
        assert restarted.background(identity, "owner")["status"] == "reconciliation_required"
    finally:
        release.set()
        thread.join(3)
    assert calls == []


def test_actual_failed_worker_visible_without_case_usage_mutation(tmp_path):
    def fail(**kwargs):
        raise RuntimeError("/Users/private/api-key-never-expose")

    system, host, calls = setup(tmp_path, runner=fail)
    identity = system["view"]["case_id"]
    job = host.schedule("parent", identity, "owner", "create-1")
    assert wait(host, job)["status"] == "reconciliation_required"
    path = host.cases._directory(identity) / "case.json"
    before = path.read_bytes()
    value = host.background(identity, "owner")
    assert value == {"status": "reconciliation_required", "requires_reconciliation": True,
                     "jobs": [{"action": "parent", "status": "reconciliation_required", "reason": "host_action_failed"}]}
    assert path.read_bytes() == before and calls == []
    with pytest.raises(CaseError, match="case_not_found"):
        host.background(identity, "other")


def test_old_reply_failure_and_superseded_result_do_not_block_current_reply(tmp_path):
    system, host, _ = setup(tmp_path)
    identity, cases = system["view"]["case_id"], system["cases"]
    wait(host, host.schedule("parent", identity, "owner", "create-1"))
    view = cases.get(identity, "owner")
    cases.submit_reply(identity, "owner", operation_id="reply-old", expected_revision=view["revision"], text="800 USD")
    old = host._job("reply", identity, "owner", "reply-old")
    save_intent(host, old, started=True, result={"status": "reconciliation_required", "error": "worker_result_unknown"})
    view = cases.get(identity, "owner")
    cases.submit_reply(identity, "owner", operation_id="reply-new", expected_revision=view["revision"], text="900 USD instead")
    new = host._job("reply", identity, "owner", "reply-new")
    directory = save_intent(host, new)
    assert host.background(identity, "owner") == {"status": "queued", "requires_reconciliation": False,
                                                "jobs": [{"action": "reply", "status": "queued", "reason": None}]}
    atomic_json(directory / "result.json", {"status": "superseded"})
    assert host.background(identity, "owner")["status"] == "idle"


def test_detail_and_mutations_expose_safe_background_and_refuse_resume(api_system, monkeypatch):
    settings, adapter, _ = api_system
    host = ContinuationHost(cases=adapter.cases, coordinator=None, product=None, create_inputs=lambda _: {})
    adapter.background = host.background
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        login(client)
        view = reply(client, create(client))
        assert view["background"]["status"] == "idle"
        owner = verify_cookie(settings, client.cookies.get(settings.cookie_name), audience="testserver")
        monkeypatch.setattr(host, "_perform", lambda job: (_ for _ in ()).throw(RuntimeError("/Users/private/key")))
        job = host.schedule("parent", view["case_id"], owner, "create-1")
        assert wait(host, job)["status"] == "reconciliation_required"
        path = f"/api/investigation/cases/{view['case_id']}"
        response = client.get(path)
        assert response.status_code == 200 and response.json()["background"]["requires_reconciliation"]
        assert response.json()["status"] == "awaiting_human"
        assert "/Users/" not in response.text and "job_id" not in response.text
        before = (adapter.cases._directory(view["case_id"]) / "case.json").read_bytes()
        for suffix, body in [("confirm", confirmation(view)), ("handoff", {"operation_id": "handoff", "expected_revision": view["revision"]}),
                             ("reply", {"operation_id": "retry", "expected_revision": view["revision"], "text": "900"})]:
            result = client.post(path + "/" + suffix, json=body)
            assert result.status_code == 409 and result.json()["detail"] == "investigation_reconciliation_required"
        assert (adapter.cases._directory(view["case_id"]) / "case.json").read_bytes() == before
        login(client, OTHER)
        assert client.get(path).status_code == 404
        assert client.post(path + "/confirm", json=confirmation(view)).status_code == 404


def test_mutation_response_includes_host_queue_and_pending_reply_is_retained(api_system):
    settings, adapter, _ = api_system
    host = ContinuationHost(cases=adapter.cases, coordinator=None, product=None, create_inputs=lambda _: {})
    adapter.background = host.background
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        login(client)
        view = create(client)
        def enqueue_only(action, case_id, principal, operation_id):
            save_intent(host, host._job(action, case_id, principal, operation_id))
        adapter.schedule = enqueue_only
        result = reply(client, view, "SO-30 budget 800 USD")
        assert result["background"]["status"] == "queued"
        assert result["background"]["requires_reconciliation"] is False
        assert result["replies"][-1]["text"] == "SO-30 budget 800 USD"
        raw = read_json(adapter.cases._directory(result["case_id"]) / "case.json")
        assert "reply-1" in raw["operations"] and raw["confirmation"] is None
