"""Authenticated continuation API boundaries, with real case state and no model."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from investigation_support import EVENT, make_case

from forgeflow.api.app import create_app
from forgeflow.api.auth import verify_cookie
from forgeflow.api.investigation import InvestigationAdapter
from forgeflow.api.settings import create_settings
from forgeflow.investigation.cases import InvestigationCases
from forgeflow.investigation.clarification import interpret_reply
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import PartialPolicy, policy_from_payload
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import FixtureSource
from forgeflow.recovery.storage import scope_lock

INVITE = "investigation-owner-invite"
OTHER = "investigation-other-invite"


@pytest.fixture
def api_system(tmp_path):
    source, contract, _ = make_case()
    full = contract.payload()
    policy = policy_from_payload({"kind": "forgeflow.investigation_policy", "schema_version": 1,
        "policy_id": "intake", "task_id": full["task_id"],
        **{k: full[k] for k in ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")},
        "orders": {"30": {"authorized": True, "budget_cap": None}}, "sources": {"policy": "host"}})
    def factory(value):
        return FixtureSource(source.records, value.source_policy() if isinstance(value, PartialPolicy) else value)
    cases = InvestigationCases(tmp_path / "cases", source_factory=factory, generation=lambda: "generation-1",
        currency_probe=lambda _: {"currency": "USD", "company_id": 1, "provenance": "fixed-fixture",
                                 "record_currencies": {"orders-and-offers": "USD"}})
    calls = []
    def schedule(action, case_id, principal, operation_id):
        calls.append((action, case_id, principal, operation_id))
        view = cases.get(case_id, principal)
        if action == "parent" and view["episodes"][0]["status"] == "pending":
            config = cases.episode_config(case_id, principal, view["current_episode"])
            service = InvestigationService(source=factory(policy), contract=policy, store=config["store"],
                bundle=tmp_path / "unused", connection_factory=lambda: None, event=EVENT)
            service.dispatch(config["run_id"], "finish", {"status": "needs_clarification",
                "reason": "Budget is missing", "question": "What budget do you authorize for SO-30?"})
            cases.record_terminal(case_id, principal, config["run_id"])
        if action == "reply":
            with scope_lock(cases.root):
                raw = cases._read(case_id, principal)
                reply_id = raw["operations"][operation_id]["receipt"]["reply_id"]
                reply = next(row for row in raw["replies"] if row["reply_id"] == reply_id)
            proposal = interpret_reply(view["ticket"], reply["text"], view["currency"])
            cases.propose(case_id, principal, reply_id=reply_id, proposal=proposal)
    def create(principal, operation_id):
        view = cases.create(principal, operation_id=operation_id, template=task_template(contract), policy=policy, event=EVENT)
        schedule("parent", view["case_id"], principal, operation_id)
        return cases.get(view["case_id"], principal)
    adapter = InvestigationAdapter(cases=cases, create_case=create, schedule=schedule)
    settings = create_settings({"FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_SESSION_SECRET": "test-investigation-session-secret-0123456789",
        "FORGEFLOW_INVITE_CODES": INVITE, "FORGEFLOW_SECURE_COOKIE": "true"})
    settings = replace(settings, invite_codes=(INVITE, OTHER))
    return settings, adapter, calls


def login(client, code=INVITE):
    assert client.post("/api/auth/session", json={"invite_code": code}).status_code == 201


def create(client):
    result = client.post("/api/investigation/cases", json={"operation_id": "create-1"})
    assert result.status_code == 201, result.text
    return result.json()


def reply(client, view, text="SO-30 budget 800 USD", operation="reply-1"):
    result = client.post(f"/api/investigation/cases/{view['case_id']}/reply", json={
        "operation_id": operation, "expected_revision": view["revision"], "text": text})
    assert result.status_code == 200, result.text
    return result.json()


def confirmation(view):
    return {"operation_id": "confirm-1", "expected_revision": view["revision"],
        "draft_sha256": view["draft"]["draft_sha256"],
        "parent_policy_sha256": view["ticket"]["parent_policy_sha256"], "confirmed": True}


def test_actual_api_reply_explicit_confirmation_and_idempotent_child_intent(api_system):
    settings, adapter, calls = api_system
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        assert client.get("/api/config").json()["workbenches"] == {"investigation": True, "recovery_review": False}
        assert client.get("/api/investigation/cases").status_code == 401
        login(client)
        view = create(client)
        assert view["status"] == "awaiting_human"
        assert create(client)["case_id"] == view["case_id"]
        view = reply(client, view)
        assert view["draft"]["proposal"]["amount"] == "800.00"
        assert view["confirmation"] is None and len(view["episodes"]) == 1
        body = confirmation(view)
        endpoint = f"/api/investigation/cases/{view['case_id']}/confirm"
        assert client.post(endpoint, json={**body, "confirmed": False}).status_code == 400
        first = client.post(endpoint, json=body)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "continuation_pending"
        assert len(first.json()["episodes"]) == 2
        assert client.post(endpoint, json=body).json() == first.json()
        assert [row[3] for row in calls if row[0] == "child"] == ["confirm-1", "confirm-1"]
        # The host scheduler owns dispatch claims; these are repeated enqueue
        # requests for the same durable intent, not two model launches.


def test_api_never_accepts_actor_policy_template_or_generation_from_the_browser(api_system):
    settings, adapter, _ = api_system
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        login(client)
        for field in ("actor", "principal", "policy", "template", "generation", "contract", "binding"):
            response = client.post("/api/investigation/cases", json={"operation_id": "create-1", field: "forged"})
            assert response.status_code == 422
        assert client.get("/api/investigation/cases").json() == {"cases": []}


def test_other_authenticated_owner_cannot_list_read_or_change_case(api_system):
    settings, adapter, _ = api_system
    app = create_app(settings, investigation_adapter=adapter)
    with TestClient(app, base_url="https://testserver") as first, TestClient(app, base_url="https://testserver") as other:
        login(first)
        view = create(first)
        login(other, OTHER)
        assert other.get("/api/investigation/cases").json() == {"cases": []}
        path = f"/api/investigation/cases/{view['case_id']}"
        assert other.get(path).status_code == 404
        assert other.post(path + "/reply", json={"operation_id": "reply", "expected_revision": view["revision"], "text": "800"}).status_code == 404
        assert other.post(path + "/handoff", json={"operation_id": "handoff", "expected_revision": view["revision"]}).status_code == 404


def test_prior_workspace_cannot_start_an_investigation_with_a_replaced_cookie(api_system):
    settings, adapter, calls = api_system
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        login(client)
        scope = client.get("/api/auth/me").json()["scope_id"]
        login(client, OTHER)
        headers = {"X-ForgeFlow-Workspace": scope}
        for response in (
            client.get("/api/investigation/cases", headers=headers),
            client.post("/api/investigation/cases", json={"operation_id": "stale-create"}, headers=headers),
        ):
            assert response.status_code == 409
            assert response.headers["X-ForgeFlow-Workspace-Changed"] == "1"
        assert calls == []
        assert client.get("/api/investigation/cases").json() == {"cases": []}


def test_stale_confirmation_and_cross_origin_mutation_are_refused(api_system):
    settings, adapter, _ = api_system
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        login(client)
        view = reply(client, create(client))
        old = confirmation(view)
        latest = reply(client, view, "Change to 900 USD", "reply-2")
        assert latest["draft"]["proposal"]["amount"] == "900.00"
        endpoint = f"/api/investigation/cases/{view['case_id']}/confirm"
        assert client.post(endpoint, json=old).status_code == 409
        assert client.post(endpoint, json=confirmation(latest), headers={"Origin": "https://untrusted.example"}).status_code == 403
        assert client.get(f"/api/investigation/cases/{view['case_id']}").json()["confirmation"] is None


def test_schedule_failure_retains_operation_receipt_and_safe_retry(api_system):
    settings, adapter, _ = api_system
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        login(client)
        view = create(client)
        original = adapter.schedule
        adapter.schedule = lambda *_: (_ for _ in ()).throw(RuntimeError("private-host-path"))
        endpoint = f"/api/investigation/cases/{view['case_id']}/reply"
        payload = {"operation_id": "reply-1", "expected_revision": view["revision"], "text": "Budget 800"}
        response = client.post(endpoint, json=payload)
        assert response.status_code == 503
        assert "private-host-path" not in response.text
        adapter.schedule = original
        replay = client.post(endpoint, json=payload)
        assert replay.status_code == 200
        assert len(replay.json()["replies"]) == 1
        assert replay.json()["draft"]["proposal"]["amount"] == "800.00"


def test_review_and_unconfigured_servers_do_not_start_continuations(api_system):
    settings, adapter, _ = api_system
    for configured, mode, expected in [(None, "local", 503), (adapter, "review", 403)]:
        with TestClient(create_app(replace(settings, deployment=mode), investigation_adapter=configured), base_url="https://testserver") as client:
            login(client)
            assert client.post("/api/investigation/cases", json={"operation_id": "create"}).status_code == expected


def test_four_region_ui_is_same_origin_and_exposes_no_policy_editor(api_system):
    settings, adapter, _ = api_system
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url="https://testserver") as client:
        page = client.get("/investigation")
        assert page.status_code == 200
        for section in ("question-title", "confirmation-title", "history-title", "handoff-title"):
            assert f'id="{section}"' in page.text
        script = client.get("/static/investigation.js").text
        assert 'credentials:"same-origin"' in script and "innerHTML" not in script
        assert '"confirm-check").checked' in script and "draft_sha256" in script
        assert '/#case=' in script and "sessionStorage" in script
        assert "parent_policy_sha256" not in page.text
        login(client)
        uid = verify_cookie(settings, client.cookies.get(settings.cookie_name), audience="testserver")
        assert uid is not None
