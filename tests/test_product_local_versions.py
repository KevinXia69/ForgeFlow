"""Real service/Local/socket wiring with invented ERP state and verifier."""
from __future__ import annotations

import copy
import json

import pytest
import test_product_versions as root_tests

from forgeflow.api.evidence import public_evidence
from forgeflow.executor.daemon import Handlers
from forgeflow.executor.handlers import RecoveryHandlers, approval_wire_record
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.product import local as local_module
from forgeflow.recovery import client, scope_guard, service
from forgeflow.recovery.product_versions import ProductVersions, owner_scope
from forgeflow.recovery.storage import atomic_json, read_json

system = root_tests.system
serve = root_tests.serve
workspace = root_tests.workspace
handlers = root_tests.handlers
ACTOR = "controlled-local-owner"


@pytest.fixture
def wired(system, serve, monkeypatch, tmp_path):
    connection, request, options, _ = system
    solver = service.solve_blueprint

    def complete_fixture_metrics(*args):
        result = solver(*args)
        plan = result["plan"]
        sales = sum(row["demand"] for row in plan["allocations"]) * connection.db["product.product"][1]["list_price"]
        plan["margin"] = 1 - plan["expected_new_spend"] / sales
        return result

    monkeypatch.setattr(service, "solve_blueprint", complete_fixture_metrics)
    policy = options["store"].parent / "reviewed-policy.json"
    atomic_json(policy, root_tests._contract().payload())
    verifications = []

    def verifier():
        verifications.append(True)
        return {"status": "passed", "source": "synthetic_executor",
                "strict_success": True, "evaluation_valid": True,
                "evaluation_scope": "product_original_verifier"}

    root = RecoveryHandlers(connection_factory=lambda: connection, policy_file=policy,
                            product_verifier=verifier, **options)
    server = serve(custom=Handlers(root.prepare, root.apply, root.status,
                                  product=root.product, reconcile=root.reconcile))
    # Constructor validates a test-only task configuration. Production A/B
    # contracts and scenario constants are not edited or used as test evidence.
    specs = copy.deepcopy(local_module.SCENARIOS)
    specs["B"].update(product_code=request["target_product_code"], disruption=request["disruption"])
    monkeypatch.setattr(local_module, "SCENARIOS", specs)
    path = tmp_path / "request.json"
    atomic_json(path, request)
    executor = local_module.LocalProductExecutor(
        socket_path=str(server.socket_path), store=options["store"],
        approvals=options["store"].parent / "approvals", request=path,
        container="synthetic-never-docker", scenario="B",
    )
    cases = ProductCaseService(tmp_path / "cases", executor)
    case = cases.create(ACTOR, "B")
    stored = cases._read(case["case_id"])
    stored.order["units"] = 10  # the invented fixture's demand, not real B data
    cases._write(stored)
    monkeypatch.setattr(scope_guard, "collect_snapshot", lambda client, request: connection.collect(request))
    return cases, case["case_id"], executor, connection, options, verifications, root


def test_local_case_uses_root_authority_and_exactly_one_execution(wired):
    cases, case_id, executor, connection, options, verifications, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    assert prepared["decision"]["can_approve"] is True, prepared["plan"]["checks"]
    assert "executor_binding" not in prepared["plan"]
    assert connection.writes == []
    plan_id = prepared["plan"]["plan_id"]
    stored = cases._read(case_id)
    assert stored.plan["executor_binding"]["product"]["owner_sha256"] == owner_scope(ACTOR)
    approved = cases.approve(case_id, ACTOR, plan_id, comment="keep this exact choice")
    assert approved["state"] == "succeeded", approved
    assert approved["result"]["simulation_passed"] is True
    writes = list(connection.writes)
    assert writes and verifications == [True]
    assert cases.approve(case_id, ACTOR, plan_id, comment="do not replace") == approved
    assert connection.writes == writes
    original = cases._read(case_id).approval
    tokens = list(executor.approvals.glob("approval-*.json"))
    assert len(tokens) == len(list(executor.approvals.glob("approval-*.used"))) == 1
    assert read_json(tokens[0])["nonce"] == original["nonce"]
    public = json.dumps(public_evidence(approved))
    for private in ("executor_binding", "executor_grant", "_owner_sha256", original["nonce"], ACTOR):
        assert private not in public
    root_state = ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert root_state["state"] == "executed"


