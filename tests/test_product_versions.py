"""Root product-version behavior with invented ERP state, never a benchmark pass."""
from __future__ import annotations

import copy
import io
import multiprocessing
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_executor_daemon as daemon_tests
import test_recovery_service as recovery_tests

from forgeflow.approval import Approval, ApprovalError, issue
from forgeflow.executor.daemon import Handlers
from forgeflow.executor.handlers import RecoveryHandlers, approval_wire_record
from forgeflow.executor.protocol import MAX_FRAME_BYTES, write_frame
from forgeflow.recovery import client, service
from forgeflow.recovery import product_versions as versions_module
from forgeflow.recovery.product_versions import ProductVersions, VersionError, owner_scope
from forgeflow.recovery.storage import atomic_json, digest, read_json

RUN = "local-" + "c" * 32
ACTOR = "test-owner"
OWNER = owner_scope(ACTOR)
system = recovery_tests.system
_contract = recovery_tests._contract
workspace = daemon_tests.workspace
handlers = daemon_tests.handlers
serve = daemon_tests.serve


def prepare(system, expected=0, operation="prepare-1"):
    connection, request, options, _ = system
    return service.prepare_product(connection, request, run_id=RUN, owner_sha256=OWNER,
                                   expected_revision=expected, operation_id=operation,
                                   contract=_contract(), **options)


def intent():
    now = datetime.now(UTC)
    return {"nonce": uuid.uuid4().hex, "approver": ACTOR,
            "issued_at_utc": now.isoformat(), "expires_at_utc": (now + timedelta(minutes=15)).isoformat()}


def authorize(options, decision, request=None):
    return service.authorize_product(
        run_id=RUN, owner_sha256=OWNER, plan_id=decision["selected_plan_id"],
        expected_revision=decision["revision"], intent=intent() if request is None else request,
        store=options["store"],
    )


def grant_from(payload):
    approval = Approval(**{k: v for k, v in payload.items() if k != "schema_version"})
    return service.ExecutionGrant(approval, RUN, approval.policy_sha256,
                                  approval.snapshot_sha256, approval.approver)


def manual_grant(options, decision):
    selected = decision["selected_plan_id"]
    row = decision["plans"][selected]
    token = issue(options["store"].parent / "approvals", run_id=RUN, plan_id=selected,
                  policy_sha256=row["policy_sha256"], snapshot_sha256=row["snapshot_sha256"],
                  approver=ACTOR)
    return grant_from(token.payload())


def test_product_prepare_binds_version_and_policy_without_mutating_original_contract(system, monkeypatch):
    connection, _, options, _ = system
    decision = prepare(system)
    assert decision["state"] == "selected" and decision["revision"] == 2
    plan = decision["selected_plan_id"]
    manifest = read_json(options["store"] / plan / "manifest.json")
    assert manifest["product"]["version"] == 1
    assert read_json(options["store"] / plan / "product-policy.json") == _contract().payload()
    assert not (options["store"].parent / "policy.json").exists()
    assert connection.writes == []
    monkeypatch.setattr(service, "collect_snapshot", lambda *a: pytest.fail("must not prepare twice"))
    assert prepare(system) == decision


def test_token_without_root_selection_approval_cannot_execute(system):
    connection, _, options, _ = system
    decision = prepare(system)
    grant = manual_grant(options, decision)
    with pytest.raises(VersionError, match="product_approval_required"):
        service.apply(connection, decision["selected_plan_id"], grant=grant, **options)
    assert connection.writes == []


def test_rejected_plan_remains_retired_even_with_previously_created_token(system):
    connection, _, options, _ = system
    decision = prepare(system)
    grant = manual_grant(options, decision)
    registry = ProductVersions(options["store"])
    rejected = registry.decide(RUN, OWNER, expected_revision=2, operation_id="reject-1",
                               action="reject", plan_id=decision["selected_plan_id"])
    assert rejected["state"] == "retired"
    assert registry.decide(RUN, OWNER, expected_revision=2, operation_id="reject-1",
                           action="reject", plan_id=decision["selected_plan_id"]) == rejected
    with pytest.raises(VersionError, match="product_plan_retired"):
        service.apply(connection, decision["selected_plan_id"], grant=grant, **options)
    assert connection.writes == []


