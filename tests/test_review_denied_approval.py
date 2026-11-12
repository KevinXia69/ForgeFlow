"""A legacy review intent can close only after the root excludes an issued grant."""

import pytest
from test_product_handoff import OWNER, review_case
from test_product_handoff import real_bundle as real_bundle
from test_product_handoff import wired as wired

from forgeflow.product import ProductCaseError, ProductCaseService


@pytest.mark.parametrize("change", ["cancel", "fully_received"])
def test_legacy_review_closed_purchase_denies_approval_and_closes_safely(wired, change):
    case_id, plan_id = review_case(wired)
    cases = wired.cases
    case = cases._owned(case_id, OWNER)
    binding_sha = case.handoff["binding_sha256"]
    records = wired.connections[0].records
    if change == "cancel":
        records["purchase.order"][0]["state"] = "cancel"
    else:
        line = records["purchase.order.line"][0]
        line["qty_received"] = line["product_qty"]
    refused = cases.approve(case_id, OWNER, plan_id)
    assert refused["reason"] == "review_notice_line_mismatch"
    assert refused["approval"] is None and refused["execution"] is None
    (audit,) = [
        row for row in cases._owned(case_id, OWNER).audit if row["action"] == "authorization_denied"
    ]
    assert audit["reason"] == refused["reason"] and audit["plan_id"] == plan_id
    assert audit["dispatch_state"] == "not_dispatched"
    restored = ProductCaseService(cases.root, wired.router)
    closed = restored.close_handoff(case_id, OWNER, binding_sha256=binding_sha)
    assert closed["outcome"] == "never_approved" and closed["environment_clear"]
    assert restored.close_handoff(case_id, OWNER, binding_sha256=binding_sha) == closed
    assert not list(wired.executors[0].approvals.glob("approval-*"))
    assert not list(wired.executors[0].store.rglob("journal*.jsonl"))
    assert wired.connections[0].write_attempts == []


@pytest.mark.parametrize("entrypoint", ["reject", "close", "expired_close"])
def test_old_failed_record_can_retire_only_its_bound_plan_after_restart(
    wired, monkeypatch, entrypoint
):
    case_id, plan_id = review_case(wired)
    cases = wired.cases
    prior = cases._owned(case_id, OWNER)
    binding_sha = prior.handoff["binding_sha256"]
    wired.connections[0].records["purchase.order"][0]["state"] = "cancel"
    # Keep exactly the failed/authorizing record persisted by the old version.
    # Merely reading it must neither mutate the record nor issue a root request.
    with monkeypatch.context() as patch:
        patch.setattr(wired.router, "retire", lambda *_: None)
        failed = cases.approve(case_id, OWNER, plan_id)
    assert failed["state"] == "failed" and failed["approval"]["phase"] == "authorizing"
    assert failed["decision"]["can_reject"] and not failed["decision"]["can_reconcile"]
    if entrypoint == "expired_close":
        # Older clients could replay approval after expiry, overwriting only
        # case.reason while its authorization_error still identifies the denial.
        from datetime import UTC, datetime, timedelta

        with monkeypatch.context() as patch:
            patch.setattr(
                "forgeflow.product.service._now", lambda: datetime.now(UTC) + timedelta(days=1)
            )
            with pytest.raises(ProductCaseError, match="approval_expired"):
                cases.approve(case_id, OWNER, plan_id)
        failed = cases.get(case_id, OWNER)
        assert failed["state"] == "expired" and failed["decision"]["can_reject"]
    before = cases._owned(case_id, OWNER).to_record()
    with pytest.raises(ProductCaseError):
        cases.cancel_unstarted(case_id, OWNER, plan_id)
    restored = ProductCaseService(cases.root, wired.router)
    assert restored.get(case_id, OWNER) == failed
    assert before == restored._owned(case_id, OWNER).to_record()
    with pytest.raises(ProductCaseError, match="plan_mismatch"):
        restored.reject(case_id, OWNER, plan_id="e" * 64)
    with pytest.raises(ProductCaseError):
        restored.reject(case_id, "other-principal", plan_id=plan_id)
    assert before == restored._owned(case_id, OWNER).to_record()

    # Drop the retirement acknowledgement *after* the trusted registry committed.
    # A later close must replay the original CAS, not issue a different decision.
    retire = wired.router.retire

    def lose_reply(*args):
        retire(*args)
        raise TimeoutError

    with monkeypatch.context() as patch:
        patch.setattr(wired.router, "retire", lose_reply)
        with pytest.raises(ProductCaseError, match="retirement_unconfirmed"):
            restored.close_handoff(case_id, OWNER, binding_sha256=binding_sha)
    assert before == restored._owned(case_id, OWNER).to_record()
    if entrypoint == "reject":
        final = restored.reject(case_id, OWNER, plan_id=plan_id, reason="Close revoked purchase")
    else:
        assert (
            restored.close_handoff(case_id, OWNER, binding_sha256=binding_sha)["outcome"]
            == "never_approved"
        )
        final = restored.get(case_id, OWNER)
    assert final["state"] == "rejected" and final["approval"] is None and final["execution"] is None
    after = restored._owned(case_id, OWNER)
    assert after.audit[:-1] == before["audit"]
    assert after.audit[-1]["action"] == "authorization_denied"
    assert restored.handoff_closure_receipt(case_id, OWNER)["outcome"] == "never_approved"
    assert not list(wired.executors[0].approvals.glob("approval-*"))
    assert wired.connections[0].write_attempts == []