@pytest.mark.parametrize("crash", ["before_issue", "after_issue", "after_authorized_save"])
def test_authorization_response_loss_or_restart_reuses_intent_without_apply(wired, monkeypatch, crash):
    cases, case_id, executor, connection, _, _, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    plan_id = prepared["plan"]["plan_id"]
    product = client.product
    calls = []

    def lose(*args, **kwargs):
        if args[2] != "authorize":
            return product(*args, **kwargs)
        calls.append(copy.deepcopy(args[3]["intent"]))
        if crash == "after_issue":
            product(*args, **kwargs)
        raise client.ExecutorError("unavailable", retryable=True)

    if crash == "after_authorized_save":
        write = cases._write

        def stop_after_confirmation(case):
            write(case)
            if (case.approval or {}).get("phase") == "authorized":
                raise KeyboardInterrupt("controlled process stop after durable authorization")

        monkeypatch.setattr(cases, "_write", stop_after_confirmation)
        with pytest.raises(KeyboardInterrupt):
            cases.approve(case_id, ACTOR, plan_id, comment="original intent")
    else:
        monkeypatch.setattr(client, "product", lose)
        pending = cases.approve(case_id, ACTOR, plan_id, comment="original intent")
        assert pending["state"] == "awaiting_approval" and pending["execution"] is None
        assert pending["decision"]["authorization_pending"] is True
        assert not pending["decision"]["can_reconcile"]
    saved = cases._read(case_id).approval
    assert connection.writes == []
    with pytest.raises(ProductCaseError, match="already_approved"):
        cases.reject(case_id, ACTOR)
    with pytest.raises(ProductCaseError, match="execution_not_started"):
        cases.reconcile(case_id, ACTOR)
    monkeypatch.setattr(client, "product", product)
    restarted = ProductCaseService(cases.root.parent, executor)
    result = restarted.approve(case_id, ACTOR, plan_id, comment="ignored retry comment")
    assert result["state"] == "succeeded", result
    final = restarted._read(case_id)
    assert final.approval["nonce"] == saved["nonce"]
    assert final.approval["comment"] == "original intent"
    assert sum(row["action"] == "approved" for row in final.audit) == 1
    assert sum(row["action"] == "approval_requested" for row in final.audit) == 1
    assert len(list(executor.approvals.glob("approval-*.json"))) == 1


def test_lost_apply_response_restarts_into_read_only_original_receipt(wired, monkeypatch):
    cases, case_id, executor, connection, _, verifications, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    plan = prepared["plan"]["plan_id"]
    real_apply = client.apply

    def lost(*args, **kwargs):
        real_apply(*args, **kwargs)
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "apply", lost)
    unknown = cases.approve(case_id, ACTOR, plan)
    assert unknown["state"] == "execution_unknown"
    writes = list(connection.writes)
    assert writes and verifications == []
    monkeypatch.setattr(client, "apply", lambda *a, **k: pytest.fail("must not send another apply"))
    restarted = ProductCaseService(cases.root.parent, executor)
    assert restarted.approve(case_id, ACTOR, plan) == unknown
    recovered = restarted.reconcile(case_id, ACTOR)
    assert recovered["state"] == "succeeded"
    assert connection.writes == writes and verifications == [True]


def test_rejection_lost_response_does_not_claim_success_and_retries_same_retirement(wired, monkeypatch):
    cases, case_id, _, connection, options, _, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    plan = prepared["plan"]["plan_id"]
    real = client.product

    def lost(*args, **kwargs):
        real(*args, **kwargs)
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "product", lost)
    with pytest.raises(ProductCaseError, match="unavailable"):
        cases.reject(case_id, ACTOR, plan_id=plan)
    assert cases.get(case_id, ACTOR)["state"] == "awaiting_approval"
    assert ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))["state"] == "retired"
    monkeypatch.setattr(client, "product", real)
    assert cases.reject(case_id, ACTOR, plan_id=plan)["state"] == "rejected"
    assert connection.writes == []