def test_failed_revision_does_not_restore_old_plan_or_authority(system, monkeypatch):
    connection, _, options, _ = system
    decision = prepare(system)
    old = decision["selected_plan_id"]
    grant = manual_grant(options, decision)

    def fail(*a):
        raise RuntimeError("controlled solver failure")

    monkeypatch.setattr(service, "solve_blueprint", fail)
    with pytest.raises(RuntimeError, match="controlled solver"):
        prepare(system, expected=2, operation="prepare-2")
    registry = ProductVersions(options["store"])
    current = registry.status(RUN, OWNER)
    assert current["state"] == "prepare_failed" and current["version"] == 2
    assert current["selected_plan_id"] is None
    assert current["plans"][old]["state"] == "retired"
    with pytest.raises(VersionError, match="product_plan_retired"):
        registry.decide(RUN, OWNER, expected_revision=current["revision"],
                        operation_id="restore-old", action="select", plan_id=old)
    with pytest.raises(VersionError, match="product_plan_retired"):
        service.apply(connection, old, grant=grant, **options)
    assert connection.writes == []


def test_same_business_plan_new_version_gets_new_binding_and_old_is_permanently_retired(system):
    _, _, options, _ = system
    first = prepare(system)
    second = prepare(system, expected=first["revision"], operation="prepare-2")
    assert first["selected_plan_id"] != second["selected_plan_id"]
    assert second["version"] == 2
    assert second["plans"][first["selected_plan_id"]]["state"] == "retired"
    assert second["plans"][first["selected_plan_id"]]["retired_reason"] == "revision"
    with pytest.raises(VersionError, match="product_revision_conflict"):
        prepare(system, expected=0, operation="prepare-1")
    # This verifies distinct version bindings, not distinct business candidates.
    assert ProductVersions(options["store"]).status(RUN, OWNER) == second


def test_authorization_replay_and_apply_use_one_token_and_one_set_of_writes(system):
    connection, _, options, _ = system
    decision = prepare(system)
    data = intent()
    authorized = authorize(options, decision, data)
    assert authorize(options, decision, data) == authorized
    grant = grant_from(authorized["approval"])
    result = service.apply(connection, decision["selected_plan_id"], grant=grant, **options)
    writes = list(connection.writes)
    assert result["execution_validated"] is True and writes
    assert service.apply(connection, decision["selected_plan_id"], grant=grant, **options) == result
    assert connection.writes == writes
    assert len(list((options["store"].parent / "approvals").glob("approval-*.json"))) == 1
    assert len(list((options["store"].parent / "approvals").glob("*.used"))) == 1
    assert ProductVersions(options["store"]).status(RUN, OWNER)["state"] == "executed"


@pytest.mark.parametrize("stage", ["before_issue", "after_issue"])
def test_lost_authorization_response_keeps_old_version_locked_and_reuses_same_nonce(system, monkeypatch, stage):
    _, _, options, _ = system
    decision = prepare(system)
    data = intent()
    if stage == "before_issue":
        from forgeflow import approval
        original = approval.issue

        def fail(*a, **kw):
            raise OSError("controlled crash before token")

        monkeypatch.setattr(approval, "issue", fail)
    else:
        original = versions_module.atomic_json

        def fail(path, value, **kw):
            if value.get("state") == "approved":
                raise OSError("controlled crash after token")
            return original(path, value, **kw)

        monkeypatch.setattr(versions_module, "atomic_json", fail)
    with pytest.raises(OSError):
        authorize(options, decision, data)
    state = ProductVersions(options["store"]).status(RUN, OWNER)
    assert state["state"] == "approval_issuing"
    with pytest.raises(VersionError, match="product_already_authorized"):
        prepare(system, expected=state["revision"], operation="prepare-2")
    if stage == "before_issue":
        monkeypatch.setattr(approval, "issue", original)
    else:
        monkeypatch.setattr(versions_module, "atomic_json", original)
    recovered = authorize(options, decision, data)
    assert recovered["approval"]["nonce"] == data["nonce"]
    assert len(list((options["store"].parent / "approvals").glob("approval-*.json"))) == 1


