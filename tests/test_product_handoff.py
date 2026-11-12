"""Trusted handoff routing with a real reader/planner and invented ERP facts.

No model, network, Docker or ERP writes are used. Dispatch failure tests exercise
the persistent environment guard independently of executor implementation.
"""
from __future__ import annotations

import copy
import hashlib
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_recovery_scope_closure import control_inputs
from test_recovery_scope_closure import real_bundle as real_bundle

from forgeflow.executor.handlers import RecoveryHandlers
from forgeflow.investigation.fixtures import FixtureConnection
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.product.executor import ProductExecutionError
from forgeflow.product.handoff import HandoffProductExecutor, HandoffRegistry, handoff_summary
from forgeflow.product.local import LocalProductExecutor
from forgeflow.recovery import client
from forgeflow.recovery.storage import atomic_json, digest, read_json

OWNER = "handoff-owner"


def binding(identity="1" * 32, *, cap=2000):
    case, records, request = control_inputs(auxiliary=False)
    policy = case.contract.payload()
    policy["budgets"]["1"] = float(cap)
    request["sales_order_budget_caps"]["1"] = cap
    event = {"text": case.event, "sha256": hashlib.sha256(case.event.encode()).hexdigest()}
    return {"owner": OWNER, "generation": "generation-1", "source_case": "ic-" + identity,
            "source_episode": "episode-1", "source_review_sha256": "a" * 64,
            "event": event, "request": request, "contract": policy,
            "currency": {"currency": "USD", "company_id": 1,
                         "record_currencies": [], "provenance": "invented fixture currency"},
            "product_case_id": identity}, records


def private_case(value):
    return {"case_id": value["product_case_id"], "scenario": "A",
            "_handoff": handoff_summary(value),
            "_owner_sha256": hashlib.sha256(value["owner"].encode()).hexdigest()}


@pytest.fixture
def wired(tmp_path, monkeypatch, real_bundle):
    handlers, executors, connections = {}, [], []
    generation = ["generation-1"]

    def factory(value, paths):
        _, records = binding()
        connection = FixtureConnection(records)
        root = RecoveryHandlers(connection_factory=lambda: connection, store=paths.store,
            bundle=real_bundle, approval_store=paths.approvals, policy_file=paths.policy,
            product_verifier=lambda: pytest.fail("preparation cannot run verifier"))
        executor = LocalProductExecutor(socket_path=str(paths.socket), store=paths.store,
            approvals=paths.approvals, request=paths.request, policy=paths.policy,
            handoff_binding=value, container="invented-no-docker", enable_candidates="review_profile" in value,
            review_connection=(lambda: connection) if "review_profile" in value else None)
        handlers[str(paths.socket)] = root
        connections.append(connection)
        executors.append(executor)
        return executor

    def product(socket, run_id, action, payload, **kwargs):
        return handlers[socket].product(run_id, action, payload)

    monkeypatch.setattr(client, "product", product)
    with tempfile.TemporaryDirectory(prefix="ff-handoff-", dir="/tmp") as short:
        registry = HandoffRegistry(tmp_path / "registry", socket_root=short,
                                  generation=lambda: generation[0], executor_factory=factory)
        router = HandoffProductExecutor(registry)
        cases = ProductCaseService(tmp_path / "product", router)
        yield SimpleNamespace(registry=registry, router=router, cases=cases,
            executors=executors, connections=connections, generation=generation, factory=factory,
            socket_root=Path(short), tmp_path=tmp_path)


