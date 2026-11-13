"""ERP rules and plan endpoints authorize before reading facts or writing ERP."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.superagent.identity import Role, TenantContext


@pytest.fixture
def erp_access(monkeypatch):
    from forgeflow.integrations import odoo20
    from forgeflow.superagent import bindings, execution, store, tasks

    state = SimpleNamespace(
        role=Role.APPROVER, bound=True, actor=7, companies=(1,), calls=[], writes=[],
        loads=[], closes=0, company_error=False, approval_identity="",
        attention_state="open", answers=0, events=[])
    records = {
        "forgeflow.business_rule": [
            {"id": 10, "company_id": [1, "Allowed"], "state": "draft", "name": "Allowed",
             "version": 1, "scope_model": "mrp.production", "active": True},
            {"id": 11, "company_id": [2, "Other"], "state": "draft", "name": "Other",
             "version": 1, "scope_model": "mrp.production", "active": True}],
        "forgeflow.recovery_plan": [
            {"id": plan, "company_id": [company, "Company"], "tenant_ref": tenant}
            for plan, company, tenant in [
                (42, 1, "actual-tenant:company:1"), (43, 2, "actual-tenant:company:2"),
                (44, 1, "other-tenant:company:1"), (46, 1, "actual-tenant:company:2")]],
        "purchase.order": [{"id": 1, "company_id": [1, "Allowed"]},
                           {"id": 2, "company_id": [2, "Other"]}],
    }

    class Adapter:
        def __init__(self, company=0):
            self.company = company

        def for_company(self, company):
            assert company in state.companies
            return Adapter(company)

        def user_company_ids(self, actor):
            assert actor == state.actor
            if state.company_error:
                raise RuntimeError("company lookup failed")
            return state.companies

        def search_read(self, model, domain, fields, **kwargs):
            state.calls.append((model, domain, fields))

            def matches(row):
                for field, operator, value in domain:
                    actual = row.get(field)
                    if isinstance(actual, list):
                        actual = actual[0]
                    if operator == "=" and actual != value:
                        return False
                    if operator == "in" and actual not in value:
                        return False
                return True

            return [row for row in records[model] if matches(row)]

        def write(self, model, ids, values):
            state.writes.append((model, ids, values, self.company))

    class Service:
        def __init__(self, adapter):
            self.adapter = adapter

        def load_prepared(self, plan_id):
            state.loads.append(plan_id)
            return SimpleNamespace(plan_id=plan_id, exception_id=5, plan_hash="plan",
                                   snapshot_hash="snapshot", facts={"order": {"id": 1}},
                                   actions=(), execution_identity=state.approval_identity,
                                   plan_version=1, plan_digest="a" * 64, action_digest="b" * 64,
                                   expires_at="",
                                   plan_state="approved" if state.approval_identity else "awaiting_approval")

        def approve(self, prepared, *, approver_uid, comment, expires_in_hours=24):
            assert approver_uid == 7
            assert not state.approval_identity, "an existing approval must not be written again"
            state.writes.append(("approve", prepared.plan_id, self.adapter.company))
            state.approval_identity = "synthetic-approval-identity"
            return 100

    def close():
        state.closes += 1

    def principal_context(_store, *, principal_id):
        if not state.bound:
            return None
        return TenantContext("actual-tenant", principal_id, frozenset({state.role}), 1)

    def actor(_store, *, principal_id, tenant_id):
        assert tenant_id == "actual-tenant"
        return state.actor

    def plan_task(_store, *, tenant_id, plan_id):
        assert tenant_id == "actual-tenant"
        if plan_id == 45:
            return None
        return SimpleNamespace(ticket_id="tenant-task", attention_state=state.attention_state,
                               attention_id="attention", need_type="approval")

    def answered(_store, ctx, *, plan_id):
        assert ctx.tenant_id == "actual-tenant"
        assert state.attention_state == "open", "answered attention cannot be answered twice"
        state.attention_state = "answered"
        state.answers += 1
        return SimpleNamespace(ticket_id="tenant-task", attention_state="answered", need_type="approval")

    for name in ("URL", "DB", "LOGIN", "PASSWORD"):
        monkeypatch.setenv(f"FORGEFLOW_ODOO20_{name}", "synthetic")
    monkeypatch.setenv("FORGEFLOW_CONTROL_DATABASE_URL", "synthetic")
    monkeypatch.setenv("FORGEFLOW_CONTROL_TENANT_ID", "unrelated-default")
    monkeypatch.setattr(store, "ControlStore", lambda url: SimpleNamespace(migrate=lambda: None, close=close))
    monkeypatch.setattr(bindings, "principal_context", principal_context)
    monkeypatch.setattr(bindings, "odoo_user_id_for", actor)
    monkeypatch.setattr(odoo20, "Odoo20Adapter", lambda config: Adapter())
    monkeypatch.setattr(odoo20, "Odoo20BusinessService", Service)
    monkeypatch.setattr(tasks, "plan_task", plan_task)
    monkeypatch.setattr(tasks, "answer_attention", answered)
    monkeypatch.setattr(execution, "odoo_user_id_for", actor)
    monkeypatch.setattr(execution, "plan_task", plan_task)
    monkeypatch.setattr(execution, "answer_attention", answered)
    monkeypatch.setattr(execution, "ControlEngine", lambda _: SimpleNamespace(
        append_event=lambda *a, **kw: state.events.append(kw)))
    monkeypatch.setattr(execution, "request_execution", lambda *a, **kw: {
        "state": "pending", "effect_id": "single-effect"})
    return state


def _auth(client, settings):
    response = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[0]})
    return {"cookie": response.headers["set-cookie"].split(";", 1)[0]}


def _request(client, headers, endpoint, plan_id=42):
    if endpoint == "read":
        return client.get(f"/api/odoo20/plans/{plan_id}", headers=headers)
    if endpoint == "rule":
        return client.post("/api/rules/10/confirm", headers=headers)
    if endpoint == "prepare":
        return client.post("/api/odoo20/plans", json={"purchase_order_id": 1, "reason": "synthetic"}, headers=headers)
    body = {"idempotency_key": "synthetic-test-key"} if endpoint == "apply" else {}
    if endpoint == "approve":
        body = {"expected_plan_version": 1, "expected_plan_digest": "a" * 64,
                "expected_action_digest": "b" * 64}
    return client.post(f"/api/odoo20/plans/{plan_id}/{endpoint}", json=body, headers=headers)


@pytest.mark.parametrize("endpoint", ["rule", "approve", "revoke", "apply", "reconcile", "prepare"])
def test_observer_cannot_mutate_erp_before_a_late_role_check(client, settings, erp_access, endpoint):
    erp_access.role = Role.OBSERVER
    response = _request(client, _auth(client, settings), endpoint)
    assert response.status_code == 403, response.text
    assert erp_access.calls == erp_access.loads == erp_access.writes == []


@pytest.mark.parametrize("endpoint", ["rule", "approve", "revoke", "apply"])
def test_planner_cannot_authorize_erp_writes(client, settings, erp_access, endpoint):
    erp_access.role = Role.PLANNER
    response = _request(client, _auth(client, settings), endpoint)
    assert response.status_code == 403, response.text
    assert erp_access.calls == erp_access.loads == erp_access.writes == []


@pytest.mark.parametrize("endpoint", ["read", "approve", "revoke", "apply", "reconcile"])
@pytest.mark.parametrize("plan_id", [43, 44, 45, 46])
def test_plan_id_requires_own_tenant_link_and_matching_company_and_tenant_ref(
        client, settings, erp_access, endpoint, plan_id):
    if plan_id == 46:
        erp_access.companies = (1, 2)
    response = _request(client, _auth(client, settings), endpoint, plan_id)
    assert response.status_code == 404, response.text
    assert erp_access.loads == erp_access.writes == []
    if plan_id == 45:
        assert erp_access.calls == []
    else:
        # Only bounded authority metadata was read, never a plan snapshot.
        assert len(erp_access.calls) == 1
        assert erp_access.calls[0][2] == ["id", "company_id", "tenant_ref"]


@pytest.mark.parametrize("endpoint", ["rule", "read", "approve", "prepare"])
@pytest.mark.parametrize("failure", ["unbound", "no_actor", "no_companies", "lookup_failed"])
def test_erp_access_fails_closed_without_bound_authority(
        client, settings, erp_access, endpoint, failure):
    erp_access.role = Role.ADMIN
    if failure == "unbound":
        erp_access.bound = False
    elif failure == "no_actor":
        erp_access.actor = None
    elif failure == "no_companies":
        erp_access.companies = ()
    else:
        erp_access.company_error = True
    response = _request(client, _auth(client, settings), endpoint)
    assert response.status_code == (503 if failure == "lookup_failed" else 403), response.text
    assert erp_access.calls == erp_access.loads == erp_access.writes == []
    assert erp_access.closes >= 1


@pytest.mark.parametrize("role", [Role.ADMIN, Role.APPROVER])
def test_allowed_approver_confirms_rule_as_bound_actor_in_rule_company(client, settings, erp_access, role):
    erp_access.role = role
    response = _request(client, _auth(client, settings), "rule")
    assert response.status_code == 200, response.text
    model, ids, values, company = erp_access.writes[0]
    assert (model, ids, values["confirmed_by_id"], company) == ("forgeflow.business_rule", [10], 7, 1)


def test_rules_list_and_confirmation_exclude_other_companies(client, settings, erp_access):
    headers = _auth(client, settings)
    response = client.get("/api/rules", headers=headers)
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [10]
    denied = client.post("/api/rules/11/confirm", headers=headers)
    assert denied.status_code == 404, denied.text
    assert erp_access.writes == []


@pytest.mark.parametrize("role", [Role.ADMIN, Role.APPROVER])
def test_same_tenant_approver_can_approve_with_actual_actor_and_company(client, settings, erp_access, role):
    erp_access.role = role
    response = _request(client, _auth(client, settings), "approve")
    assert response.status_code == 200, response.text
    assert erp_access.writes == [("approve", 42, 1)]
    assert response.json()["task"]["attention"] == "answered"
    assert response.json()["execution"]["state"] == "pending"


def test_observer_can_read_in_scope_plan(client, settings, erp_access):
    erp_access.role = Role.OBSERVER
    response = _request(client, _auth(client, settings), "read")
    assert response.status_code == 200, response.text
    assert erp_access.loads == [42]
    assert erp_access.writes == []


def test_prepare_rejects_other_company_before_creating_metadata(client, settings, erp_access):
    erp_access.role = Role.PLANNER
    response = client.post("/api/odoo20/plans", json={"purchase_order_id": 2, "reason": "synthetic"},
                           headers=_auth(client, settings))
    assert response.status_code == 404, response.text
    assert erp_access.loads == erp_access.writes == []


def test_repeated_approval_reuses_existing_decision_and_answers_attention_once(client, settings, erp_access):
    headers = _auth(client, settings)
    first = _request(client, headers, "approve")
    second = _request(client, headers, "approve")
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["decision_id"] == 100
    assert first.json()["already_approved"] is False
    assert second.json()["already_approved"] is True
    assert second.json()["task"]["attention"] == "answered"
    assert first.json()["execution"] == second.json()["execution"]
    assert erp_access.writes == [("approve", 42, 1)]
    assert erp_access.answers == len(erp_access.events) == 1


def test_existing_approval_recovers_unanswered_attention_without_another_erp_write(
        client, settings, erp_access):
    erp_access.approval_identity = "synthetic-existing-approval"
    response = _request(client, _auth(client, settings), "approve")
    assert response.status_code == 200, response.text
    assert response.json()["already_approved"] is True
    assert response.json()["task"]["attention"] == "answered"
    assert erp_access.writes == []
    assert erp_access.answers == 1


@pytest.mark.parametrize("field,value", [
    ("expected_plan_version", 2), ("expected_plan_digest", "c" * 64),
    ("expected_action_digest", "d" * 64)])
def test_changed_review_identity_never_approves_or_enqueues(client, settings, erp_access, field, value):
    body = {"expected_plan_version": 1, "expected_plan_digest": "a" * 64,
            "expected_action_digest": "b" * 64}
    body[field] = value
    response = client.post("/api/odoo20/plans/42/approve", json=body,
                           headers=_auth(client, settings))
    assert response.status_code == 409
    assert response.json()["detail"] == "plan_review_stale"
    assert erp_access.writes == erp_access.events == []
    assert erp_access.answers == 0


def test_approval_without_review_identity_is_rejected(client, settings, erp_access):
    response = client.post("/api/odoo20/plans/42/approve", json={},
                           headers=_auth(client, settings))
    assert response.status_code == 422
    assert erp_access.writes == erp_access.loads == []


@pytest.mark.parametrize("code,status", [
    ("approval_permission_unavailable", 503), ("approver_permission_denied", 403),
    ("approval_delegation_required", 403), ("plan_review_stale", 409)])
def test_native_approval_rejections_keep_actionable_safe_reason(
        client, settings, erp_access, monkeypatch, code, status):
    from forgeflow.integrations.odoo20.adapter import Odoo20Error
    from forgeflow.superagent import execution

    def rejected(*args, **kwargs):
        raise Odoo20Error(code)
    monkeypatch.setattr(execution, "approve_plan", rejected)
    response = _request(client, _auth(client, settings), "approve")
    assert response.status_code == status
    assert response.json()["detail"] == code
    assert erp_access.writes == []
