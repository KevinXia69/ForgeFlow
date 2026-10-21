"""Recover original preparation across HTTP/Local/root using synthetic ERP facts."""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_product_cases as api_tests
import test_product_local_versions as local_tests
import test_product_versions as root_tests

from forgeflow.api.case_qa import answer_case
from forgeflow.api.evidence import public_evidence
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.recovery import client, service
from forgeflow.recovery.product_versions import ProductVersions, owner_scope

system = root_tests.system
serve = root_tests.serve
workspace = root_tests.workspace
handlers = root_tests.handlers
wired = local_tests.wired
ACTOR = local_tests.ACTOR


def record_preparations(monkeypatch):
    solve, collect = service.solve_blueprint, service.collect_snapshot
    calls = {"solve": 0, "snapshot": 0}

    def counted_solve(*args):
        calls["solve"] += 1
        return solve(*args)

    def counted_collect(*args):
        calls["snapshot"] += 1
        return collect(*args)

    monkeypatch.setattr(service, "solve_blueprint", counted_solve)
    monkeypatch.setattr(service, "collect_snapshot", counted_collect)
    return calls


@pytest.mark.parametrize("lost_at", ["before_transport", "after_root_reply", "bad_response"])
def test_explicit_retry_finishes_same_operation_without_second_solve(wired, monkeypatch, lost_at):
    cases, case_id, executor, connection, options, verifications, root = wired
    calls = record_preparations(monkeypatch)
    product = client.product
    root_reply = None

    def lost(*args, **kwargs):
        nonlocal root_reply
        assert args[2] == "prepare"
        if lost_at != "before_transport":
            root_reply = product(*args, **kwargs)
        raise client.ExecutorError("bad_response" if lost_at == "bad_response" else "unavailable")

    monkeypatch.setattr(client, "product", lost)
    pending = cases.prepare(case_id, ACTOR)
    assert pending["state"] == "preparing" and pending["plan"] is None
    assert pending["decision"]["preparation_pending"] and pending["decision"]["can_resume_preparation"]
    assert pending["reason"] == "preparation_confirmation_pending"
    assert cases.get(case_id, ACTOR) == pending
    assert cases.list_for(ACTOR) == [pending]  # reading never retries
    assert calls["solve"] == (0 if lost_at == "before_transport" else 1)
    with pytest.raises(ProductCaseError, match="plan_not_prepared"):
        cases.approve(case_id, ACTOR, "e" * 64)
    assert connection.writes == [] and verifications == []
    saved = copy.deepcopy(cases._read(case_id).preparation)
    monkeypatch.setattr(client, "product", product)
    if root_reply is not None:
        monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("saved reply must not open ERP"))
    restarted = ProductCaseService(cases.root.parent, executor)
    recovered = restarted.prepare(case_id, ACTOR)
    assert recovered["state"] == "awaiting_approval", recovered
    if root_reply is not None:
        assert recovered["plan"]["plan_id"] == root_reply["decision"]["selected_plan_id"]
        assert recovered["plan"]["prepared_at"] == root_reply["decision"]["updated_at"]
    assert calls == {"solve": 1, "snapshot": 1}
    assert restarted._read(case_id).preparation["intent"] == saved["intent"]
    assert sum(row["action"] == "preparation_requested" for row in recovered["audit"]) == 1
    assert sum(row["action"] == "prepared" for row in recovered["audit"]) == 1
    assert restarted.prepare(case_id, ACTOR) == recovered
    public = public_evidence(recovered)
    assert "preparation" not in public and "_preparation" not in public
    assert saved["intent"]["request_sha256"] not in json.dumps(public)
    assert connection.writes == [] and not list(executor.approvals.glob("approval-*.json"))
    assert ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))["version"] == 1


@pytest.mark.parametrize("crash_at", ["intent_saved", "before_case_commit", "after_case_commit"])
def test_api_process_restart_preserves_one_prepare_intent_and_audit(wired, monkeypatch, crash_at):
    cases, case_id, executor, connection, _, _, root = wired
    calls = record_preparations(monkeypatch)
    write = cases._write

    def crash(case):
        if crash_at == "before_case_commit" and case.plan is not None:
            raise KeyboardInterrupt("controlled stop before case plan commit")
        write(case)
        if crash_at == "intent_saved" or case.plan is not None:
            raise KeyboardInterrupt("controlled stop after durable case write")

    with monkeypatch.context() as patch:
        patch.setattr(cases, "_write", crash)
        with pytest.raises(KeyboardInterrupt):
            cases.prepare(case_id, ACTOR)
    saved = copy.deepcopy(cases._read(case_id).preparation)
    assert saved and saved["intent"]["operation_id"] == "prepare-v1"
    if crash_at != "intent_saved":
        monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("no ERP on replay"))
    recovered = ProductCaseService(cases.root.parent, executor).prepare(case_id, ACTOR)
    assert recovered["state"] == "awaiting_approval"
    assert calls == {"solve": 1, "snapshot": 1}
    assert sum(row["action"] == "preparation_requested" for row in recovered["audit"]) == 1
    assert sum(row["action"] == "prepared" for row in recovered["audit"]) == 1
    assert connection.writes == []


