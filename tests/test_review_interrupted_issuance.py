"""Restart recovery on either side of the durable token write."""

from datetime import UTC, datetime, timedelta

import pytest
from test_product_handoff import OWNER, review_case
from test_product_handoff import real_bundle as real_bundle
from test_product_handoff import wired as wired

from forgeflow.approval import Approval
from forgeflow.executor.protocol import ProtocolError
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.recovery import product_versions, service
from forgeflow.recovery.storage import atomic_json, read_json


def interrupted(wired, monkeypatch, *, signed, expired):
    case_id, plan_id = review_case(wired)
    cases, executor = wired.cases, wired.executors[0]
    saved = product_versions.atomic_json

    def interrupt(path, value, **kwargs):
        if value.get("state") == "approved":
            raise OSError("lost before approval commit")
        return saved(path, value, **kwargs)

    def stop_before_signing(*args, **kwargs):
        raise OSError("lost before issuing token")

    with monkeypatch.context() as patch:
        if signed:
            patch.setattr(product_versions, "atomic_json", interrupt)
        else:
            patch.setattr("forgeflow.approval.issue", stop_before_signing)
        pending = cases.approve(case_id, OWNER, plan_id)
    assert pending["decision"]["authorization_pending"]
    path = executor.store / "product-decisions" / f"local-{case_id}.json"
    assert read_json(path)["state"] == "approval_issuing"
    assert bool(list(executor.approvals.glob("approval-*.json"))) == signed
    restarted = ProductCaseService(cases.root, wired.router)
    if expired:
        with monkeypatch.context() as patch:
            patch.setattr("forgeflow.product.service._now", lambda: datetime.now(UTC) + timedelta(days=1))
            with pytest.raises(ProductCaseError, match="approval_expired"):
                restarted.approve(case_id, OWNER, plan_id)
    else:
        wired.connections[0].records["purchase.order"][0]["state"] = "cancel"
        assert restarted.approve(case_id, OWNER, plan_id)["reason"] == "review_notice_line_mismatch"
    assert restarted.get(case_id, OWNER)["decision"]["can_cancel_unstarted"]
    return restarted, case_id, plan_id, path


@pytest.mark.parametrize("expired", [False, True], ids=["purchase_closed", "intent_expired"])
@pytest.mark.parametrize("signed", [False, True], ids=["before_token", "after_token"])
def test_interrupted_issuance_retires_only_original_intent(wired, monkeypatch, real_bundle, signed, expired):
    cases, case_id, plan_id, path = interrupted(wired, monkeypatch, signed=signed, expired=expired)
    original = cases._owned(case_id, OWNER)
    executor = wired.executors[0]
    context = cases._executor_view(original)
    expected = executor._approval_payload(context, original.plan)
    intent = {key: expected[key] for key in ("nonce", "approver", "issued_at_utc", "expires_at_utc")}
    product, receipts = executor._product, []

    def lose_first_reply(case, action, payload):
        assert action == "cancel_unstarted" and payload["approval"] == expected
        result = product(case, action, payload)
        receipts.append(result["receipt"])
        if len(receipts) == 1:
            raise TimeoutError("lost retirement reply")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(executor, "_product", lose_first_reply)
        with pytest.raises(ProductCaseError):
            cases.cancel_unstarted(case_id, OWNER, plan_id)
        # Root has already closed. A new product process can recover that exact receipt.
        assert read_json(path)["state"] == "retired"
        restored = ProductCaseService(cases.root, wired.router)
        closed = restored.cancel_unstarted(case_id, OWNER, plan_id)
        assert closed["state"] == "rejected" and len(receipts) == 2 and receipts[0] == receipts[1]
        assert restored.cancel_unstarted(case_id, OWNER, plan_id) == closed
    outcome = "not_started" if signed else "never_approved"
    assert receipts[0]["outcome"] == outcome
    assert restored.close_handoff(case_id, OWNER, binding_sha256=original.handoff["binding_sha256"])["outcome"] == outcome
    if signed:
        assert closed["approval"]["phase"] == "authorized" and closed["execution"]["writes"] == []
    else:
        assert closed["approval"] is None and closed["execution"] is None
        audit = restored._owned(case_id, OWNER).audit[-1]
        assert audit["action"] == "authorization_denied" and audit["receipt_sha256"] == receipts[0]["receipt_sha256"]
    # Replay a delayed root authorization request with the original parameters.
    with pytest.raises(ProtocolError) as rejected:
        product(context, "authorize", {"owner_sha256": context["_owner_sha256"], "intent": intent,
                "expected_revision": original.plan["executor_binding"]["revision"], "plan_id": plan_id})
    assert rejected.value.reason == "product_plan_not_selected"
    token = Approval(**expected)
    grant = service.ExecutionGrant(token, token.run_id, token.policy_sha256, token.snapshot_sha256, OWNER)
    with pytest.raises(product_versions.VersionError, match="product_plan_retired"):
        service.apply(wired.connections[0], plan_id, store=executor.store, bundle=real_bundle,
                      require_product_version=True, approval_store=executor.approvals, grant=grant)
    assert bool(list(executor.approvals.glob("approval-*.json"))) == signed
    assert not list(executor.approvals.glob("*.used")) and not list(executor.store.rglob("journal*.jsonl"))
    assert wired.connections[0].write_attempts == []


@pytest.mark.parametrize("conflict", ["different_intent", "different_token", "duplicate_grant", "used",
                                       "partial_journal", "active_execution", "success"])
def test_contradictory_evidence_keeps_issuance_locked(wired, monkeypatch, conflict):
    cases, case_id, plan_id, path = interrupted(wired, monkeypatch, signed=False, expired=True)
    original = cases._owned(case_id, OWNER)
    executor = wired.executors[0]
    expected = executor._approval_payload(cases._executor_view(original), original.plan)
    executor.approvals.mkdir(exist_ok=True)
    nonce = expected["nonce"]
    if conflict == "different_intent":
        original.approval["nonce"] = "e" * 32
        cases._write(original)
    elif conflict == "different_token":
        atomic_json(executor.approvals / f"approval-{nonce}.json", {**Approval(**expected).payload(), "approver": "other"})
    elif conflict == "duplicate_grant":
        other = Approval(**{**expected, "nonce": "f" * 32})
        atomic_json(executor.approvals / f"approval-{other.nonce}.json", other.payload())
    elif conflict == "used":
        (executor.approvals / f"approval-{nonce}.used").write_text(Approval(**expected).digest())
    elif conflict == "partial_journal":
        (executor.store / plan_id / "journal.jsonl").write_text("partial")
    elif conflict == "active_execution":
        atomic_json(executor.store / "active-plan.json", {"plan_id": plan_id})
    else:
        atomic_json(executor.store / plan_id / "success.json", {})
    before = read_json(path)
    with pytest.raises(ProductCaseError):
        cases.cancel_unstarted(case_id, OWNER, plan_id)
    assert cases._owned(case_id, OWNER).to_record() == original.to_record()
    assert read_json(path) == before and wired.connections[0].write_attempts == []
