"""Closure of actual prepared fixture artifacts, without model or ERP calls."""
import copy
import threading

import pytest
from test_continuation_product_chain import host_for
from test_investigation_handoff_integrity import prepared_case as prepared_case
from test_product_handoff import OWNER, binding
from test_product_handoff import real_bundle as real_bundle
from test_product_handoff import wired as wired

from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.recovery.storage import digest


def request_close(cases, host, identity):
    view = cases.get(identity, "owner")
    return host.request_closure(identity, "owner", operation_id="close", expected_revision=view["revision"])


def test_registered_but_undispatched_handoff_closes_without_product_creation(prepared_case, wired):
    cases, view, _ = prepared_case
    cases.handoff(view["case_id"], "owner", operation_id="handoff", expected_revision=view["revision"])
    closed = request_close(cases, host_for(cases, wired.cases), view["case_id"])
    assert closed["status"] == "closed"
    assert closed["closure_intent"]["receipt"]["kind"] == "handoff_not_dispatched"
    assert wired.cases.get(closed["handoff"]["product_case_id"], "owner") is None
    assert wired.executors == []


def test_dispatched_product_not_found_is_unknown_not_proof_of_never_created(prepared_case, wired):
    cases, view, _ = prepared_case
    cases.handoff(view["case_id"], "owner", operation_id="handoff", expected_revision=view["revision"])
    cases.handoff_context(view["case_id"], "owner", dispatch=True)
    blocked = request_close(cases, host_for(cases, wired.cases), view["case_id"])
    assert blocked["status"] == "closing"
    assert blocked["closure_intent"]["blockers"] == ["product_handoff_unknown"]
    assert blocked["environment_released"] is None and wired.executors == []


def test_created_product_missing_callback_closes_only_existing_record(prepared_case, wired, monkeypatch):
    cases, view, _ = prepared_case
    cases.handoff(view["case_id"], "owner", operation_id="handoff", expected_revision=view["revision"])
    context = cases.handoff_context(view["case_id"], "owner", dispatch=True)
    product = wired.cases.create_from_handoff("owner", context["binding"])
    receipt = wired.cases.handoff_receipt(product["case_id"], "owner")
    monkeypatch.setattr(wired.cases, "create_from_handoff", lambda *a, **kw: pytest.fail("no create during closure"))
    monkeypatch.setattr(wired.cases, "prepare", lambda *a, **kw: pytest.fail("no prepare during closure"))
    closed = request_close(cases, host_for(cases, wired.cases), view["case_id"])
    assert closed["status"] == "closed"
    assert closed["closure_intent"]["receipt"]["outcome"] == "never_approved"
    assert wired.cases.get(product["case_id"], "owner")["state"] == "rejected"
    assert wired.executors == []
    cases.complete_handoff(view["case_id"], "owner", product_case_id=product["case_id"],
        binding_sha256=context["intent"]["binding_sha256"], product_receipt=receipt)
    assert cases.get(view["case_id"], "owner")["status"] == "closed"


def test_unapproved_prepared_product_retired_then_approval_rejected(wired):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(identity, OWNER)
    receipt = wired.cases.close_handoff(identity, OWNER, binding_sha256=digest(value))
    assert receipt["outcome"] == "never_approved" and receipt["state"] == "rejected"
    with pytest.raises(ProductCaseError, match="case_rejected"):
        wired.cases.approve(identity, OWNER, prepared["plan"]["plan_id"])
    assert all(not connection.write_attempts for connection in wired.connections)


def test_pending_preparation_is_not_inferred_unstarted(wired):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    case = wired.cases._read(identity)
    case.preparation = {"phase": "pending", "intent": {}, "requested_at": case.created_at, "error": None}
    wired.cases._write(case)
    before = wired.cases.path_for(identity).read_bytes()
    with pytest.raises(ProductCaseError, match="not_closed"):
        wired.cases.close_handoff(identity, OWNER, binding_sha256=digest(value))
    assert wired.cases.path_for(identity).read_bytes() == before


def test_approval_wins_product_lock_closure_preserves_success(wired, monkeypatch):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(identity, OWNER)
    entered, release = threading.Event(), threading.Event()
    # Invented executor result exercises the product lock, not ERP execution.
    def apply(case, plan):
        entered.set()
        assert release.wait(3)
        return {"status": "applied", "readback": {"execution_validated": True},
                "verifier": {"status": "not_run"}, "writes": []}
    monkeypatch.setattr(wired.router, "apply", apply)
    approved, blocked = [], []
    first = threading.Thread(target=lambda: approved.append(wired.cases.approve(identity, OWNER, prepared["plan"]["plan_id"])))
    first.start()
    assert entered.wait(2)
    def close():
        try:
            wired.cases.close_handoff(identity, OWNER, binding_sha256=digest(value))
        except ProductCaseError as error:
            blocked.append(error.code)
    second = threading.Thread(target=close)
    second.start()
    second.join(1)
    assert not second.is_alive() and blocked == ["product_handoff_not_closed"]
    release.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert approved[0]["state"] == "succeeded"
    closed = wired.cases.close_handoff(identity, OWNER, binding_sha256=digest(value))
    assert closed["state"] == "succeeded" and closed["outcome"] == "execution_validated"
    after = wired.cases.get(identity, OWNER)
    assert after["approval"] == approved[0]["approval"]
    assert after["execution"] == approved[0]["execution"] and after["plan"] == approved[0]["plan"]


def test_unknown_execution_closure_keeps_original_result(wired, monkeypatch):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(identity, OWNER)
    monkeypatch.setattr(wired.router, "apply", lambda *a: (_ for _ in ()).throw(RuntimeError("unknown")))
    unknown = wired.cases.approve(identity, OWNER, prepared["plan"]["plan_id"])
    before = copy.deepcopy(unknown)
    with pytest.raises(ProductCaseError, match="not_closed"):
        wired.cases.close_handoff(identity, OWNER, binding_sha256=digest(value))
    assert wired.cases.get(identity, OWNER) == before


def test_closure_file_lock_is_nonblocking_across_service_instances(wired):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    other = ProductCaseService(wired.cases.root, wired.router)
    with wired.cases._mutation():
        with pytest.raises(ProductCaseError, match="product_handoff_not_closed"):
            other.close_handoff(identity, OWNER, binding_sha256=digest(value))
    assert other.close_handoff(identity, OWNER, binding_sha256=digest(value))["outcome"] == "never_approved"
