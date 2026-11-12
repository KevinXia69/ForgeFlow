"""R lifecycle controls use structured cases and the real persistent host.

Model/ERP transport boundaries are controlled fixtures. Reader, planner, policy
compilation, case locks, handoff binding and product admission remain real.
"""
import copy
import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from investigation_support import EVENT
from test_continuation_host import wait
from test_continuation_product_chain import handoff_job, host_for
from test_continuation_worker import runner_for
from test_investigation_continuation import confirm, waiting
from test_investigation_dev_dataset import observed_draft
from test_product_handoff import binding
from test_product_handoff import real_bundle as real_bundle
from test_product_handoff import wired as wired
from test_structured_budget_cases import current, make_structured_system, submit

from forgeflow.investigation.cases import RESOURCE_FIELDS, CaseError, InvestigationCases
from forgeflow.investigation.continuation import ContinuationCoordinator
from forgeflow.investigation.fixtures import FixtureConnection, supplier_cancellation_control
from forgeflow.investigation.host import ContinuationHost
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import (
    PartialPolicy,
    load_investigation_policy,
    policy_from_payload,
)
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import OdooSource
from forgeflow.product import ProductCaseError
from forgeflow.recovery import client
from forgeflow.recovery.storage import atomic_json, read_json


def setup(tmp_path, *, expiry=None, runner=None):
    system = make_structured_system(tmp_path, expiry=expiry)
    calls = []
    coordinator = ContinuationCoordinator(system["cases"],
        episode_runner=runner or runner_for(system, calls), reply_interpreter=None)
    host = ContinuationHost(cases=system["cases"], coordinator=coordinator,
                            product=None, create_inputs=lambda _: {})
    return system, host, calls


def close(host, view, operation="close"):
    return host.request_closure(view["case_id"], "owner", operation_id=operation,
                                expected_revision=view["revision"])


def create_next(system, operation="next"):
    return system["cases"].create("owner", operation_id=operation, template=task_template(system["contract"]),
        policy=system["policy"], event=EVENT, decision_input_mode="structured_budget_v1")


def test_active_structured_parent_cancel_closes_after_original_late_callback(tmp_path):
    system, host, calls = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = host.coordinator.episode_runner

    def running(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)

    host.coordinator.episode_runner = running
    identity = system["view"]["case_id"]
    job = host.schedule("parent", identity, "owner", "create")
    assert entered.wait(2)
    try:
        blocked = close(host, current(system))
        assert blocked["status"] == "closing" and blocked["environment_released"] is None
        assert blocked["closure_intent"]["blockers"] == ["worker_result_unknown"]
        assert read_json(system["cases"].root / "environment.json")["active_case"] == identity
    finally:
        release.set()
    wait(host, job)
    closed = current(system)
    assert closed["status"] == "closed" and closed["usage_complete"]
    assert closed["ticket"] is None and closed["confirmation"] is None and closed["budget_inputs"] == []
    assert closed["decision_input_mode"] == "structured_budget_v1" and len(calls) == 1
    late = system["cases"].record_terminal(identity, "owner", system["view"]["current_episode"])
    assert late["status"] == "closed" and late["ticket"] is None
    assert host.reconcile_closure(identity, "owner")["closure_intent"]["receipt"] == closed["closure_intent"]["receipt"]


def test_expired_structured_candidate_closes_then_allows_new_case(tmp_path):
    system, host, calls = setup(tmp_path, expiry="1970-01-01T00:02:30+00:00")
    system["view"] = waiting(system)
    submit(system)
    candidate = current(system)
    system["environment"]["time"] = 200
    path = system["cases"]._directory(candidate["case_id"]) / "case.json"
    before = path.read_bytes()
    assert current(system)["policy_expired"] and path.read_bytes() == before
    closed = host.expire(candidate["case_id"], "owner")
    assert closed["status"] == "closed" and closed["closure_intent"]["reason"] == "policy_expired"
    assert closed["confirmation"] is None and len(closed["episodes"]) == 1
    newer = create_next(system)
    assert newer["decision_input_mode"] == "structured_budget_v1" and newer["status"] == "investigating"
    assert host.reconcile_closure(candidate["case_id"], "owner")["environment_released"] == closed["environment_released"]
    assert read_json(system["cases"].root / "environment.json")["active_case"] == newer["case_id"]
    with pytest.raises(CaseError):
        confirm(system["cases"], candidate)
    assert calls == []