def lost_grant_then_denied(wired, monkeypatch, *, expired=False):
    from forgeflow.product.executor import ProductExecutionError

    case_id, plan_id = review_case(wired)
    cases, executor = wired.cases, wired.executors[0]
    product = executor._product

    def lost_grant(case, action, payload):
        response = product(case, action, payload)
        if action == "authorize":
            raise ProductExecutionError("unavailable")
        return response

    with monkeypatch.context() as patch:
        patch.setattr(executor, "_product", lost_grant)
        pending = cases.approve(case_id, OWNER, plan_id)
    assert pending["decision"]["authorization_pending"]
    assert list(executor.approvals.glob("approval-*"))
    wired.connections[0].records["purchase.order"][0]["state"] = "cancel"
    if expired:
        from datetime import UTC, datetime, timedelta

        # The owner returns after expiry, before any retry could recheck the PO.
        with monkeypatch.context() as patch:
            patch.setattr("forgeflow.product.service._now", lambda: datetime.now(UTC) + timedelta(days=1))
            with pytest.raises(ProductCaseError, match="approval_expired"):
                cases.approve(case_id, OWNER, plan_id)
        refused = cases.get(case_id, OWNER)
        assert refused["state"] == "expired"
    else:
        refused = cases.approve(case_id, OWNER, plan_id)
        assert refused["state"] == "failed"
    assert refused["approval"]["phase"] == "authorizing"
    return case_id, plan_id, refused