def test_handoff_reprepares_exact_request_and_policy_with_real_reader_solver(wired):
    value, _ = binding()
    created = wired.cases.create_from_handoff(OWNER, value)
    identity = value["product_case_id"]
    assert created["case_id"] == identity and created["plan"] is None
    assert created["order"]["product"] == "FIN" and created["order"]["units"] is None
    assert created["order"]["due_days"] is None and wired.executors == []
    receipt = wired.cases.handoff_receipt(identity, OWNER)
    assert wired.cases.create_from_handoff(OWNER, copy.deepcopy(value)) == created
    prepared = wired.cases.prepare(identity, OWNER)
    assert prepared["state"] == "awaiting_approval", prepared
    assert prepared["decision"]["can_approve"] is True, prepared["plan"]["checks"]
    assert prepared["order"]["units"] == 10 and prepared["order"]["ref"] == "SO-1"
    assert prepared["order"]["order_count"] == 1
    assert prepared["plan"]["new_spend"] == 60
    assert prepared["plan"]["policy_sha256"] == digest(value["contract"])
    paths = wired.registry.paths(value)
    directory = paths.store / prepared["plan"]["plan_id"]
    assert read_json(directory / "request.json") == value["request"]
    manifest = read_json(directory / "manifest.json")
    assert manifest["product"]["run_id"] == "local-" + identity
    assert manifest["policy_sha256"] == digest(value["contract"])
    assert wired.executors[0].task == value["contract"]["task_id"]
    assert not list(paths.approvals.glob("approval-*"))
    assert wired.connections[0].write_attempts == []
    assert wired.cases.handoff_receipt(identity, OWNER) == receipt
    assert wired.cases.prepare(identity, OWNER) == prepared
    assert len(wired.executors) == 1


def test_distinct_handoffs_have_distinct_immutable_executors(wired):
    first, _ = binding()
    second, _ = binding("2" * 32, cap=1000)
    for value in (first, second):
        wired.cases.create_from_handoff(OWNER, value)
        result = wired.cases.prepare(value["product_case_id"], OWNER)
        assert result["state"] == "awaiting_approval", result
    left, right = wired.executors
    assert left.request != right.request
    assert left.store != right.store and left.approvals != right.approvals
    assert left.socket_path != right.socket_path
    assert len(left.socket_path.encode()) < 104
    assert left.handoff_sha256 != right.handoff_sha256
    assert all(not connection.write_attempts for connection in wired.connections)


@pytest.mark.parametrize("change", ["owner", "generation", "request", "policy", "binding", "metadata"])
def test_handoff_tampering_fails_before_factory_dispatch(wired, change):
    value, _ = binding()
    wired.cases.create_from_handoff(OWNER, value)
    case = private_case(value)
    paths = wired.registry.paths(value)
    if change == "owner":
        case["_owner_sha256"] = "f" * 64
    elif change == "generation":
        wired.generation[0] = "generation-2"
    elif change == "metadata":
        case["_handoff"]["source_review_sha256"] = "b" * 64
    else:
        path = {"request": paths.request, "policy": paths.policy,
                "binding": paths.directory / "binding.json"}[change]
        payload = read_json(path)
        payload["unexpected"] = "changed"
        atomic_json(path, payload, replace=True)
    with pytest.raises((ProductExecutionError, ValueError)):
        wired.router.preparation_intent(case)
    assert wired.executors == []


def test_handoff_idempotency_rejects_conflict_and_wrong_owner(wired):
    value, _ = binding()
    wired.cases.create_from_handoff(OWNER, value)
    with pytest.raises(ProductCaseError, match="owner_mismatch"):
        wired.cases.create_from_handoff("another-owner", value)
    changed = copy.deepcopy(value)
    changed["source_review_sha256"] = "b" * 64
    with pytest.raises(ProductCaseError, match="binding_changed"):
        wired.cases.create_from_handoff(OWNER, changed)
    assert wired.executors == []


def test_socket_paths_and_registry_symlinks_fail_closed(tmp_path):
    value, _ = binding()
    registry = HandoffRegistry(tmp_path / "root", socket_root=tmp_path / ("x" * 110),
        generation=lambda: "generation-1", executor_factory=lambda *_: pytest.fail("no dispatch"))
    with pytest.raises(ProductExecutionError, match="socket_path_too_long"):
        registry.register(value)
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    (tmp_path / "linked").symlink_to(alternate, target_is_directory=True)
    registry = HandoffRegistry(tmp_path / "linked", socket_root="/tmp",
        generation=lambda: "generation-1", executor_factory=lambda *_: pytest.fail("no dispatch"))
    with pytest.raises(ProductExecutionError, match="store_untrusted"):
        registry.register(value)


