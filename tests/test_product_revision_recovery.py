"""Durable Local/API revision, real socket and synthetic ERP; no A/B claim."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_product_cases as api_tests
import test_product_local_versions as local_tests
import test_product_versions as root_tests
from recovery_synthetic import Model
from test_product_constraints import bind_synthetic_solver
from test_product_prepare_recovery import record_preparations

from forgeflow.api.case_qa import answer_case
from forgeflow.api.evidence import public_evidence
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.recovery import client, service
from forgeflow.recovery.product_versions import ProductVersions, VersionError, owner_scope
from forgeflow.recovery.readback import ReadbackAdapter
from forgeflow.recovery.storage import atomic_json, read_json

system = root_tests.system
serve = root_tests.serve
workspace = root_tests.workspace
handlers = root_tests.handlers
wired = local_tests.wired
ACTOR = local_tests.ACTOR
CHANGES = {"minimum_margin_pct": 90.0}
REAL_ADAPTER_APPLY = ReadbackAdapter.apply


@pytest.fixture
def revision_system(wired, monkeypatch):
    bind_synthetic_solver(monkeypatch)
    wired[2].supports_revisions = True  # explicit acceptance injection only
    prepared = wired[0].prepare(wired[1], ACTOR)
    assert prepared["state"] == "awaiting_approval"
    return wired, prepared


def revise(cases, prepared, changes=None):
    return cases.request_revision(prepared["case_id"], ACTOR, prepared["plan"]["plan_id"],
                                  reason="Tighten planned margin", changes=CHANGES if changes is None else changes)


def test_default_local_capability_stays_closed(wired):
    cases, case_id, executor, connection, _, _, _ = wired
    prepared = cases.prepare(case_id, ACTOR)
    assert executor.supports_revisions is False
    assert prepared["decision"]["can_request_changes"] is False
    with pytest.raises(ProductCaseError, match="revision_unsupported"):
        revise(cases, prepared)
    assert cases.get(case_id, ACTOR) == prepared and connection.writes == []


def test_revision_switches_after_root_retirement_and_executes_only_new_plan(revision_system, monkeypatch):
    (cases, case_id, executor, connection, options, verifications, _), first = revision_system
    counts = record_preparations(monkeypatch)
    revised = revise(cases, first)
    assert revised["state"] == "awaiting_approval", revised
    assert revised["revision"]["version"] == 2 and revised["revision"]["changes"] == CHANGES
    old, new = first["plan"]["plan_id"], revised["plan"]["plan_id"]
    assert old != new and revised["history"][0]["plan"]["plan_id"] == old
    root = ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert root["plans"][old]["state"] == "retired" and root["selected_plan_id"] == new
    assert revised["history"][0]["retired_at"] == root["plans"][old]["retired_at"]
    checks = {row["code"]: row for row in revised["plan"]["checks"]}
    assert checks["margin"]["threshold"] == "90%" and checks["margin"]["status"] == "pass"
    assert first["revision_limits"] == {"original_margin_pct": 20.0, "effective_margin_pct": 20.0,
                                        "max_exclusive_pct": 100, "decimal_places": 2}
    assert revised["revision_limits"] == {**first["revision_limits"], "effective_margin_pct": 90.0}
    assert "original floor 20%" in checks["margin"]["source"] and revised["plan"]["margin"] == 93
    assert revise(cases, first) == revised and counts == {"solve": 1, "snapshot": 1}
    assert connection.writes == [] and not list(executor.approvals.glob("approval-*.json"))
    with pytest.raises(VersionError, match="product_plan_retired"):
        service.apply(connection, old, **options)
    approved = cases.approve(case_id, ACTOR, new)
    assert approved["state"] == "succeeded", approved
    writes = connection.writes[:]
    assert cases.approve(case_id, ACTOR, new) == approved
    assert connection.writes == writes and len(verifications) == 1
    assert counts["solve"] == 1


@pytest.mark.parametrize("lost_at", ["before_transport", "after_root_reply", "bad_response"])
def test_lost_revision_reply_keeps_old_card_readonly_until_same_operation_recovered(revision_system, monkeypatch, lost_at):
    (cases, case_id, executor, connection, options, _, root), first = revision_system
    counts = record_preparations(monkeypatch)
    product = client.product
    response = None

    def lost(*args, **kwargs):
        nonlocal response
        if args[2] == "status":
            if lost_at == "before_transport":
                return product(*args, **kwargs)
            raise client.ExecutorError("unavailable")
        if lost_at != "before_transport":
            response = product(*args, **kwargs)
        if lost_at == "bad_response":
            return {"decision": {}}
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "product", lost)
    pending = revise(cases, first)
    assert pending["state"] == "revision_requested" and pending["decision"]["can_resume_revision"]
    assert pending["plan"] == first["plan"] and pending["revision"] == first["revision"]
    assert pending["history"] == [] and pending["revision_request"]["version"] == 2
    assert cases.get(case_id, ACTOR) == pending and cases.list_for(ACTOR) == [pending]
    for action in (lambda: cases.approve(case_id, ACTOR, first["plan"]["plan_id"]),
                   lambda: cases.reject(case_id, ACTOR, reason="cancel")):
        with pytest.raises(ProductCaseError, match="revision_confirmation_pending"):
            action()
    with pytest.raises(ProductCaseError, match="revision_unsupported"):
        revise(cases, first, {"minimum_margin_pct": 91})
    intent = copy.deepcopy(cases._read(case_id).preparation["intent"])
    assert counts["solve"] == (0 if lost_at == "before_transport" else 1)
    monkeypatch.setattr(client, "product", product)
    if response is not None:
        monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("completed revision must not open ERP"))
    resumed = ProductCaseService(cases.root.parent, executor)
    recovered = resumed.prepare(case_id, ACTOR)
    assert recovered["state"] == "awaiting_approval" and recovered["revision"]["version"] == 2, recovered
    assert resumed._read(case_id).preparation["intent"] == intent
    assert len(recovered["history"]) == 1 and counts == {"solve": 1, "snapshot": 1}
    assert sum(row["action"] == "revision_requested" for row in recovered["audit"]) == 1
    assert sum(row["action"] == "revision_retired" for row in recovered["audit"]) == 1
    assert resumed.prepare(case_id, ACTOR) == recovered
    public = public_evidence(recovered)
    assert intent["inputs_sha256"] not in json.dumps(public)
    assert "_plan" not in public and "preparation" not in public
    assert connection.writes == []


def test_revision_limits_are_readonly_and_remain_available_while_capability_is_closed(wired, monkeypatch):
    cases, case_id, executor, _, _, _, _ = wired
    monkeypatch.setattr(client, "product", lambda *args, **kwargs: pytest.fail("GET cannot contact root"))
    view = cases.get(case_id, ACTOR)
    assert view["revision_limits"]["original_margin_pct"] == 20.0
    assert view["decision"]["can_request_changes"] is False
    executor.request["margin"] = 0.275
    assert cases.get(case_id, ACTOR)["revision_limits"]["effective_margin_pct"] == 27.5
    executor.request["margin"] = float("nan")
    assert cases.get(case_id, ACTOR)["revision_limits"] is None


def test_changed_local_request_cannot_relabel_a_prepared_plan_as_a_new_original_floor(revision_system, monkeypatch):
    (cases, case_id, executor, _, _, _, _), first = revision_system
    executor.request["margin"] = 0.275
    monkeypatch.setattr(client, "product", lambda *args, **kwargs: pytest.fail("GET cannot contact root"))
    observed = cases.get(case_id, ACTOR)
    assert observed["revision_limits"] is None and observed["plan"] == first["plan"]


@pytest.mark.parametrize("crash_at", ["intent_saved", "before_commit", "after_commit"])
def test_revision_intent_and_commit_survive_api_restart(revision_system, monkeypatch, crash_at):
    (cases, case_id, executor, connection, _, _, root), first = revision_system
    counts = record_preparations(monkeypatch)
    write = cases._write

    def crash(case):
        matches = (case.state == "revision_requested" if crash_at == "intent_saved"
                   else case.state == "awaiting_approval" and case.revision["version"] == 2)
        if not matches:
            return write(case)
        if crash_at != "before_commit":
            write(case)
        raise KeyboardInterrupt("controlled API process interruption")

    monkeypatch.setattr(cases, "_write", crash)
    with pytest.raises(KeyboardInterrupt):
        revise(cases, first)
    if crash_at != "intent_saved":
        monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("do not solve a completed revision"))
    resumed = ProductCaseService(cases.root.parent, executor)
    recovered = resumed.prepare(case_id, ACTOR)
    assert recovered["state"] == "awaiting_approval" and recovered["revision"]["version"] == 2
    assert len(recovered["history"]) == 1 and counts == {"solve": 1, "snapshot": 1}
    assert connection.writes == []


def test_failed_tightening_is_recovered_with_original_reason_and_old_plan_retired(revision_system, monkeypatch):
    (cases, case_id, executor, connection, options, _, _), first = revision_system
    counts = record_preparations(monkeypatch)
    product = client.product

    def lost_status(*args, **kwargs):
        if args[2] == "status":
            raise client.ExecutorError("unavailable")
        return product(*args, **kwargs)

    monkeypatch.setattr(client, "product", lost_status)
    pending = revise(cases, first, {"minimum_margin_pct": 95})
    assert pending["state"] == "revision_requested" and pending["history"] == []
    monkeypatch.setattr(client, "product", product)
    recovered = ProductCaseService(cases.root.parent, executor).prepare(case_id, ACTOR)
    assert recovered["state"] == "failed" and recovered["reason"] == "product_constraint_not_satisfied", recovered
    assert recovered["plan"] is None and recovered["revision"]["version"] == 2
    assert len(recovered["history"]) == 1 and recovered["history"][0]["plan"] == first["plan"]
    root = ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert root["preparation"]["failure_reason"] == recovered["reason"]
    assert root["plans"][first["plan"]["plan_id"]]["state"] == "retired"
    assert counts == {"solve": 1, "snapshot": 1} and connection.writes == []


def test_root_reservation_interruption_proves_retirement_without_running_solver(revision_system, monkeypatch):
    (cases, _, _, connection, _, _, _), first = revision_system
    save = ProductVersions._save
    counts = record_preparations(monkeypatch)

    def interrupted(registry, record):
        save(registry, record)
        if record["state"] == "preparing" and record["version"] == 2:
            raise RuntimeError("controlled interruption after reservation persistence")

    monkeypatch.setattr(ProductVersions, "_save", interrupted)
    failed = revise(cases, first)
    assert failed["state"] == "failed" and failed["reason"] == "product_preparation_interrupted", failed
    assert failed["plan"] is None and failed["history"][0]["plan"] == first["plan"]
    assert counts == {"solve": 0, "snapshot": 0} and connection.writes == []


def test_root_refusal_before_reservation_does_not_claim_retirement(revision_system, monkeypatch):
    (cases, _, _, connection, _, _, _), first = revision_system
    original = service.prepare_product

    def refuse(*args, **kwargs):
        raise service.RecoveryError("task_contract_mismatch")

    monkeypatch.setattr(service, "prepare_product", refuse)
    result = revise(cases, first)
    assert result["state"] == "awaiting_approval" and result["reason"] == "revision_not_started"
    assert result["plan"] == first["plan"] and result["history"] == [] and result["revision"] == first["revision"]
    assert connection.writes == []
    monkeypatch.setattr(service, "prepare_product", original)
    retried = revise(cases, first)
    assert retried["state"] == "awaiting_approval" and retried["revision"]["version"] == 2
    assert len(retried["history"]) == 1 and connection.writes == []


@pytest.mark.parametrize("tamper", ["request", "intent"])
def test_pending_revision_cannot_replace_original_intent_or_request(revision_system, monkeypatch, tamper):
    (cases, case_id, executor, connection, _, _, _), first = revision_system

    def lost(*args, **kwargs):
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "product", lost)
    pending = revise(cases, first)
    assert pending["state"] == "revision_requested"
    if tamper == "request":
        executor.request["margin"] = 0.25
    else:
        case = cases._read(case_id)
        case.preparation["intent"]["request_sha256"] = "a" * 64
        cases._write(case)
    monkeypatch.setattr(client, "product", lambda *args, **kwargs: pytest.fail("changed intent cannot reach root"))
    result = cases.prepare(case_id, ACTOR)
    assert result["state"] == "revision_requested"
    assert result["revision_request"]["error"] == "product_binding_changed"
    assert result["plan"] == first["plan"] and result["history"] == [] and connection.writes == []


def test_authorization_winning_at_root_is_not_misrepresented_as_revision(revision_system):
    (cases, case_id, _, connection, options, _, _), first = revision_system
    intent = root_tests.intent()
    intent["approver"] = ACTOR
    service.authorize_product(run_id=f"local-{case_id}", owner_sha256=owner_scope(ACTOR),
        expected_revision=2, plan_id=first["plan"]["plan_id"], intent=intent, store=options["store"])
    result = revise(cases, first)
    assert result["state"] == "failed" and result["reason"] == "product_revision_conflict"
    assert result["plan"] == first["plan"] and result["history"] == []
    assert connection.writes == []


def test_two_api_instances_recover_one_revision(revision_system, monkeypatch):
    (cases, _, executor, connection, _, _, _), first = revision_system
    counts = record_preparations(monkeypatch)
    instances = [ProductCaseService(cases.root.parent, executor) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda instance: revise(instance, first), instances))
    assert results[0] == results[1] and results[0]["revision"]["version"] == 2
    assert counts == {"solve": 1, "snapshot": 1} and connection.writes == []


def test_http_revision_pending_resume_owner_and_review_gates(revision_system, monkeypatch, tmp_path):
    (__, _, executor, connection, _, _, _), _ = revision_system
    product = client.product
    with api_tests.client_for(tmp_path / "http", executor) as (http, _):
        actor = api_tests.sign_in(http)
        cases = http.app.state.product_cases
        created = cases.create(actor, "B")
        record = cases._read(created["case_id"])
        record.order["units"] = 10
        cases._write(record)
        first = cases.prepare(created["case_id"], actor)
        url = f"/api/product/cases/{created['case_id']}"

        def lost(*args, **kwargs):
            if args[2] == "prepare":
                product(*args, **kwargs)
            raise client.ExecutorError("unavailable")

        monkeypatch.setattr(client, "product", lost)
        pending = http.post(url + "/revision", json={"plan_id": first["plan"]["plan_id"],
                          "reason": "Tighter floor", "changes": CHANGES})
        assert pending.status_code == 200 and pending.json()["decision"]["can_resume_revision"], pending.text
        assert "inputs_sha256" not in pending.text and "executor_binding" not in pending.text
        assert http.get(url).json()["history"] == []
        api_tests.sign_in(http, api_tests.INVITE_B)
        assert http.post(url + "/prepare").status_code == 404
        api_tests.sign_in(http)
    with api_tests.client_for(tmp_path / "http", executor, deployment="review") as (http, _):
        api_tests.sign_in(http)
        assert http.get(url).json()["decision"]["can_resume_revision"] is False
        assert http.post(url + "/prepare").status_code == 503
    monkeypatch.setattr(client, "product", product)
    with api_tests.client_for(tmp_path / "http", executor) as (http, _):
        api_tests.sign_in(http)
        recovered = http.post(url + "/prepare").json()
        assert recovered["revision"]["version"] == 2 and recovered["state"] == "awaiting_approval", recovered
    assert connection.writes == []


def test_root_failure_trace_is_durable_private_data(revision_system, monkeypatch):
    from forgeflow.planning.tool import SolverFailure

    (cases, case_id, _, connection, options, _, _), first = revision_system
    trace = {"solve_calls": [{"status": "INFEASIBLE", "phase": "primary"}], "private-canary": True}

    def fail(*args):
        raise SolverFailure("planning_model_infeasible", trace)

    monkeypatch.setattr(service, "solve_blueprint", fail)
    result = revise(cases, first)
    assert result["state"] == "failed" and result["reason"] == "planning_model_infeasible", result
    record = read_json(options["store"] / "product-decisions" / f"local-{case_id}.json")
    assert record["last_operation"]["solver_trace"] == trace
    view = ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert "private-canary" not in json.dumps(view) and "private-canary" not in json.dumps(result)
    assert connection.writes == []


@pytest.mark.parametrize("failed_first", [False, True])
@pytest.mark.parametrize("change", ["policy", "request"])
def test_root_pins_original_task_identity_before_retirement_or_erp(system, monkeypatch, failed_first, change):
    from forgeflow.recovery.product_constraints import ConstraintError

    connection, request, options, _ = system
    bind_synthetic_solver(monkeypatch)
    params = dict(run_id=root_tests.RUN, owner_sha256=root_tests.OWNER, operation_id="first",
                  expected_revision=0, contract=root_tests._contract(), **options)
    if failed_first:
        with pytest.raises(ConstraintError, match="product_constraint_not_satisfied"):
            service.prepare_product(connection, request, constraints={"minimum_margin_pct": 95}, **params)
    else:
        service.prepare_product(connection, request, constraints={"minimum_margin_pct": 90}, **params)
    registry = ProductVersions(options["store"])
    before = registry.status(root_tests.RUN, root_tests.OWNER)
    changed = copy.deepcopy(request)
    if change == "request":
        changed["margin"] = 0.21
        policy = root_tests._contract(margin=0.21)
    else:
        policy = root_tests._contract(contract_id="different-reviewed-policy")
    with pytest.raises(VersionError, match="product_binding_changed"):
        service.prepare_product(None, changed, run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
            expected_revision=before["revision"], operation_id="changed-identity",
            constraints={"minimum_margin_pct": 96}, contract=policy,
            connection_factory=lambda: pytest.fail("changed task identity cannot open ERP"), **options)
    assert registry.status(root_tests.RUN, root_tests.OWNER) == before and connection.writes == []


def test_legacy_registry_migrates_identity_only_from_its_existing_plan_facts(system, monkeypatch):
    connection, request, options, _ = system
    bind_synthetic_solver(monkeypatch)
    initial = root_tests.prepare(system)
    path = options["store"] / "product-decisions" / f"{root_tests.RUN}.json"
    record = read_json(path)
    record["schema_version"] = 2
    record.pop("task_identity")
    atomic_json(path, record, replace=True)
    changed_policy = root_tests._contract(contract_id="cannot-replace-legacy-policy")
    with pytest.raises(VersionError, match="product_binding_changed"):
        service.prepare_product(None, request, run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
            expected_revision=initial["revision"], operation_id="legacy-revision",
            constraints=CHANGES, contract=changed_policy,
            connection_factory=lambda: pytest.fail("reject before ERP"), **options)
    result = service.prepare_product(connection, request, run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
        expected_revision=initial["revision"], operation_id="legacy-revision",
        constraints=CHANGES, contract=root_tests._contract(), **options)
    migrated = read_json(path)
    assert result["version"] == 2 and migrated["schema_version"] == 3
    assert migrated["task_identity"]["policy_sha256"] == root_tests._contract().digest()
    assert connection.writes == []


@pytest.mark.parametrize("question", ["方案怎么样？", "执行了吗？", "What is the plan?", "Was it executed?"])
def test_pending_revision_answers_do_not_present_old_plan_as_new(question):
    case = {"state": "revision_requested", "reason": "revision_confirmation_pending",
            "decision": {"revision_pending": True}, "revision": {"version": 1},
            "revision_request": {"version": 2}, "plan": {"margin": 93}}
    result = answer_case(question, case)
    assert "修改前" in result["answer"] or "earlier version" in result["answer"]
    assert {row["field"] for row in result["citations"]} >= {"revision.version", "revision_request.version"}
    assert "93" not in result["answer"]


def test_actual_pinned_solver_prepares_revises_and_applies_invented_erp_through_local_socket(wired, monkeypatch):
    cases, case_id, executor, connection, options, verifications, _ = wired
    executor.supports_revisions = True
    calls = []
    simulated_apply = ReadbackAdapter.apply

    def write(model, *, ids, vals):
        for identity in ids:
            model.database[model.model][identity].update(vals)
            if model.model == "purchase.order.line":
                line = model.database[model.model][identity]
                for move_id in line["move_ids"]:
                    # The observed native ERP side effect omits move.date and
                    # receipt.schedule; production PlanDocuments must sync them.
                    model.database["stock.move"][move_id].update(
                        product_uom_qty=line["product_qty"], date_deadline=line["date_planned"])
        return True

    def apply(adapter, step):
        if step.kind == "update_purchase" and step.payload["arguments"]["mode"] == "line":
            connection.writes.append(step.key)
            return REAL_ADAPTER_APPLY(adapter, step)
        return simulated_apply(adapter, step)

    monkeypatch.setattr(Model, "write", write, raising=False)
    monkeypatch.setattr(ReadbackAdapter, "apply", apply)
    code = """
