"""Upgrade old immutable plans with explicit review, never old approval replay."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import test_product_versions as product_tests
from recovery_synthetic import Fake
from test_recovery_change_set import stock_margin_fixture

from forgeflow.approval import ApprovalError
from forgeflow.recovery import change_set, compatibility, scope_guard, service
from forgeflow.recovery.journal import ExecutionStopped, Journal
from forgeflow.recovery.margin import MARGIN_BASIS
from forgeflow.recovery.product_versions import ProductVersions
from forgeflow.recovery.readback import ReadbackAdapter
from forgeflow.recovery.storage import atomic_json, read_json

system = product_tests.system
OLD_BASIS = "legacy_whole_sales_margin_v1"


def original_bytes(directory):
    return {path.name: path.read_bytes() for path in directory.glob("*.json")}


def assert_original_bytes(directory, original):
    assert {name: (directory / name).read_bytes() for name in original} == original


def old_product(system, monkeypatch, *, approved=True):
    _, _, options, _ = system
    with monkeypatch.context() as old:
        old.setattr(service, "MARGIN_BASIS", OLD_BASIS)
        decision = product_tests.prepare(system)
        grant = (product_tests.grant_from(product_tests.authorize(options, decision)["approval"])
                 if approved else None)
    return decision["selected_plan_id"], grant


def interrupt_after_first_write(system, monkeypatch, plan_id, *, grant=None, legacy_check=False):
    connection, _, options, _ = system
    apply = ReadbackAdapter.apply
    predicate = change_set.margin_satisfied

    def crash(adapter, step):
        apply(adapter, step)
        if connection.writes:
            raise KeyboardInterrupt("synthetic crash after committed ERP write")

    with monkeypatch.context() as old:
        old.setattr(service, "MARGIN_BASIS", OLD_BASIS)
        old.setattr(ReadbackAdapter, "apply", crash)
        if legacy_check:
            old.setattr(change_set, "margin_satisfied",
                        lambda cost, revenue, floor: predicate(cost, 1000, floor))
        with pytest.raises(KeyboardInterrupt, match="committed ERP write"):
            service.apply(connection, plan_id, grant=grant, **options)
    assert len(connection.writes) == 1


def partial_product(system, monkeypatch):
    plan_id, grant = old_product(system, monkeypatch)
    interrupt_after_first_write(system, monkeypatch, plan_id, grant=grant)
    return plan_id, grant


def review_and_authorize(plan_id, options, **kwargs):
    review = compatibility.review_legacy_plan(plan_id, **options)
    token = compatibility.authorize_legacy_resume(plan_id, review["review_id"],
        approver=product_tests.ACTOR, **options, **kwargs)
    return review, token


def replace_current_bundle_revision(options):
    """Emulate a reviewed new bundle without altering the old plan manifest."""
    source = options["bundle"] / "forgeflow/recovery/margin.py"
    source.write_bytes(source.read_bytes() + b"\n# new reviewed release\n")
    provenance = read_json(options["bundle"] / "provenance.json")
    provenance["files_sha256"]["forgeflow/recovery/margin.py"] = hashlib.sha256(source.read_bytes()).hexdigest()
    atomic_json(options["bundle"] / "provenance.json", provenance, replace=True)


def test_completed_legacy_history_is_read_without_rewriting_original_evidence(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = old_product(system, monkeypatch)
    with monkeypatch.context() as old:
        old.setattr(service, "MARGIN_BASIS", OLD_BASIS)
        service.apply(connection, plan_id, grant=grant, **options)
    directory = options["store"] / plan_id
    originals = original_bytes(directory)
    journal = (directory / "journal.jsonl").read_bytes()
    writes = list(connection.writes)
    report = compatibility.review_legacy_plan(plan_id, **options)
    assert report["state"] == "completed_history"
    assert report == compatibility.review_legacy_plan(plan_id, **options)
    with pytest.raises(service.RecoveryError, match="legacy_recovery_not_resumable"):
        compatibility.authorize_legacy_resume(plan_id, report["review_id"],
            approver=product_tests.ACTOR, **options)
    assert_original_bytes(directory, originals)
    assert (directory / "journal.jsonl").read_bytes() == journal
    assert connection.writes == writes


def test_unstarted_old_approval_is_rejected_then_retirement_allows_fresh_preparation(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = old_product(system, monkeypatch)
    directory = options["store"] / plan_id
    originals = original_bytes(directory)
    registry = ProductVersions(options["store"])
    decision = registry.status(product_tests.RUN, product_tests.OWNER)
    with pytest.raises(service.RecoveryError, match="margin_basis_requires_reprepare"):
        product_tests.authorize(options, decision)
    with pytest.raises(service.RecoveryError, match="margin_basis_requires_reprepare"):
        service.apply(connection, plan_id, grant=grant, **options)
    report = compatibility.review_legacy_plan(plan_id, **options)
    assert report["state"] == "requires_reprepare"
    with pytest.raises(service.RecoveryError, match="legacy_recovery_not_resumable"):
        compatibility.authorize_legacy_resume(plan_id, report["review_id"],
            approver=product_tests.ACTOR, **options)
    retired = compatibility.retire_legacy_unstarted(plan_id, approver=product_tests.ACTOR, **options)
    assert retired["status"] == "retired"
    assert registry.status(product_tests.RUN, product_tests.OWNER)["selected_plan_id"] is None
    receipt = read_json(next(directory.glob("retirement-*.json")))
    assert receipt["previous_approval"]["token"] == grant.approval.payload()
    fresh = product_tests.prepare(system, expected=retired["revision"], operation="prepare-corrected")
    assert fresh["selected_plan_id"] != plan_id
    manifest = read_json(options["store"] / fresh["selected_plan_id"] / "manifest.json")
    assert manifest["margin_basis"] == MARGIN_BASIS
    assert_original_bytes(directory, originals)
    assert connection.writes == []


def test_old_historical_target_cannot_retire_the_current_corrected_decision(system, monkeypatch):
    _, _, options, _ = system
    plan_id, _ = old_product(system, monkeypatch, approved=False)
    registry = ProductVersions(options["store"])
    previous = registry.status(product_tests.RUN, product_tests.OWNER)
    fresh = product_tests.prepare(system, expected=previous["revision"], operation="prepare-new-version")
    with pytest.raises(service.RecoveryError, match="product_plan_retired"):
        compatibility.retire_legacy_unstarted(plan_id, approver=product_tests.ACTOR, **options)
    assert registry.status(product_tests.RUN, product_tests.OWNER) == fresh


def test_valid_partial_legacy_plan_resumes_only_with_new_review_authorization(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = partial_product(system, monkeypatch)
    directory = options["store"] / plan_id
    originals = original_bytes(directory)
    journal_prefix = (directory / "journal.jsonl").read_bytes()
    old_approval_path = options["store"].parent / "approvals" / f"approval-{grant.approval.nonce}.json"
    old_approval = old_approval_path.read_bytes()
    with pytest.raises(service.RecoveryError, match="margin_basis_requires_reprepare"):
        service.apply(connection, plan_id, grant=grant, **options)
    with pytest.raises(service.RecoveryError, match="legacy_recovery_not_unstarted"):
        compatibility.retire_legacy_unstarted(plan_id, approver=product_tests.ACTOR, **options)
    report, token = review_and_authorize(plan_id, options)
    assert report["state"] == "requires_recovery_authorization"
    assert report["pending_dispatch"] and report["checks"]["margin_basis"] == MARGIN_BASIS
    assert token.nonce != grant.approval.nonce and token.policy_sha256 == report["review_id"]
    with pytest.raises(service.RecoveryError, match="legacy_recovery_review_changed"):
        service.apply(connection, plan_id, grant=grant, legacy_authorization=grant.approval, **options)
    assert len(connection.writes) == 1
    result = service.apply(connection, plan_id, grant=grant, legacy_authorization=token, **options)
    assert result["execution_validated"] is True
    assert len(connection.writes) == len(set(connection.writes))
    assert (directory / "journal.jsonl").read_bytes().startswith(journal_prefix)
    assert_original_bytes(directory, originals)
    assert compatibility.review_legacy_plan(plan_id, **options)["state"] == "completed_history"
    assert old_approval_path.read_bytes() == old_approval
    writes = list(connection.writes)
    with pytest.raises(service.RecoveryError, match="legacy_recovery_not_resumable"):
        service.apply(connection, plan_id, grant=grant, legacy_authorization=token, **options)
    assert connection.writes == writes


def test_expired_recovery_authorization_does_not_resume_writes(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = partial_product(system, monkeypatch)
    issued = datetime.now(UTC)
    _, token = review_and_authorize(plan_id, options, now=issued, ttl_seconds=1)
    with pytest.raises(ApprovalError, match="approval_expired"):
        service.apply(connection, plan_id, grant=grant, legacy_authorization=token,
                      now=issued + timedelta(seconds=2), **options)
    assert len(connection.writes) == 1


def test_consumed_old_approval_without_dispatch_can_retire_and_reprepare(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = old_product(system, monkeypatch)
    consume = service._consume_grant

    def consumed_then_crash(*args, **kwargs):
        consume(*args, **kwargs)
        raise KeyboardInterrupt("consumed before dispatch")

    with monkeypatch.context() as old:
        old.setattr(service, "MARGIN_BASIS", OLD_BASIS)
        old.setattr(service, "_consume_grant", consumed_then_crash)
        with pytest.raises(KeyboardInterrupt, match="consumed before dispatch"):
            service.apply(connection, plan_id, grant=grant, **options)
    token = options["store"].parent / "approvals" / f"approval-{grant.approval.nonce}.used"
    token_bytes = token.read_bytes()
    directory = options["store"] / plan_id
    original, journal = original_bytes(directory), (directory / "journal.jsonl").read_bytes()
    assert compatibility.review_legacy_plan(plan_id, **options)["state"] == "requires_reprepare"
    retired = compatibility.retire_legacy_unstarted(plan_id, approver=product_tests.ACTOR, **options)
    fresh = product_tests.prepare(system, expected=retired["revision"], operation="prepare-after-consumed")
    assert fresh["selected_plan_id"] != plan_id
    assert_original_bytes(directory, original)
    assert (directory / "journal.jsonl").read_bytes() == journal
    assert token.read_bytes() == token_bytes and connection.writes == []


def test_completed_journal_prefix_without_pending_dispatch_can_resume(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = old_product(system, monkeypatch)
    append = Journal.append

    def verified_then_crash(journal, event, data):
        append(journal, event, data)
        if event == "verified":
            raise KeyboardInterrupt("completed prefix")

    with monkeypatch.context() as old:
        old.setattr(service, "MARGIN_BASIS", OLD_BASIS)
        old.setattr(Journal, "append", verified_then_crash)
        with pytest.raises(KeyboardInterrupt, match="completed prefix"):
            service.apply(connection, plan_id, grant=grant, **options)
    directory = options["store"] / plan_id
    original, prefix = original_bytes(directory), (directory / "journal.jsonl").read_bytes()
    report, token = review_and_authorize(plan_id, options)
    assert report["completed_steps"] >= 1 and report["pending_dispatch"] is False
    assert service.apply(connection, plan_id, grant=grant,
                         legacy_authorization=token, **options)["execution_validated"] is True
    assert len(connection.writes) == len(set(connection.writes))
    assert_original_bytes(directory, original)
    assert (directory / "journal.jsonl").read_bytes().startswith(prefix)


def test_recovery_authorization_requires_the_original_product_owner(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, _ = partial_product(system, monkeypatch)
    report = compatibility.review_legacy_plan(plan_id, **options)
    with pytest.raises(service.RecoveryError, match="approval_approver_mismatch"):
        compatibility.authorize_legacy_resume(plan_id, report["review_id"],
            approver="different-operator", **options)
    assert len(connection.writes) == 1


def test_actual_invalid_margin_in_old_partial_plan_blocks_recovery(system, monkeypatch):
    connection, _, options, _ = system
    request, snapshot, _, plan = stock_margin_fixture(unit_cost=76, floor=0.5)
    replacement = Fake(snapshot)
    connection.__dict__.update(replacement.__dict__)
    monkeypatch.setattr(service, "collect_snapshot", lambda *args: copy.deepcopy(snapshot))
    monkeypatch.setattr(service, "solve_blueprint", lambda *args: copy.deepcopy(plan))
    predicate = change_set.margin_satisfied
    with monkeypatch.context() as old:
        old.setattr(service, "MARGIN_BASIS", OLD_BASIS)
        old.setattr(change_set, "margin_satisfied",
                    lambda cost, revenue, floor: predicate(cost, 1000, floor))
        prepared = service.prepare(connection, request, **options)
    plan_id = prepared["plan_id"]
    interrupt_after_first_write(system, monkeypatch, plan_id, legacy_check=True)
    directory = options["store"] / plan_id
    originals = original_bytes(directory)
    journal = (directory / "journal.jsonl").read_bytes()
    report = compatibility.review_legacy_plan(plan_id, **options)
    assert report["state"] == "recovery_blocked"
    assert report["reason"] == "corrected_projection_rejected"
    with pytest.raises(service.RecoveryError, match="legacy_recovery_not_resumable"):
        compatibility.authorize_legacy_resume(plan_id, report["review_id"],
            approver=product_tests.ACTOR, **options)
    assert_original_bytes(directory, originals)
    assert (directory / "journal.jsonl").read_bytes() == journal
    assert len(connection.writes) == 1


def test_valid_journal_progress_invalidates_prior_review_and_remaining_steps(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = partial_product(system, monkeypatch)
    original, token = review_and_authorize(plan_id, options)
    journal_path = options["store"] / plan_id / "journal.jsonl"
    with Journal(journal_path) as journal:
        pending = copy.deepcopy(journal.rows[-1]["data"])
        assert journal.rows[-1]["event"] == "dispatch"
        journal.append("reconciled", pending)
    current = compatibility.review_legacy_plan(plan_id, **options)
    assert current["journal_sha256"] != original["journal_sha256"]
    assert current["remaining_steps_sha256"] != original["remaining_steps_sha256"]
    assert current["completed_steps"] == original["completed_steps"] + 1
    with pytest.raises(service.RecoveryError, match="legacy_recovery_review_changed"):
        compatibility.authorize_legacy_resume(plan_id, original["review_id"],
            approver=product_tests.ACTOR, **options)
    with pytest.raises(service.RecoveryError, match="legacy_recovery_review_changed"):
        service.apply(connection, plan_id, grant=grant, legacy_authorization=token, **options)
    assert len(connection.writes) == 1


def test_changed_current_source_bundle_invalidates_the_recovery_review(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = partial_product(system, monkeypatch)
    original, token = review_and_authorize(plan_id, options)
    replace_current_bundle_revision(options)
    current = compatibility.review_legacy_plan(plan_id, **options)
    assert current["current_sources_sha256"] != original["current_sources_sha256"]
    assert current["original_sources_sha256"] == original["original_sources_sha256"]
    with pytest.raises(service.RecoveryError, match="legacy_recovery_review_changed"):
        service.apply(connection, plan_id, grant=grant, legacy_authorization=token, **options)
    assert len(connection.writes) == 1


def test_reapproved_new_source_bundle_resumes_identical_original_steps(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, grant = partial_product(system, monkeypatch)
    directory = options["store"] / plan_id
    originals = original_bytes(directory)
    replace_current_bundle_revision(options)
    report, token = review_and_authorize(plan_id, options)
    assert report["current_sources_sha256"] != report["original_sources_sha256"]
    result = service.apply(connection, plan_id, grant=grant, legacy_authorization=token, **options)
    assert result["execution_validated"] is True
    assert len(connection.writes) == len(set(connection.writes))
    assert_original_bytes(directory, originals)


def test_changed_compilation_cannot_resume_the_original_journal(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, _ = partial_product(system, monkeypatch)
    compile_steps = scope_guard.compile_steps

    def changed_compiler(*args, **kwargs):
        result = compile_steps(*args, **kwargs)
        steps = list(result["steps"])
        steps[-1] = replace(steps[-1], key=steps[-1].key + "-changed")
        return {**result, "steps": steps}

    monkeypatch.setattr(scope_guard, "compile_steps", changed_compiler)
    with pytest.raises(service.RecoveryError, match="original_execution_binding_changed"):
        compatibility.review_legacy_plan(plan_id, **options)
    assert len(connection.writes) == 1


def test_corrupt_journal_cannot_be_reviewed_or_authorized(system, monkeypatch):
    connection, _, options, _ = system
    plan_id, _ = partial_product(system, monkeypatch)
    path = options["store"] / plan_id / "journal.jsonl"
    path.write_bytes(path.read_bytes() + b'{"incomplete":')
    with pytest.raises(ExecutionStopped, match="incomplete_journal_record"):
        compatibility.review_legacy_plan(plan_id, **options)
    assert len(connection.writes) == 1