@pytest.mark.parametrize("termination", ["exception", "base_exception", "unverified_result"])
def test_unknown_execution_blocks_other_stores_after_restart(wired, termination):
    first, _ = binding()
    second, _ = binding("2" * 32)
    for value in (first, second):
        wired.registry.register(value)
    counts = {"apply": 0, "prepare": 0}

    def apply(*_):
        counts["apply"] += 1
        if termination == "base_exception":
            raise KeyboardInterrupt("independent interruption control")
        if termination == "exception":
            raise TimeoutError("independent response loss")
        return {"readback": {"execution_validated": False}}

    def prepare(*_):
        counts["prepare"] += 1

    executor = SimpleNamespace(apply=apply, prepare=prepare,
        reconcile=lambda *_: {"readback": {"execution_validated": False}},
        cancel_unstarted=lambda *_: {"outcome": "not_started", "approval_invalidated": False,
                                     "plan_retired": True})
    case = private_case(first)
    plan = {"plan_id": "a" * 64}
    if termination == "unverified_result":
        wired.registry.invoke("apply", case, (plan,), {}, lambda _: executor)
    else:
        with pytest.raises(KeyboardInterrupt if termination == "base_exception" else TimeoutError):
            wired.registry.invoke("apply", case, (plan,), {}, lambda _: executor)
    restarted = HandoffRegistry(wired.registry.root, socket_root=wired.socket_root,
        generation=lambda: "generation-1", executor_factory=wired.factory)
    # Different isolated product stores cannot reset the shared environment guard.
    other = private_case(second)
    for method in ("prepare", "authorize", "apply"):
        with pytest.raises(ProductExecutionError, match="execution_unresolved"):
            restarted.invoke(method, other, (plan,), {}, lambda _: executor)
    with pytest.raises(ProductExecutionError, match="execution_unresolved"):
        restarted.register(binding("3" * 32)[0])
    restarted.invoke("reconcile", case, (plan,), {}, lambda _: executor)
    restarted.invoke("cancel_unstarted", case, (plan,), {}, lambda _: executor)
    assert read_json(restarted.root / "environment.json")["pending"] is not None
    executor.reconcile = lambda *_: {"readback": {"execution_validated": True}}
    restarted.invoke("reconcile", case, (plan,), {}, lambda _: executor)
    assert read_json(restarted.root / "environment.json")["pending"] is None
    restarted.invoke("prepare", other, (), {}, lambda _: executor)
    assert counts == {"apply": 1, "prepare": 1}


def test_legacy_scenarios_share_environment_guard(wired):
    value, _ = binding()
    wired.registry.register(value)
    legacy = SimpleNamespace(supported_scenarios=("A", "B"), prepare=lambda _: "legacy-plan")
    router = HandoffProductExecutor(wired.registry, legacy=legacy)
    case = {"case_id": "b" * 32, "scenario": "B", "_owner_sha256": "c" * 64}
    assert router.prepare(case) == "legacy-plan"
    state = read_json(wired.registry.root / "environment.json")
    state["pending"] = {"case_id": "a" * 32, "plan_id": "d" * 64,
                        "owner_sha256": "e" * 64, "phase": "dispatching"}
    atomic_json(wired.registry.root / "environment.json", state, replace=True)
    with pytest.raises(ProductExecutionError, match="execution_unresolved"):
        router.prepare(case)


def test_real_handoff_approval_dispatch_loss_stays_blocked(wired, monkeypatch):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(identity, OWNER)
    dispatches = []

    def unavailable(*args, **kwargs):
        dispatches.append((args, kwargs))
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "apply", unavailable)
    unknown = wired.cases.approve(identity, OWNER, prepared["plan"]["plan_id"])
    assert unknown["state"] == "execution_unknown", unknown
    assert len(dispatches) == 1 and dispatches[0][1]["approval"]["plan_id"] == prepared["plan"]["plan_id"]
    assert read_json(wired.registry.root / "environment.json")["pending"]["phase"] == "unknown"
    assert wired.connections[0].write_attempts == []
    # Approval retry cannot dispatch a second write.
    assert wired.cases.approve(identity, OWNER, prepared["plan"]["plan_id"])["state"] == "execution_unknown"
    assert len(dispatches) == 1
    with pytest.raises(ProductCaseError, match="execution_unresolved"):
        wired.cases.create_from_handoff(OWNER, binding("2" * 32)[0])