def test_legacy_product_writes_refused_but_saved_cases_remain_readable(wired):
    cases, case_id, _, connection, options, _, root = wired
    request = cases.executor.request
    with pytest.raises(Exception) as caught:
        root.prepare(f"local-{case_id}", request)
    assert caught.value.reason == "product_version_required"
    legacy = service.prepare(connection, request, contract=root_tests._contract(), **options)
    manifest = read_json(options["store"] / legacy["plan_id"] / "manifest.json")
    token = root_tests.issue(root.approval_store, run_id=f"local-{case_id}", plan_id=legacy["plan_id"],
                            policy_sha256=root_tests._contract().digest(),
                            snapshot_sha256=manifest["artifacts"]["snapshot"], approver=ACTOR)
    with pytest.raises(Exception) as caught:
        root.apply(f"local-{case_id}", legacy["plan_id"], approval_wire_record(token.payload()))
    assert caught.value.reason == "product_version_required"
    assert connection.writes == []
    # A recorded old card retains read access but cannot create an approval.
    from forgeflow.product.executor import build_fake_plan
    from forgeflow.product.state import normalize_plan

    old = cases._read(case_id)
    old.plan = normalize_plan(old, build_fake_plan(old.view()))
    old.state = "awaiting_approval"
    cases._write(old)
    viewed = cases.get(case_id, ACTOR)
    assert viewed["plan"] is not None and viewed["decision"]["reason"] == "product_version_required"
    with pytest.raises(ProductCaseError, match="product_version_required"):
        cases.approve(case_id, ACTOR, old.plan["plan_id"])
    assert cases._read(case_id).approval is None


def test_existing_legacy_execution_remains_read_only_reconcilable(wired):
    cases, case_id, _, connection, options, verifications, root = wired
    legacy = service.prepare(connection, cases.executor.request, contract=root_tests._contract(), **options)
    plan = legacy["plan_id"]
    manifest = read_json(options["store"] / plan / "manifest.json")
    run = f"local-{case_id}"
    token = root_tests.issue(root.approval_store, run_id=run, plan_id=plan,
                            policy_sha256=root_tests._contract().digest(),
                            snapshot_sha256=manifest["artifacts"]["snapshot"], approver=ACTOR)
    # Construct past legacy execution using the unchanged autonomous/library
    # service, then exercise only the new product root's read-only recovery.
    grant = service.ExecutionGrant(token, run, token.policy_sha256, token.snapshot_sha256, ACTOR)
    service.apply(connection, plan, grant=grant, **options)
    writes = list(connection.writes)
    receipt = root.reconcile(run, plan)
    assert receipt["execution_validated"] is True and verifications == [True]
    assert root.reconcile(run, plan) == receipt and connection.writes == writes


def test_durable_dispatch_intent_never_auto_resends_after_service_restart(wired, monkeypatch):
    cases, case_id, executor, connection, _, _, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    write = cases._write

    def stop_before_transport(case):
        write(case)
        if (case.approval or {}).get("phase") == "dispatching":
            raise KeyboardInterrupt("controlled stop after durable dispatch intent")

    monkeypatch.setattr(cases, "_write", stop_before_transport)
    with pytest.raises(KeyboardInterrupt):
        cases.approve(case_id, ACTOR, prepared["plan"]["plan_id"])
    monkeypatch.setattr(client, "apply", lambda *a, **k: pytest.fail("restart must not reapply"))
    restarted = ProductCaseService(cases.root.parent, executor)
    with pytest.raises(ProductCaseError, match="execution_in_progress"):
        restarted.approve(case_id, ACTOR, prepared["plan"]["plan_id"])
    assert connection.writes == []
    assert restarted.get(case_id, ACTOR)["decision"]["can_approve"] is False
    # No journal/receipt exists. Automatic resend stays forbidden; explicit
    # root-proven cancellation is covered separately in test_product_unstarted.
    assert restarted.get(case_id, ACTOR)["decision"]["can_cancel_unstarted"] is True


def test_history_projection_omits_root_binding_and_authorization_material(wired):
    cases, case_id, _, _, _, _, _ = wired
    cases.prepare(case_id, ACTOR)
    case = cases._read(case_id)
    case.history.append({"version": 1, "plan": copy.deepcopy(case.plan), "reason": "test"})
    cases._write(case)
    view = cases.get(case_id, ACTOR)
    assert "executor_binding" not in json.dumps(view)
    assert "executor_binding" in cases.path_for(case_id).read_text()


def test_root_retirement_conflict_is_terminal_not_a_retryable_authorization(wired):
    cases, case_id, _, connection, options, _, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    ProductVersions(options["store"]).decide(f"local-{case_id}", owner_scope(ACTOR),
        expected_revision=2, operation_id="external-retire", action="reject", plan_id=prepared["plan"]["plan_id"])
    refused = cases.approve(case_id, ACTOR, prepared["plan"]["plan_id"])
    assert refused["state"] == "failed" and refused["execution"] is None
    assert refused["reason"] == "product_plan_not_selected"
    assert not refused["decision"]["can_approve"]
    assert not refused["decision"].get("authorization_pending")
    assert connection.writes == []