@pytest.mark.parametrize("change", ["owner", "expired", "future", "extra", "nonce", "boolean_revision"])
def test_invalid_authorization_does_not_issue_any_token(system, change):
    connection, _, options, _ = system
    decision, data = prepare(system), intent()
    if change == "owner":
        data["approver"] = "someone-else"
    elif change == "expired":
        data["issued_at_utc"] = "2020-01-01T00:00:00+00:00"
        data["expires_at_utc"] = "2020-01-01T00:15:00+00:00"
    elif change == "future":
        data["issued_at_utc"] = "2099-01-01T00:00:00+00:00"
        data["expires_at_utc"] = "2099-01-01T00:15:00+00:00"
    elif change == "extra":
        data["instructions"] = "PRIVATE_CANARY"
    elif change == "nonce":
        data["nonce"] = "../elsewhere"
    else:
        decision["revision"] = True
    with pytest.raises((VersionError, service.RecoveryError)):
        authorize(options, decision, data)
    assert connection.writes == []
    assert not list((options["store"].parent / "approvals").glob("approval-*.json"))


def test_cross_owner_status_and_cas_conflict_preserve_state(system):
    _, _, options, _ = system
    state = prepare(system)
    registry = ProductVersions(options["store"])
    with pytest.raises(VersionError, match="product_owner_mismatch"):
        registry.status(RUN, owner_scope("someone-else"))
    with pytest.raises(VersionError, match="product_revision_conflict"):
        registry.decide(RUN, OWNER, expected_revision=1, operation_id="stale-reject", action="reject")
    assert registry.status(RUN, OWNER) == state


def _race(store, initial, data, action, start, queue):
    start.wait(5)
    try:
        if action == "authorize":
            authorize({"store": Path(store)}, initial, data)
        else:
            ProductVersions(Path(store)).decide(RUN, OWNER, expected_revision=initial["revision"],
                                                operation_id="reject-race", action="reject",
                                                plan_id=initial["selected_plan_id"])
        queue.put((action, "passed"))
    except Exception as error:
        queue.put((action, type(error).__name__))


def test_real_processes_racing_approval_and_retirement_have_one_winner(system):
    connection, _, options, _ = system
    initial, data = prepare(system), intent()
    context = multiprocessing.get_context("spawn")
    start, queue = context.Event(), context.Queue()
    children = [context.Process(target=_race, args=(str(options["store"]), initial, data, action, start, queue))
                for action in ("authorize", "reject")]
    for child in children:
        child.start()
    start.set()
    try:
        results = [queue.get(timeout=10) for _ in children]
        assert [result for _, result in results].count("passed") == 1
        assert all(result in {"passed", "VersionError", "StorageError"} for _, result in results)
        state = ProductVersions(options["store"]).status(RUN, OWNER)
        assert state["state"] in {"approved", "retired"}
        assert len(list((options["store"].parent / "approvals").glob("approval-*.json"))) == (
            1 if state["state"] == "approved" else 0)
        assert connection.writes == []
    finally:
        for child in children:
            child.join(timeout=5)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
        queue.close()