def test_only_original_plan_and_owner_can_close_unknown_execution(wired):
    value, _ = binding()
    wired.registry.register(value)
    case, plan = private_case(value), {"plan_id": "a" * 64}
    state = read_json(wired.registry.root / "environment.json")
    state["pending"] = {"case_id": case["case_id"], "plan_id": plan["plan_id"],
                        "owner_sha256": case["_owner_sha256"], "phase": "unknown"}
    atomic_json(wired.registry.root / "environment.json", state, replace=True)
    executor = SimpleNamespace(cancel_unstarted=lambda *_: {"outcome": "not_started",
        "approval_invalidated": True, "plan_retired": True})
    for wrong_case, wrong_plan in ((case, {"plan_id": "b" * 64}),
                                   ({**case, "_owner_sha256": "c" * 64}, plan)):
        with pytest.raises(ProductExecutionError, match="execution_unresolved"):
            wired.registry.invoke("cancel_unstarted", wrong_case, (wrong_plan,), {}, lambda _: executor)
    wired.registry.invoke("cancel_unstarted", case, (plan,), {}, lambda _: executor)
    closed = read_json(wired.registry.root / "environment.json")
    assert closed["pending"] is None and closed["last_closed"]["plan_id"] == plan["plan_id"]


def test_handoff_closure_requires_terminal_case_and_environment_clear(wired):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    with pytest.raises(ProductCaseError, match="not_closed"):
        wired.cases.handoff_closure_receipt(identity, OWNER)
    prepared = wired.cases.prepare(identity, OWNER)
    with pytest.raises(ProductCaseError, match="not_closed"):
        wired.cases.handoff_closure_receipt(identity, OWNER)
    wired.cases.reject(identity, OWNER, plan_id=prepared["plan"]["plan_id"])
    receipt = wired.cases.handoff_closure_receipt(identity, OWNER)
    assert receipt["outcome"] == "never_approved" and receipt["environment_clear"] is True
    assert receipt["case_record_sha256"] == digest(wired.cases._read(identity).to_record())
    assert wired.cases.handoff_closure_receipt(identity, OWNER) == receipt
    state = read_json(wired.registry.root / "environment.json")
    state["pending"] = {"case_id": "a" * 32, "plan_id": "d" * 64,
                        "owner_sha256": "e" * 64, "phase": "unknown"}
    atomic_json(wired.registry.root / "environment.json", state, replace=True)
    with pytest.raises(ProductCaseError, match="execution_unresolved"):
        wired.cases.handoff_closure_receipt(identity, OWNER)


def test_changed_currency_guard_blocks_before_authorization(wired):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(identity, OWNER)

    def changed(_):
        raise ProductExecutionError("product_handoff_currency_changed")

    wired.executors[0].binding_guard = changed
    result = wired.cases.approve(identity, OWNER, prepared["plan"]["plan_id"])
    assert result["reason"] == "approval_authorization_pending"
    assert wired.cases._read(identity).approval["authorization_error"] == "product_handoff_currency_changed"
    assert not list(wired.executors[0].approvals.glob("approval-*"))
    assert wired.connections[0].write_attempts == []