@pytest.mark.parametrize("expired", [False, True])
def test_lost_grant_is_proved_and_retired_without_dispatch_after_restart(wired, monkeypatch, expired):
    from forgeflow.recovery.storage import read_json

    case_id, plan_id, refused = lost_grant_then_denied(wired, monkeypatch, expired=expired)
    cases, executor = wired.cases, wired.executors[0]
    binding_sha = cases._owned(case_id, OWNER).handoff["binding_sha256"]
    restored = ProductCaseService(cases.root, wired.router)
    for close in (
        lambda: restored.reject(case_id, OWNER, plan_id=plan_id),
        lambda: restored.close_handoff(case_id, OWNER, binding_sha256=binding_sha),
    ):
        with pytest.raises(ProductCaseError):
            close()
        assert restored.get(case_id, OWNER) == refused
    assert not any(
        row["action"] == "authorization_denied" for row in restored._owned(case_id, OWNER).audit
    )
    assert not list(executor.store.rglob("journal*.jsonl"))
    assert wired.connections[0].write_attempts == []

    assert refused["decision"]["can_cancel_unstarted"]
    (token_file,) = executor.approvals.glob("approval-*.json")
    grant = read_json(token_file)
    grant.pop("schema_version")
    product = executor._product
    receipts = []

    def cancel_only(case, action, payload):
        assert action == "cancel_unstarted" and payload["approval"] == grant
        response = product(case, action, payload)
        receipts.append(response["receipt"])
        if len(receipts) == 1:
            raise TimeoutError("lost cancellation reply")
        return response

    monkeypatch.setattr(executor, "_product", cancel_only)
    monkeypatch.setattr(executor, "apply", lambda *_: pytest.fail("must never dispatch"))
    monkeypatch.setattr(executor, "review_connection", lambda: pytest.fail("must not consult changed ERP"))
    with pytest.raises(ProductCaseError):
        restored.cancel_unstarted(case_id, OWNER, plan_id)
    assert restored.get(case_id, OWNER) == refused
    restarted = ProductCaseService(cases.root, wired.router)
    closed = restarted.cancel_unstarted(case_id, OWNER, plan_id)
    assert closed["state"] == "rejected" and closed["execution"]["status"] == "not_started"
    assert closed["approval"]["phase"] == "authorized" and not closed["decision"]["can_reconcile"]
    assert not closed["decision"]["can_cancel_unstarted"] and not closed["decision"]["can_reject"]
    assert closed["execution"]["verifier"]["status"] == "not_run" and closed["execution"]["writes"] == []
    saved = restarted._owned(case_id, OWNER)
    assert saved.approval["executor_grant"] == grant
    assert receipts[0] == receipts[1] == saved.approval["unstarted_cancellation"]
    assert saved.audit[-1]["action"] == "unstarted_cancelled"
    assert restarted.close_handoff(case_id, OWNER, binding_sha256=binding_sha)["outcome"] == "not_started"
    assert restarted.cancel_unstarted(case_id, OWNER, plan_id) == closed
    assert list(executor.approvals.glob("approval-*")) == [token_file]
    assert not list(executor.store.rglob("journal*.jsonl")) and wired.connections[0].write_attempts == []


@pytest.mark.parametrize("evidence", ["dispatch", "corrupt", "other_grant", "unknown_environment"])
def test_lost_grant_cannot_close_with_unresolved_or_conflicting_evidence(wired, monkeypatch, evidence):
    from forgeflow.recovery.journal import Journal
    from forgeflow.recovery.storage import atomic_json, read_json

    case_id, plan_id, _ = lost_grant_then_denied(wired, monkeypatch)
    cases, executor = wired.cases, wired.executors[0]
    case = cases._owned(case_id, OWNER)
    path = executor.store / plan_id / "journal.jsonl"
    env = wired.registry.root / "environment.json"
    if evidence == "dispatch":
        with Journal(path) as journal:
            journal.append("dispatch", {"step": 1})
    elif evidence == "corrupt":
        path.write_bytes(b"partial")
    elif evidence == "other_grant":
        case.approval["nonce"] = "e" * 32
        cases._write(case)
    else:
        held = read_json(env)
        held["pending"] = {"case_id": case_id, "plan_id": plan_id,
                           "owner_sha256": cases._executor_view(case)["_owner_sha256"], "phase": "unknown"}
        atomic_json(env, held, replace=True)
    before = cases._owned(case_id, OWNER).to_record()
    held = read_json(env)
    with pytest.raises(ProductCaseError):
        cases.cancel_unstarted(case_id, OWNER, plan_id)
    assert before == cases._owned(case_id, OWNER).to_record() and read_json(env) == held
    with pytest.raises(ProductCaseError):
        cases.close_handoff(case_id, OWNER, binding_sha256=case.handoff["binding_sha256"])
    assert wired.connections[0].write_attempts == []