@pytest.mark.parametrize("crash_at", ["solver", "root_commit"])
def test_root_interruption_is_terminal_without_replanning(wired, monkeypatch, crash_at):
    cases, case_id, executor, connection, options, _, root = wired
    calls = record_preparations(monkeypatch)
    if crash_at == "solver":
        def fail(*args):
            raise RuntimeError("controlled solver failure")
        monkeypatch.setattr(service, "solve_blueprint", fail)
    else:
        save = ProductVersions._save

        def fail(registry, value):
            if value["state"] == "selected":
                raise OSError("controlled root stop before final commit")
            return save(registry, value)

        monkeypatch.setattr(ProductVersions, "_save", fail)
    pending = cases.prepare(case_id, ACTOR)
    assert pending["decision"]["preparation_pending"]
    registry = ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert registry["selected_plan_id"] is None
    assert registry["state"] == ("prepare_failed" if crash_at == "solver" else "preparing")
    monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("interruption must not replan"))
    recovered = ProductCaseService(cases.root.parent, executor).prepare(case_id, ACTOR)
    assert recovered["state"] == "failed" and recovered["reason"] == "product_preparation_interrupted"
    assert not recovered["decision"]["can_resume_preparation"]
    assert cases.prepare(case_id, ACTOR) == recovered
    assert calls["snapshot"] == 1 and connection.writes == []


def test_changed_local_request_cannot_replace_a_pending_intent(wired, monkeypatch):
    cases, case_id, executor, connection, _, _, _ = wired
    calls = record_preparations(monkeypatch)
    product = client.product
    monkeypatch.setattr(client, "product", lambda *a, **k: (_ for _ in ()).throw(client.ExecutorError("unavailable")))
    cases.prepare(case_id, ACTOR)
    monkeypatch.setattr(client, "product", product)
    executor.request = {**executor.request, "minimum_margin_pct": 90}
    result = ProductCaseService(cases.root.parent, executor).prepare(case_id, ACTOR)
    assert result["state"] == "failed" and result["reason"] == "product_binding_changed"
    assert calls == {"solve": 0, "snapshot": 0} and connection.writes == []


def test_two_api_instances_recover_the_same_plan_only_once(wired, monkeypatch):
    cases, case_id, executor, connection, _, _, _ = wired
    calls = record_preparations(monkeypatch)
    product = client.product

    def lost(*args, **kwargs):
        product(*args, **kwargs)
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "product", lost)
    cases.prepare(case_id, ACTOR)
    monkeypatch.setattr(client, "product", product)
    instances = [ProductCaseService(cases.root.parent, executor) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda instance: instance.prepare(case_id, ACTOR), instances))
    assert results[0] == results[1] and results[0]["state"] == "awaiting_approval"
    assert calls == {"solve": 1, "snapshot": 1} and connection.writes == []


def test_http_pending_prepare_owner_review_and_secret_projection(wired, monkeypatch, tmp_path):
    executor = wired[2]
    product = client.product
    calls = record_preparations(monkeypatch)
    with api_tests.client_for(tmp_path / "http", executor) as (http, _):
        actor = api_tests.sign_in(http)
        cases = http.app.state.product_cases
        case = cases.create(actor, "B")
        original = cases._read(case["case_id"])
        original.order["units"] = 10
        cases._write(original)
        url = f"/api/product/cases/{case['case_id']}"

        def lost(*args, **kwargs):
            product(*args, **kwargs)
            raise client.ExecutorError("unavailable")

        monkeypatch.setattr(client, "product", lost)
        pending = http.post(url + "/prepare")
        assert pending.status_code == 200 and pending.json()["decision"]["can_resume_preparation"]
        assert "_preparation" not in pending.json() and "preparation" not in pending.json()
        assert "request_sha256" not in pending.text
        api_tests.sign_in(http, api_tests.INVITE_B)
        assert http.post(url + "/prepare").status_code == 404
        assert http.get(url).status_code == 404
        api_tests.sign_in(http)
        monkeypatch.setattr(client, "product", product)
        assert http.post(url + "/prepare").json()["state"] == "awaiting_approval"
        assert calls == {"solve": 1, "snapshot": 1}
    with api_tests.client_for(tmp_path / "http", executor, deployment="review") as (http, _):
        api_tests.sign_in(http)
        assert not http.get(url).json()["decision"]["can_resume_preparation"]
        assert http.post(url + "/prepare").status_code == 503
    assert calls == {"solve": 1, "snapshot": 1} and wired[3].writes == []


@pytest.mark.parametrize("question,phrase", [("What is the plan?", "has not received a confirmed plan"),
                                            ("现在方案是什么？", "尚未收到确认的方案")])
def test_pending_preparation_answer_does_not_invent_progress(question, phrase):
    case = {"state": "preparing", "plan": None, "decision": {"preparation_pending": True},
            "reason": "preparation_confirmation_pending"}
    answer = answer_case(question, case)
    assert phrase in answer["answer"]
    assert answer["citations"]
    assert "still being prepared" not in answer["answer"]