def test_bound_review_preserves_accepted_facts_and_retires_a_stale_deadline(wired, monkeypatch):
    from forgeflow.investigation.fixtures import SCHEMAS
    monkeypatch.setitem(SCHEMAS, "sale.order", SCHEMAS["sale.order"] | {"write_date"})
    value, _ = binding()
    value["review_profile"] = "recovery-review-v1"
    cases, identity = wired.cases, value["product_case_id"]
    cases.create_from_handoff(OWNER, value)
    current = cases.prepare(identity, OWNER)
    assert current.get("comparison", {}).get("status") == "ready", current.get("reason")
    if current["plan"] is None:
        comparison = current["comparison"]
        current = cases.select_candidate(identity, OWNER, comparison["plans"][0]["plan_id"],
            comparison_id=comparison["comparison_id"], selection_revision=comparison["selection_revision"])
    plan = current["plan"]["plan_id"]
    params = dict(plan_id=plan, expected_version=1, order_id=1)
    assert current["decision"]["can_review"] is True
    assert cases.review_sources(identity, OWNER, plan)["rows"]
    cases.open_review(identity, OWNER, operation_id="note1", kind="reason", note="Keep the agreed order", **params)
    first = cases.open_review(identity, OWNER, operation_id="first", kind="deadline", note="Check delivery", **params)
    assert first["decision"]["can_approve"] is False
    with pytest.raises(ProductCaseError, match="review_pending"):
        cases.approve(identity, OWNER, plan)
    with pytest.raises(ProductCaseError, match="review_operation_conflict"):
        cases.open_review(identity, OWNER, operation_id="first", kind="deadline", note="Changed text", **params)
    with pytest.raises(ProductCaseError):
        cases.check_review(identity, "different-owner", "first")
    records = wired.connections[0].records["sale.order"]
    records[0]["write_date"] = "2026-10-01 00:00:00"
    kept, kept_prepared = cases.check_review(identity, OWNER, "first", return_prepare=True)
    assert kept_prepared is False
    assert kept["reviews"][-1]["status"] == "retained", kept["reviews"][-1]
    assert kept["plan"]["plan_id"] == plan and kept["decision"]["can_approve"]
    cases.open_review(identity, OWNER, operation_id="second", kind="deadline", note="New notice", **params)
    from datetime import datetime, timedelta
    records[0]["commitment_date"] = str(datetime.fromisoformat(records[0]["commitment_date"]) + timedelta(days=1))
    corrected, corrected_prepared = cases.check_review(
        identity, OWNER, "second", return_prepare=True)
    assert corrected_prepared is True
    assert corrected["reviews"][-1]["status"] == "corrected", corrected["reviews"][-1]
    assert corrected["revision"]["version"] == 2 and corrected["revision"]["parent_plan_id"] == plan
    with pytest.raises(ProductCaseError):
        cases.approve(identity, OWNER, plan)
    replayed, replayed_prepared = cases.check_review(
        identity, OWNER, "second", return_prepare=True)
    assert replayed_prepared is False
    assert replayed["reviews"] == corrected["reviews"]


def review_case(wired):
    value, _ = binding()
    value["review_profile"] = "recovery-review-v1"
    cases, identity = wired.cases, value["product_case_id"]
    cases.create_from_handoff(OWNER, value)
    current = cases.prepare(identity, OWNER)
    if current["plan"] is None:
        comparison = current["comparison"]
        current = cases.select_candidate(identity, OWNER, comparison["plans"][0]["plan_id"],
            comparison_id=comparison["comparison_id"], selection_revision=comparison["selection_revision"])
    return identity, current["plan"]["plan_id"]


def test_unverified_external_factor_is_held_after_restart_without_any_grant(wired):
    identity, plan = review_case(wired)
    params = dict(plan_id=plan, expected_version=1, order_id=1, note="Night shift unavailable",
                  kind="unmodeled", operation_id="lead")
    with pytest.raises(ProductCaseError):
        wired.cases.open_review(identity, "observer", **params)
    with pytest.raises(ProductCaseError):
        wired.cases.open_review("2" * 32, OWNER, **params)
    for invalid in ({"expected_version": 2}, {"order_id": 9000}, {"plan_id": "f" * 64}):
        with pytest.raises(ProductCaseError):
            wired.cases.open_review(identity, OWNER, **{**params, **invalid})
    view = wired.cases.open_review(identity, OWNER, **params)
    restarted = ProductCaseService(wired.cases.root, wired.router)
    assert restarted.open_review(identity, OWNER, **params)["reviews"] == view["reviews"]
    held = restarted.check_review(identity, OWNER, "lead")
    assert held["reviews"][-1]["status"] == "held" and held["plan"]["plan_id"] == plan
    assert held["approval"] is None and held["execution"] is None and not held["decision"]["can_approve"]
    with pytest.raises(ProductCaseError):
        restarted.approve(identity, OWNER, plan)
    assert not wired.connections[0].write_attempts