def test_unknown_original_worker_requires_verified_same_claim_before_release(tmp_path):
    calls = []

    def missing_result(**kwargs):
        calls.append(kwargs["config"]["run_id"])
        raise RuntimeError("original transport lost")

    system, host, _ = setup(tmp_path, runner=missing_result)
    identity = system["view"]["case_id"]
    job = host.schedule("parent", identity, "owner", "create")
    assert wait(host, job)["status"] == "reconciliation_required"
    blocked = close(host, current(system))
    assert blocked["usage"]["tokens"] is None and blocked["environment_released"] is None
    assert blocked["closure_intent"]["blockers"] == ["worker_result_unknown"]
    with pytest.raises(CaseError, match="environment_chain_active"):
        create_next(system)
    operation = "episode-" + system["view"]["current_episode"]
    original_path = system["cases"]._directory(identity) / "workers" / operation / "claim.json"
    original_claim = read_json(original_path)
    original_bytes = original_path.read_bytes()
    seen = []

    def recover(claim, directory):
        assert claim == original_claim and directory == original_path.parent
        seen.append(claim["operation_id"])
        return {"original_worker_verified": True, "quiescence": "confirmed",
                "usage": {"tokens": 123, "seconds": 2, "queries": 0, "validations": 0, "prepares": 0}}

    denied = host.reconcile_closure(identity, "owner", recover_worker=lambda *_: {
        "original_worker_verified": False, "quiescence": "unknown"})
    assert denied["environment_released"] is None and len(calls) == 1
    closed = host.reconcile_closure(identity, "owner", recover_worker=recover)
    assert closed["status"] == "closed" and closed["usage"]["tokens"] == 123
    assert closed["ticket"] is None and seen == [operation] and len(calls) == 1
    assert original_path.read_bytes() == original_bytes


def test_partial_usage_receipt_is_not_replaced_by_zero_or_released(tmp_path):
    system, host, calls = setup(tmp_path)
    original = host.coordinator.episode_runner

    def partial(**kwargs):
        result = original(**kwargs)
        result["usage"]["tokens"] = None
        return result

    host.coordinator.episode_runner = partial
    identity = system["view"]["case_id"]
    job = host.schedule("parent", identity, "owner", "create")
    wait(host, job)
    blocked = close(host, current(system))
    assert blocked["closure_intent"]["blockers"] == ["case_usage_unknown"]
    assert blocked["usage"]["tokens"] is None and blocked["environment_released"] is None
    operation = "episode-" + system["view"]["current_episode"]
    result_path = system["cases"]._directory(identity) / "workers" / operation / "result.json"
    before = result_path.read_bytes()
    with pytest.raises(CaseError, match="usage_receipt_conflict"):
        system["cases"].finish_work(identity, "owner", operation_id=operation,
                                    usage=dict.fromkeys(RESOURCE_FIELDS, 0))
    host.coordinator.reconcile_worker(identity, "owner", operation,
        recover=lambda *_: pytest.fail("original result exists; do not replace it"))
    assert result_path.read_bytes() == before and len(calls) == 1
    assert host.reconcile_closure(identity, "owner")["environment_released"] is None


def test_old_generation_closure_never_clears_a_new_environment_claim(tmp_path, monkeypatch):
    system, host, _ = setup(tmp_path)
    system["view"] = waiting(system)
    cases, identity = system["cases"], system["view"]["case_id"]
    new_claim = {"generation": "new-generation", "active_case": "ic-" + "f" * 32, "write_operation": None}
    original_write = cases._write

    def reset_at_release(state):
        original_write(state)
        if (state.get("closure_intent") or {}).get("phase") == "releasing":
            system["environment"]["generation"] = new_claim["generation"]
            atomic_json(cases.root / "environment.json", new_claim, replace=True)

    monkeypatch.setattr(cases, "_write", reset_at_release)
    blocked = close(host, current(system))
    assert blocked["status"] == "closing" and blocked["environment_released"] is None
    assert blocked["closure_intent"]["blockers"] == ["environment_generation_changed"]
    assert read_json(cases.root / "environment.json") == new_claim
    with pytest.raises(CaseError, match="case_closing"):
        cases.budget_control(identity, "owner", operation_id="resume-old", expected_revision=blocked["revision"], action="resume")
    assert read_json(cases.root / "environment.json") == new_claim