def test_comparison_rejects_different_fact_snapshots_without_selecting_any(tmp_path):
    registry = ProductVersions(tmp_path / "store")

    def produce(context):
        plans = []
        for number in range(2):
            manifest = {"product": context, "sources": {"code": "fixed"},
                        "artifacts": {"snapshot": str(number) * 64, "baseline": "b" * 64, "request": "d" * 64}}
            plan = digest(manifest)
            directory = registry.store / plan
            directory.mkdir()
            atomic_json(directory / "manifest.json", manifest)
            atomic_json(directory / "prepared.json", {"policy_sha256": "c" * 64, "plan_id": plan, "ok": True, "odoo_writes": False})
            plans.append(plan)
        return plans

    with pytest.raises(VersionError, match="product_comparison_facts_mismatch"):
        registry.prepare(RUN, OWNER, expected_revision=0, operation_id="prepare-1", inputs={}, produce=produce)
    assert registry.status(RUN, OWNER)["state"] == "prepare_failed"
    assert registry.status(RUN, OWNER)["selected_plan_id"] is None


@pytest.mark.parametrize("tamper", ["missing", "corrupt", "link", "fifo", "policy", "selection"])
def test_missing_or_changed_authority_fails_before_business_writes(system, tmp_path, tamper):
    connection, _, options, _ = system
    decision = prepare(system)
    token = authorize(options, decision)["approval"]
    path = options["store"] / "product-decisions" / f"{RUN}.json"
    if tamper == "missing":
        path.unlink()
    elif tamper == "corrupt":
        path.write_text("[]")
    elif tamper == "link":
        outside = tmp_path / "outside.json"
        path.rename(outside)
        path.symlink_to(outside)
    elif tamper == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif tamper == "selection":
        value = read_json(path)
        value["selected_plan_id"] = None
        atomic_json(path, value, replace=True)
    else:
        policy = options["store"] / decision["selected_plan_id"] / "product-policy.json"
        value = copy.deepcopy(_contract().payload())
        value["contract_id"] = "tampered"
        atomic_json(policy, value, replace=True)
    with pytest.raises((VersionError, service.RecoveryError, ValueError)):
        service.apply(connection, decision["selected_plan_id"], grant=grant_from(token), **options)
    assert connection.writes == []


def test_actual_socket_prepare_authorize_apply_replay_and_cross_owner_rejection(system, serve, monkeypatch):
    connection, request, options, _ = system
    policy = options["store"].parent / "reviewed-policy.json"
    atomic_json(policy, _contract().payload())
    verifier_calls = []

    def verifier():
        verifier_calls.append(True)
        return {"status": "passed", "strict_success": True, "evaluation_valid": True,
                "evaluation_scope": "product_original_verifier"}

    root = RecoveryHandlers(connection_factory=lambda: connection, policy_file=policy,
                            product_verifier=verifier, **options)
    server = serve(custom=Handlers(root.prepare, root.apply, root.status, product=root.product,
                                   reconcile=root.reconcile))
    address = str(server.socket_path)
    assert client.health(address)["capabilities"]["product_versions"] is True
    response = client.product(address, RUN, "prepare", {
        "owner_sha256": OWNER, "expected_revision": 0, "operation_id": "prepare-1",
        "request": request, "constraints": {},
    })
    decision = response["decision"]
    assert connection.writes == []
    with pytest.raises(client.ExecutorError) as error:
        client.product(address, RUN, "status", {"owner_sha256": owner_scope("another")})
    assert error.value.code == "product_forbidden"
    data = intent()
    authorized = client.product(address, RUN, "authorize", {
        "owner_sha256": OWNER, "expected_revision": decision["revision"],
        "plan_id": decision["selected_plan_id"], "intent": data,
    })
    assert connection.writes == []
    result = client.apply(address, RUN, decision["selected_plan_id"],
                          approval=approval_wire_record(authorized["approval"]))
    assert result["execution_validated"] is True
    writes = list(connection.writes)
    replay = client.apply(address, RUN, decision["selected_plan_id"],
                          approval=approval_wire_record(authorized["approval"]))
    assert replay == result and connection.writes == writes
    with pytest.raises(client.ExecutorError) as error:
        client.product(address, RUN, "decide", {
            "owner_sha256": OWNER, "expected_revision": authorized["decision"]["revision"],
            "operation_id": "reject-after-apply", "action": "reject",
            "plan_id": decision["selected_plan_id"],
        })
    assert error.value.reason == "product_already_authorized"
    from forgeflow.recovery import scope_guard

    monkeypatch.setattr(scope_guard, "collect_snapshot", lambda client, request: connection.collect(request))
    # The original global policy location is intentionally absent. Each product
    # plan retains its own bound policy; changing another pointer cannot replace it.
    assert not (options["store"].parent / "policy.json").exists()
    recovered = client.reconcile(address, RUN, decision["selected_plan_id"])
    again = client.reconcile(address, RUN, decision["selected_plan_id"])
    assert recovered == again and connection.writes == writes
    assert verifier_calls == [True]