@pytest.mark.parametrize("diagnostic_storage_fails", [False, True])
def test_stopped_apply_failure_is_bound_and_persisted_through_real_socket(wired, monkeypatch, diagnostic_storage_fails):
    from forgeflow.recovery.readback import ReadbackAdapter
    from forgeflow.recovery.storage import StorageError, digest

    cases, case_id, executor, connection, options, verifications, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    plan_id = prepared["plan"]["plan_id"]
    original_apply, original_observe = ReadbackAdapter.apply, ReadbackAdapter.observe
    wrote = False

    def apply_then_fail_read(self, step):
        nonlocal wrote
        original_apply(self, step)
        wrote = True

    def observe(self, step):
        if wrote:
            raise ValueError("Unexpected status code 503: private-failure-canary")
        return original_observe(self, step)

    monkeypatch.setattr(ReadbackAdapter, "apply", apply_then_fail_read)
    monkeypatch.setattr(ReadbackAdapter, "observe", observe)
    atomic = service.atomic_json

    def write(path, value, **kwargs):
        if diagnostic_storage_fails and path.name.startswith("execution-failure-"):
            raise StorageError("synthetic diagnostic storage failure")
        return atomic(path, value, **kwargs)

    monkeypatch.setattr(service, "atomic_json", write)
    stopped = cases.approve(case_id, ACTOR, plan_id)
    assert stopped["state"] == "execution_unknown" and stopped["reason"] == "mutation_outcome_unknown"
    assert stopped["result"]["verified"] is False and stopped["result"]["strict_success"] is None
    failure = stopped["execution"]["failure"]
    assert failure["plan_id"] == plan_id
    assert failure["context"] == {
        "stage": "observe_after", "reason": "mutation_outcome_unknown", "step_number": 1,
        "step_kind": "cancel_manufacturing", "planned_steps": 8,
        "dispatch_count": 1, "confirmed_steps": 0, "pending_dispatch": True,
        "cause": "invalid_response", "cause_code": None, "reported_http_status": 503,
    }
    writes = copy.deepcopy(connection.writes)
    assert writes and verifications == []
    assert cases.approve(case_id, ACTOR, plan_id) == stopped
    restarted = ProductCaseService(cases.root.parent, executor)
    assert restarted.get(case_id, ACTOR) == stopped and connection.writes == writes
    records = list((options["store"] / plan_id).glob("execution-failure-*.json"))
    assert len(records) == (0 if diagnostic_storage_fails else 1)
    if records:
        record = read_json(records[0])
        assert records[0].name == f"execution-failure-{digest(record)}.json"
        assert record["failure"] == failure
        assert record["policy_sha256"] == prepared["plan"]["policy_sha256"]
        assert record["snapshot_sha256"] == prepared["plan"]["snapshot_sha256"]
        assert "private-failure-canary" not in json.dumps(record)
    assert "private-failure-canary" not in json.dumps(public_evidence(stopped))
    saved = restarted._read(case_id)
    saved.execution["failure"]["plan_id"] = "f" * 64
    assert "failure" not in saved.view()["execution"]


def test_unknown_wire_failure_code_never_leaks_or_means_zero_writes(wired, monkeypatch):
    from forgeflow.product.progress import HUB

    cases, case_id, _executor, connection, _options, verifications, _ = wired
    prepared = cases.prepare(case_id, ACTOR)

    def fail(*_args, **_kwargs):
        raise client.ExecutorError("private_code_canary", reason="private-reason-canary")

    monkeypatch.setattr(client, "apply", fail)
    stopped = cases.approve(case_id, ACTOR, prepared["plan"]["plan_id"])
    assert stopped["reason"] == "apply_failed" and stopped["state"] == "execution_unknown"
    assert "failure" not in stopped["execution"]
    assert "canary" not in json.dumps(public_evidence(stopped))
    assert "canary" not in json.dumps(HUB.snapshot(case_id))
    assert verifications == [] and connection.writes == []


def test_case_without_an_executor_binding_says_it_needs_preparing(wired):
    """A missing binding is not an older version, and must not be reported as one.

    Such a case can appear when preparation never completed, or when a case file
    was written by something other than this executor. The reader needs the real
    reason and a way out, not "this plan is from an older version".
    """
    cases, case_id, _, _, _, _, _ = wired
    stored = cases._read(case_id)
    prepared = cases.prepare(case_id, ACTOR)
    assert prepared["plan"]["plan_id"]
    stored = cases._read(case_id)
    assert "executor_binding" in stored.plan, "the fixture must start bound"

    del stored.plan["executor_binding"]
    cases._write(stored)

    view = cases.get(case_id, ACTOR)
    assert view["decision"]["can_approve"] is False
    assert view["decision"]["reason"] == "product_binding_missing"