@pytest.fixture
def structured_prepared(tmp_path, real_bundle):
    fixture = supplier_cancellation_control()
    original = fixture.contract.payload()
    policy = policy_from_payload({"kind": "forgeflow.investigation_policy", "schema_version": 1,
        "policy_id": "structured-intake", "task_id": original["task_id"],
        **{key: original[key] for key in ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")},
        "orders": {"1": {"authorized": True, "budget_cap": None}}, "sources": {"policy": "task-owner"}})
    connection = FixtureConnection(fixture.records)
    before = connection.fingerprint()
    event = {"text": fixture.event, "sha256": hashlib.sha256(fixture.event.encode()).hexdigest()}

    def factory(value):
        return OdooSource(connection, value.source_policy() if isinstance(value, PartialPolicy) else value)

    cases = InvestigationCases(tmp_path / "structured-cases", source_factory=factory, generation=lambda: "generation-1",
        currency_probe=lambda _: {"currency": "USD", "company_id": 1, "provenance": "structured-fixture",
                                 "record_currencies": {"sales_order:1": "USD", "purchase_order:1": "USD"}})
    view = cases.create("owner", operation_id="create", template=task_template(fixture.contract), policy=policy,
                        event=event, decision_input_mode="structured_budget_v1")

    def service(config):
        return InvestigationService(source=factory(config["policy"]), contract=config["policy"], event=event,
            store=config["store"], bundle=real_bundle, connection_factory=lambda: connection)

    parent = cases.episode_config(view["case_id"], "owner", view["current_episode"])
    service(parent).dispatch(parent["run_id"], "finish", {"status": "needs_clarification", "reason": "Missing cap",
        "question": "What budget do you authorize for SO-1?"})
    view = cases.record_terminal(view["case_id"], "owner", parent["run_id"])
    cases.submit_budget(view["case_id"], "owner", operation_id="budget", expected_revision=view["revision"],
                        ticket_id=view["ticket"]["ticket_id"], amount="2000")
    view = cases.get(view["case_id"], "owner")
    receipt = confirm(cases, view)
    child = cases.episode_config(view["case_id"], "owner", receipt["child_episode"])
    investigator = service(child)
    draft = observed_draft(replace(fixture, case_id=child["run_id"], policy=child["policy"]), investigator)
    validated = investigator.dispatch(child["run_id"], "validate", {"draft": draft})
    assert validated["valid"]
    result = investigator.dispatch(child["run_id"], "prepare", {"draft_id": validated["draft_id"]})
    assert result["status"] == "prepared"
    view = cases.record_terminal(view["case_id"], "owner", child["run_id"])
    assert view["decision_input_mode"] == "structured_budget_v1" and view["replies"] == []
    assert connection.fingerprint() == before and connection.write_attempts == []
    return cases, view, investigator.store / result["prepared"]["plan_id"]


def product_ready(prepared, wired):
    cases, view, original_plan = prepared
    host = host_for(cases, wired.cases)
    job = handoff_job(cases, view, host)
    host._perform(job)
    source = cases.get(view["case_id"], "owner")
    product = wired.cases.get(source["handoff"]["product_case_id"], "owner")
    assert source["status"] == "product_review" and product["state"] == "awaiting_approval"
    context = cases.handoff_context(view["case_id"], "owner")
    product_path = wired.registry.paths(context["binding"]).store / product["plan"]["plan_id"]
    assert read_json(original_plan / "request.json") == read_json(product_path / "request.json")
    assert product["plan"]["plan_id"] != original_plan.name and product["approval"] is None
    assert all(not connection.write_attempts for connection in wired.connections)
    return host, source, product


def test_structured_confirm_actual_handoff_unknown_execution_keeps_environment_and_original_plan(
        structured_prepared, wired, monkeypatch):
    host, source, product = product_ready(structured_prepared, wired)
    cases = structured_prepared[0]
    applies, reads = [], []
    with pytest.raises(ProductCaseError, match="plan_mismatch"):
        wired.cases.approve(product["case_id"], "owner", source["draft"]["draft_sha256"])

    def lost_apply(socket, run_id, plan_id, **kwargs):
        applies.append((socket, run_id, plan_id, kwargs["approval"]["plan_id"]))
        raise client.ExecutorError("unavailable")

    def unavailable_original(socket, run_id, plan_id, **kwargs):
        reads.append((socket, run_id, plan_id))
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "apply", lost_apply)
    monkeypatch.setattr(client, "reconcile", unavailable_original)
    unknown = wired.cases.approve(product["case_id"], "owner", product["plan"]["plan_id"])
    assert unknown["state"] == "execution_unknown" and len(applies) == 1
    original_result = copy.deepcopy(unknown)
    pending = read_json(wired.registry.root / "environment.json")["pending"]
    assert pending["case_id"] == product["case_id"] and pending["plan_id"] == product["plan"]["plan_id"]
    assert wired.cases.approve(product["case_id"], "owner", product["plan"]["plan_id"]) == original_result
    with pytest.raises(ProductCaseError, match="reconciliation_unavailable"):
        wired.cases.reconcile(product["case_id"], "owner")
    assert reads == [applies[0][:3]] and len(applies) == 1
    closed = close(host, cases.get(source["case_id"], "owner"))
    assert closed["status"] == "closing" and closed["environment_released"] is None
    assert closed["closure_intent"]["blockers"] == ["product_handoff_not_closed"]
    assert wired.cases.get(product["case_id"], "owner") == original_result
    assert read_json(wired.registry.root / "environment.json")["pending"] == pending
    assert read_json(cases.root / "environment.json")["active_case"] == source["case_id"]
    source_state = read_json(cases._directory(source["case_id"]) / "case.json")
    with pytest.raises(CaseError, match="environment_chain_active"):
        cases.create("owner", operation_id="another-structured-case", template=source_state["template"],
            policy=load_investigation_policy(source_state["parent_policy"]), event=source_state["event"],
            decision_input_mode="structured_budget_v1")
    with pytest.raises(ProductCaseError, match="execution_unresolved"):
        wired.cases.create_from_handoff("owner", {**binding("2" * 32)[0], "owner": "owner"})
    assert all(not connection.write_attempts for connection in wired.connections)