@pytest.mark.parametrize("require_grant,has_verifier", [(False, False), (False, True), (True, False)])
def test_product_control_is_disabled_before_credentials_or_files_in_nonproduct_executor(
    tmp_path, serve, require_grant, has_verifier,
):
    root = RecoveryHandlers(connection_factory=lambda: pytest.fail("must not open ERP credentials"),
                            store=tmp_path / "missing-store", bundle=tmp_path / "missing-bundle",
                            require_grant=require_grant, product_verifier=object() if has_verifier else None)
    # Even if someone erroneously wires the callback, its own gate rejects it.
    server = serve(custom=Handlers(root.prepare, root.apply, root.status, product=root.product))
    with pytest.raises(client.ExecutorError) as error:
        client.product(str(server.socket_path), RUN, "status", {"owner_sha256": OWNER})
    assert error.value.code == "product_operations_disabled"
    assert not root.store.exists()


def test_registry_does_not_make_an_unselected_candidate_executable(tmp_path):
    registry = ProductVersions(tmp_path / "store")

    def produce(context):
        plans = []
        for number in range(2):
            # Two distinct bindings in a synthetic registry; this does not claim
            # two different feasible business solutions (P04 needs real evidence).
            manifest = {"product": context, "sources": {"code": "fixed"}, "candidate": number,
                        "artifacts": {"snapshot": "a" * 64, "baseline": "b" * 64, "request": "d" * 64}}
            plan = digest(manifest)
            directory = registry.store / plan
            directory.mkdir()
            atomic_json(directory / "manifest.json", manifest)
            atomic_json(directory / "prepared.json", {"policy_sha256": "c" * 64, "plan_id": plan, "ok": True, "odoo_writes": False})
            plans.append(plan)
        return plans

    state = registry.prepare(RUN, OWNER, expected_revision=0, operation_id="prepare-two",
                             inputs={}, produce=produce)
    first, second = state["plans"]
    assert state["state"] == "prepared" and state["selected_plan_id"] is None
    state = registry.decide(RUN, OWNER, expected_revision=state["revision"],
                            operation_id="select-first", action="select", plan_id=first)
    with pytest.raises(VersionError, match="product_plan_not_selected"):
        registry.assert_executable_locked(read_json(registry.store / second / "manifest.json"), second, None)
    assert state["selected_plan_id"] == first


@pytest.mark.parametrize("bad", [None, True, [], {}, "unknown"])
def test_product_wire_rejects_nonaction_before_handler(serve, bad):
    server = serve()
    with pytest.raises(client.ExecutorError) as error:
        client.request(str(server.socket_path), {"schema_version": 1, "op": "product",
                       "run_id": RUN, "action": bad, "payload": {}}, timeout=2)
    assert error.value.code == "invalid_field"