def test_denied_review_does_not_clear_an_unresolved_environment_dispatch(wired, monkeypatch):
    from forgeflow.recovery.storage import atomic_json, read_json

    case_id, plan_id = review_case(wired)
    cases = wired.cases
    wired.connections[0].records["purchase.order"][0]["state"] = "cancel"
    with monkeypatch.context() as patch:
        patch.setattr(wired.router, "retire", lambda *_: None)
        failed = cases.approve(case_id, OWNER, plan_id)
    case = cases._owned(case_id, OWNER)
    env = wired.registry.root / "environment.json"
    held = read_json(env)
    held["pending"] = {
        "case_id": case_id,
        "plan_id": plan_id,
        "owner_sha256": cases._executor_view(case)["_owner_sha256"],
        "phase": "unknown",
    }
    atomic_json(env, held, replace=True)
    with pytest.raises(ProductCaseError, match="environment_execution_unresolved"):
        cases.close_handoff(case_id, OWNER, binding_sha256=case.handoff["binding_sha256"])
    assert read_json(env) == held and cases.get(case_id, OWNER) == failed
    assert wired.connections[0].write_attempts == []


def bound_review_case(wired):
    from test_product_handoff import binding

    value, records = binding()
    line = records["purchase.order.line"][0]
    po = next(row for row in records["purchase.order"] if row["id"] == line["order_id"])
    scope = {"purchase_order_id": po["id"], "purchase_line_id": line["id"]}
    value["review_profile"] = "recovery-review-v1"
    value["confirmed_supply"] = {
        **scope,
        "product_id": line["product_id"],
        "supplier_id": po["partner_id"],
    }
    value["request"]["disruption"].update(scope)
    cases, case_id = wired.cases, value["product_case_id"]
    cases.create_from_handoff(OWNER, value)
    current = cases.prepare(case_id, OWNER)
    if current["plan"] is None:
        comparison = current["comparison"]
        current = cases.select_candidate(
            case_id,
            OWNER,
            comparison["plans"][0]["plan_id"],
            comparison_id=comparison["comparison_id"],
            selection_revision=comparison["selection_revision"],
        )
    return case_id, current["plan"]["plan_id"]


@pytest.mark.parametrize(
    "change", ["cancel", "fully_received", "partial_receipt", "new_line", "replaced_line"]
)
def test_bound_review_rechecks_current_purchase_before_issuing_a_grant(wired, change):
    case_id, plan_id = bound_review_case(wired)
    records = wired.connections[0].records
    line = records["purchase.order.line"][0]
    if change == "cancel":
        records["purchase.order"][0]["state"] = "cancel"
    elif change == "fully_received":
        line["qty_received"] = line["product_qty"]
    elif change == "partial_receipt":
        line["qty_received"] = 1
    elif change == "new_line":
        records["purchase.order.line"].append(line | {"id": 99})
    else:
        line["id"] = 99
    refused = wired.cases.approve(case_id, OWNER, plan_id)
    assert refused["state"] == "rejected" and refused["reason"] == "review_notice_line_mismatch"
    assert refused["approval"] is None and refused["execution"] is None
    assert wired.cases.handoff_closure_receipt(case_id, OWNER)["outcome"] == "never_approved"
    assert not list(wired.executors[0].approvals.glob("approval-*"))
    assert wired.connections[0].write_attempts == []


def test_purchase_closed_after_grant_requires_positive_unstarted_evidence(wired, monkeypatch):
    case_id, plan_id = bound_review_case(wired)
    cases, executor = wired.cases, wired.executors[0]
    product = executor._product

    def close_after_grant(case, action, payload):
        result = product(case, action, payload)
        if action == "authorize":
            wired.connections[0].records["purchase.order"][0]["state"] = "cancel"
        return result

    monkeypatch.setattr(executor, "_product", close_after_grant)
    stopped = cases.approve(case_id, OWNER, plan_id)
    assert stopped["approval"]["phase"] == "dispatching"
    assert stopped["decision"]["can_cancel_unstarted"]
    with pytest.raises(ProductCaseError, match="not_closed"):
        cases.handoff_closure_receipt(case_id, OWNER)
    closed = cases.cancel_unstarted(case_id, OWNER, plan_id)
    assert closed["execution"]["status"] == "not_started"
    assert cases.handoff_closure_receipt(case_id, OWNER)["outcome"] == "not_started"
    assert wired.connections[0].write_attempts == []