def test_structured_handoff_approval_expiry_blocks_dispatch_with_original_intent(
        structured_prepared, wired, monkeypatch):
    import forgeflow.product.service as service_module

    _, _, product = product_ready(structured_prepared, wired)
    original = client.product

    def lost_authorization(socket, run_id, action, payload, **kwargs):
        if action == "authorize":
            raise client.ExecutorError("unavailable")
        return original(socket, run_id, action, payload, **kwargs)

    monkeypatch.setattr(client, "product", lost_authorization)
    monkeypatch.setattr(client, "apply", lambda *_a, **_kw: pytest.fail("expired approval cannot dispatch"))
    pending = wired.cases.approve(product["case_id"], "owner", product["plan"]["plan_id"])
    assert pending["reason"] == "approval_authorization_pending" and pending["execution"] is None
    record = wired.cases._read(product["case_id"])
    nonce = record.approval["nonce"]
    later = datetime.fromisoformat(record.approval["expires_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(service_module, "_now", lambda: later)
    with pytest.raises(ProductCaseError, match="approval_expired"):
        wired.cases.approve(product["case_id"], "owner", product["plan"]["plan_id"])
    expired = wired.cases.get(product["case_id"], "owner")
    assert expired["state"] == "expired" and expired["execution"] is None
    assert wired.cases._read(product["case_id"]).approval["nonce"] == nonce
    assert all(not connection.write_attempts for connection in wired.connections)