def test_legacy_source_manifest_remains_readable_but_cannot_prepare_product_version(system):
    connection, request, options, _ = system
    provenance = options["bundle"] / "provenance.json"
    record = read_json(provenance)
    record["files_sha256"].pop("forgeflow/recovery/product_versions.py")
    provenance.chmod(0o600)
    atomic_json(provenance, record, replace=True)
    assert service.verify_sources(options["bundle"]) == record
    # Existing autonomous/legacy protocol is unaffected. The new product
    # protocol requires its version authority among the pinned source inputs.
    assert service.prepare(connection, request, **options)["odoo_writes"] is False
    with pytest.raises(service.RecoveryError, match="incomplete_source_manifest"):
        prepare(system)
    assert connection.writes == []


def test_full_history_fits_wire_and_refuses_new_version_without_retiring_current(tmp_path):
    registry = ProductVersions(tmp_path / "store")

    def produce(context):
        manifest = {"product": context, "sources": {"code": "fixed"},
                    "artifacts": {"snapshot": "a" * 64, "baseline": "b" * 64, "request": "d" * 64}}
        plan = digest(manifest)
        directory = registry.store / plan
        directory.mkdir()
        atomic_json(directory / "manifest.json", manifest)
        atomic_json(directory / "prepared.json", {"policy_sha256": "c" * 64, "plan_id": plan,
                                                  "ok": True, "odoo_writes": False})
        return [plan]

    run_id = "r" * 120
    state = {"revision": 0}
    for n in range(versions_module.MAX_PLANS):
        state = registry.prepare(run_id, OWNER, expected_revision=state["revision"],
                                 operation_id=f"prepare-{n}", inputs={}, produce=produce)
    stream = io.BytesIO()
    write_frame(stream, {"schema_version": 1, "op": "product", "ok": True, "decision": state})
    assert len(stream.getvalue()) < MAX_FRAME_BYTES
    candidate_history = copy.deepcopy(state)
    for row in candidate_history["plans"].values():
        row["comparison_sha256"] = "e" * 64
    comparison_stream = io.BytesIO()
    write_frame(comparison_stream, {"schema_version": 1, "op": "product", "ok": True,
                                    "decision": candidate_history})
    assert len(comparison_stream.getvalue()) < MAX_FRAME_BYTES
    with pytest.raises(VersionError, match="product_history_limit"):
        registry.prepare(run_id, OWNER, expected_revision=state["revision"],
                         operation_id="over-limit", inputs={},
                         produce=lambda _: pytest.fail("must refuse before planning"))
    assert registry.status(run_id, OWNER) == state
    assert state["plans"][state["selected_plan_id"]]["state"] == "selected"


@pytest.mark.parametrize("bad", [None, True, [], {}, "unknown"])
def test_product_decision_rejects_malformed_nested_action(system, serve, bad):
    connection, _, options, _ = system
    root = RecoveryHandlers(connection_factory=lambda: pytest.fail("no ERP connection"),
                            product_verifier=lambda: None, **options)
    server = serve(custom=Handlers(root.prepare, root.apply, root.status, product=root.product))
    with pytest.raises(client.ExecutorError) as error:
        client.product(str(server.socket_path), RUN, "decide", {
            "owner_sha256": OWNER, "expected_revision": 0, "operation_id": "malformed",
            "action": bad, "plan_id": None,
        })
    assert error.value.code == "invalid_field"
    assert connection.writes == [] and not options["store"].exists()


def test_nonce_collision_never_overwrites_a_different_approval_or_dispatches(system):
    connection, _, options, _ = system
    decision = prepare(system)
    data = intent()
    token = issue(options["store"].parent / "approvals", run_id="different-run", plan_id="f" * 64,
                  policy_sha256="e" * 64, snapshot_sha256="d" * 64,
                  approver=ACTOR, nonce=data["nonce"])
    with pytest.raises(ApprovalError, match="approval_token_tampered"):
        authorize(options, decision, data)
    assert read_json(options["store"].parent / "approvals" / f"approval-{token.nonce}.json") == token.payload()
    assert ProductVersions(options["store"]).status(RUN, OWNER)["state"] == "approval_issuing"
    assert connection.writes == []