import json, sys
from pathlib import Path
bundle = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(bundle))
from forgeflow.planning.tool import solve_blueprint
result = solve_blueprint(json.load(sys.stdin), bundle / 'upstream')
import procurement.solver
assert Path(procurement.solver.__file__).resolve().is_relative_to(bundle / 'upstream')
print(json.dumps(result))
"""

    def solve(blueprint, upstream):
        calls.append(blueprint["margin"])
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(options["bundle"])],
                                   input=json.dumps(blueprint), capture_output=True, text=True, timeout=45)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    monkeypatch.setattr(service, "solve_blueprint", solve)
    first = cases.prepare(case_id, ACTOR)
    assert first["state"] == "awaiting_approval", first
    second = revise(cases, first)
    assert second["state"] == "awaiting_approval" and second["plan"]["margin"] == 93, second
    assert calls == [0.2, 0.9] and connection.writes == []
    assert read_json(options["store"] / second["plan"]["plan_id"] / "request.json")["margin"] == 0.2
    executed = cases.approve(case_id, ACTOR, second["plan"]["plan_id"])
    if executed["state"] != "succeeded":
        diagnostic = options["store"].parent / "executor-errors.jsonl"
        journal = options["store"] / second["plan"]["plan_id"] / "journal.jsonl"
        pytest.fail(f"Invented ERP execution: {executed.get('reason')}; "
                    + (diagnostic.read_text() if diagnostic.exists() else "no root diagnostic")
                    + "\n" + "\n".join(journal.read_text().splitlines()[-4:]))
    assert len(verifications) == 1 and calls == [0.2, 0.9]