def test_objection_and_approval_compete_under_one_case_lock(wired, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    identity, plan = review_case(wired)
    other = ProductCaseService(wired.cases.root, wired.router)
    barrier = Barrier(2)
    dispatches = []

    def unavailable(*args, **kwargs):
        dispatches.append(kwargs["approval"]["plan_id"])
        raise client.ExecutorError("unavailable")
    monkeypatch.setattr(client, "apply", unavailable)

    def call(method, *args, **kwargs):
        barrier.wait(timeout=5)
        try:
            return method(*args, **kwargs)
        except ProductCaseError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        objection = pool.submit(call, other.open_review, identity, OWNER, operation_id="race",
            plan_id=plan, expected_version=1, kind="deadline", order_id=1, note="Check date")
        approval = pool.submit(call, wired.cases.approve, identity, OWNER, plan)
        results = [objection.result(timeout=20), approval.result(timeout=20)]
    assert sum(isinstance(r, dict) for r in results) == 1, results
    if dispatches:
        assert dispatches == [plan] and results[0] == "review_version_unavailable"
        assert other.approve(identity, OWNER, plan)["state"] == "execution_unknown"
        assert dispatches == [plan]
    else:
        assert results[1] == "review_pending" and results[0]["approval"] is None


def test_review_wire_rejects_supplied_disposition_or_new_constraints(tmp_path):
    from fastapi.testclient import TestClient

    from forgeflow.api.app import create_app
    from forgeflow.api.settings import Settings

    app = create_app(Settings(data_root=tmp_path / "runs", approvals_root=tmp_path / "auth",
        session_secret="test-session-secret-0123456789abcdef", invite_codes=("private-fixture-invite",), secure_cookie=False))
    client = TestClient(app, base_url="http://localhost", headers={"origin": "http://localhost"})
    assert client.post("/api/auth/session", json={"invite_code": "private-fixture-invite"}).status_code == 201
    params = {"operation_id": "lead", "plan_id": "a" * 64, "expected_version": 1,
              "kind": "deadline", "order_id": 1, "note": "Inspect source"}
    for field, value in (("evidence", {"current_value": "2030-01-01"}), ("resolution", "retained"),
                         ("budget", 1000), ("new_deadline", "2030-01-01")):
        response = client.post("/api/product/cases/" + "a" * 32 + "/reviews", json={**params, field: value})
        assert response.status_code == 422


def test_expired_contract_cannot_confirm_disputed_fact_but_can_close(wired, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from forgeflow.product import handoff

    identity, plan = review_case(wired)
    wired.cases.open_review(identity, OWNER, operation_id="review", kind="deadline", order_id=1,
        plan_id=plan, expected_version=1, note="Recheck before the review window closes")
    original = handoff.check_request
    def elapsed(request, contract):
        # Keep business request/policy immutable; test expiry at the next validation boundary.
        from dataclasses import replace
        expired = replace(contract, expires_at_utc=(datetime.now(UTC)-timedelta(seconds=1)).isoformat())
        return original(request, expired)
    monkeypatch.setattr(handoff, "check_request", elapsed)
    held = wired.cases.check_review(identity, OWNER, "review")
    assert held["reviews"][-1]["status"] == "held" and not held["decision"]["can_approve"]
    with pytest.raises(ProductCaseError):
        wired.cases.approve(identity, OWNER, plan)
    assert wired.cases.reject(identity, OWNER, plan_id=plan)["state"] == "rejected"
    assert not wired.connections[0].write_attempts


def test_interrupted_fact_revision_recovers_the_original_reservation(wired, monkeypatch):
    from datetime import datetime, timedelta

    from forgeflow.investigation.fixtures import SCHEMAS
    monkeypatch.setitem(SCHEMAS, "sale.order", SCHEMAS["sale.order"] | {"write_date"})
    identity, plan = review_case(wired)
    row = wired.connections[0].records["sale.order"][0]
    row.update(write_date="2026-10-01 00:00:00",
               commitment_date=str(datetime.fromisoformat(row["commitment_date"]) + timedelta(days=1)))
    params = dict(plan_id=plan, expected_version=1, order_id=1, note="Check date", kind="deadline", operation_id="recheck")
    wired.cases.open_review(identity, OWNER, **params)
    executor = wired.executors[0]
    original = executor.continue_revision
    def stop_after_root(case):
        original(case)
        raise KeyboardInterrupt
    with monkeypatch.context() as crash:
        crash.setattr(executor, "continue_revision", stop_after_root)
        with pytest.raises(KeyboardInterrupt):
            wired.cases.check_review(identity, OWNER, "recheck")
    restored = ProductCaseService(wired.cases.root, wired.router)
    pending = restored.get(identity, OWNER)
    assert pending["reviews"][-1]["status"] == "repreparing" and not pending["decision"]["can_approve"]
    new = restored.check_review(identity, OWNER, "recheck")
    assert new["revision"]["version"] == 2 and new["reviews"][-1]["status"] == "corrected"
    assert len(new["history"]) == 1
    assert restored.open_review(identity, OWNER, **params)["reviews"] == new["reviews"]
    assert restored.check_review(identity, OWNER, "recheck")["history"] == new["history"]
    assert not wired.connections[0].write_attempts


@pytest.mark.parametrize("replace_line", [False, True])
def test_clarified_purchase_must_exist_in_each_prepared_snapshot(wired, replace_line):
    value, records = binding()
    line = records["purchase.order.line"][0]
    purchase = next(po for po in records["purchase.order"] if po["id"] == line["order_id"])
    value["review_profile"] = "recovery-review-v1"
    value["confirmed_supply"] = {
        "purchase_order_id": purchase["id"], "supplier_id": purchase["partner_id"],
        "purchase_line_id": line["id"] + 100 if replace_line else line["id"],
        "product_id": line["product_id"],
    }
    case_id = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(case_id, OWNER)
    if replace_line:
        assert prepared["plan"] is None and not prepared["decision"]["can_approve"]
    else:
        assert prepared["plan"] is not None or prepared["comparison"]["plans"]
    assert wired.connections[0].write_attempts == []


def test_saved_review_plan_with_expanded_cancellation_cannot_be_approved(wired):
    value, _ = binding()
    value["review_profile"] = "recovery-review-v1"
    case_id = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(case_id, OWNER)
    plan = prepared.get("plan") or prepared["comparison"]["plans"][0]
    # Replay a manifest-bound, pre-fix blueprint against the authorization guard.
    # The old review binding intentionally has neither of the new subject IDs.
    executor = wired.executors[0]
    directory = wired.registry.paths(value).store / plan["plan_id"]
    manifest = read_json(directory / "manifest.json")
    blueprint = read_json(directory / "blueprint.json")
    repair = blueprint["repair_context"]
    repair["broken_purchase_order_refs"].append("SECOND-PO")
    manifest["artifacts"]["blueprint"] = digest(blueprint)
    old_id = digest(manifest)
    saved = directory.parent / old_id
    shutil.copytree(directory, saved)
    atomic_json(saved / "manifest.json", manifest, replace=True)
    atomic_json(saved / "blueprint.json", blueprint, replace=True)
    with pytest.raises(ProductExecutionError, match="review_notice_line_mismatch"):
        executor._check_saved_review_plan(private_case(value), old_id)
    case = wired.cases._owned(case_id, OWNER)
    if case.plan is None:
        comparison = wired.cases._load_comparison(case)
        plan = next(item for item in comparison["plans"] if item["plan_id"] == plan["plan_id"])
    else:
        plan = case.plan
    view = wired.cases._executor_view(case)
    assert wired.router.can_execute_plan(view, plan)
    tokens = plan["impact_chain"]["source_facts"]["disruption"]["broken_tokens"]
    tokens.append("existing:purchase.order:2")
    assert not wired.router.can_execute_plan(view, plan)
    assert not list(wired.registry.paths(value).approvals.glob("approval-*"))
    assert wired.connections[0].write_attempts == []


def test_historical_generation_change_keeps_case_readable(wired):
    value, _ = binding()
    identity = value["product_case_id"]
    wired.cases.create_from_handoff(OWNER, value)
    prepared = wired.cases.prepare(identity, OWNER)
    wired.generation[0] = "generation-2"
    view = wired.cases.get(identity, OWNER)
    assert view["state"] == prepared["state"]
    assert view["historical"] is True
    assert view["historical_reason"] == "environment_generation_changed"
    assert view["decision"]["can_approve"] is False
    assert view["decision"]["can_reject"] is False
    assert view["decision"]["can_cancel_unstarted"] is False
    listed = wired.cases.list_for(OWNER)
    assert any(row["case_id"] == identity for row in listed)


def test_historical_generation_change_keeps_candidate_evidence(wired):
    identity, plan_id = review_case(wired)
    wired.generation[0] = "generation-2"
    view = wired.cases.get(identity, OWNER)
    assert view["historical"] is True
    assert view["comparison"]["status"] == "ready"
    assert len(view["comparison"]["plans"]) >= 1
    assert view["plan"]["plan_id"] == plan_id
    assert view["decision"]["can_select_candidate"] is False
    assert view["decision"]["can_approve"] is False


def test_handoff_declares_never_accepted_closure_capability(wired):
    assert wired.router.supports_never_accepted_closure is True
