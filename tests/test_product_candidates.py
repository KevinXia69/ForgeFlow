"""Product candidate preferences on invented facts, never benchmark evidence."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest
import test_product_versions as root_tests
from recovery_synthetic import Fake, Model
from test_planning_tool import toy_blueprint
from test_recovery_change_set import fixture

from forgeflow.executor.daemon import Handlers
from forgeflow.executor.handlers import RecoveryHandlers
from forgeflow.executor.protocol import ProtocolError
from forgeflow.planning.objectives import PRODUCT_OBJECTIVES, bound_objective, objective_kind
from forgeflow.planning.tool import SolverFailure
from forgeflow.product import reconciliation
from forgeflow.recovery import client, scope_guard, service
from forgeflow.recovery.change_set import ChangeSetError, build_change_set, validate_projection
from forgeflow.recovery.product_candidates import business_fingerprint
from forgeflow.recovery.product_constraints import (
    ConstraintError,
    constrained_blueprint,
    load_constraints,
)
from forgeflow.recovery.product_versions import ProductVersions, VersionError
from forgeflow.recovery.readback import ReadbackAdapter
from forgeflow.recovery.scope_guard import ScopeError
from forgeflow.recovery.storage import atomic_json, read_json

system = root_tests.system
REAL_ADAPTER_APPLY = ReadbackAdapter.apply
serve = root_tests.serve
workspace = root_tests.workspace
handlers = root_tests.handlers


@pytest.mark.parametrize("value", ["min_new_spend", "unknown", {}, [], True, 1])
def test_unreviewed_product_preference_is_rejected(value):
    with pytest.raises(ValueError, match="^product_objective_invalid$"):
        objective_kind(value)


def test_product_preference_cannot_enter_default_path_or_be_relabelled():
    request, snapshot, blueprint, plan = fixture()
    plan["product_objective"] = "min_planned_cost"
    plan["plan"]["objective_kind"] = "min_new_spend"
    with pytest.raises(ChangeSetError, match="objective binding"):
        build_change_set(request, snapshot, blueprint, plan)
    changes = build_change_set(request, snapshot, blueprint, plan,
                               product_objective="min_planned_cost")
    with pytest.raises(ChangeSetError, match="objective binding"):
        validate_projection(changes, request, snapshot, blueprint, plan)
    with pytest.raises(ChangeSetError, match="objective binding"):
        build_change_set(request, snapshot, blueprint, plan,
                         product_objective="preserve_quantities")


def two_route_facts():
    request, snapshot, _, _ = fixture()
    centers = snapshot["models"]["mrp.workcenter"]
    center = copy.deepcopy(centers[1])
    center.update(id=3, name="WC-C", code="WC-C")
    center["note"] = center["note"].replace('"unit_cost": 3', '"unit_cost": 1')
    centers.append(center)
    centers[0]["alternative_workcenter_ids"] = [2, 3]
    snapshot["collected_at_utc"] = "2026-01-01T10:00:00+00:00"
    return request, snapshot


def test_pinned_solver_has_two_real_objectives_on_one_invented_snapshot(system, tmp_path):
    path = tmp_path / "invented.json"
    path.write_text(json.dumps(two_route_facts()))
    script = """
import copy,json,sys
from pathlib import Path
root = Path(sys.argv[1]); sys.path.insert(0,str(root))
from forgeflow.planning.tool import solve_blueprint
from forgeflow.recovery.product_constraints import constrained_blueprint
from forgeflow.recovery.change_set import build_change_set, validate_projection
from forgeflow.recovery.product_costs import build_cost_report
request,snapshot=json.loads(Path(sys.argv[2]).read_text())
before=copy.deepcopy((request,snapshot))
blueprint=constrained_blueprint(snapshot,request,{})
outputs=[]
for objective in ('preserve_quantities','min_planned_cost'):
    plan=solve_blueprint(blueprint,root/'upstream',product_objective=objective)
    changes=build_change_set(request,snapshot,blueprint,plan,product_objective=objective)
    validate_projection(changes,request,snapshot,blueprint,plan,product_objective=objective)
    costs=build_cost_report(request,snapshot,blueprint,changes)
    outputs.append({'objective':objective,'actual_kind':plan['plan']['objective_kind'],
                    'trace':plan['trace'],'costs':costs,'changes':changes})
assert (request,snapshot)==before
assert outputs[0]['costs']['planned']['total']=='62.0'
assert outputs[1]['costs']['planned']['total']=='50.0'
assert outputs[0]['costs']['basis']==outputs[1]['costs']['basis']
assert outputs[0]['changes']['projected_state']!=outputs[1]['changes']['projected_state']
assert all(all(c['status']=='OPTIMAL' for c in row['trace']['solve_calls']) for row in outputs)
assert {row['actual_kind'] for row in outputs}=={'repair_plan','min_new_spend'}
assert all(all(m['workcenter_code']!='WC-A' for m in row['changes']['projected_state']['manufacturing_orders']) for row in outputs)
print(json.dumps({'status':'passed','scope':'actual pinned CP-SAT with invented facts; no ERP or model',
                  'costs':[row['costs']['planned']['total'] for row in outputs]}))
"""
    run = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                          str(system[2]["bundle"].resolve()), str(path)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["costs"] == ["62.0", "50.0"]


def pinned_plans(bundle, blueprint, tmp_path, objectives=PRODUCT_OBJECTIVES):
    path = tmp_path / "blueprint.json"
    path.write_text(json.dumps(blueprint))
    script = """
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); sys.path.insert(0,str(root))
from forgeflow.planning.tool import solve_blueprint
blueprint=json.loads(Path(sys.argv[2]).read_text())
print(json.dumps({objective:solve_blueprint(blueprint,root/'upstream',product_objective=objective)
    for objective in json.loads(sys.argv[3])}))
"""
    run = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(bundle.resolve()), str(path), json.dumps(objectives)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_cost_objective_still_excludes_canceled_low_price_supplier(system, tmp_path):
    outputs = pinned_plans(system[2]["bundle"], toy_blueprint(), tmp_path)
    for plan in outputs.values():
        orders = plan["plan"]["purchase_orders"]
        assert orders and all(order["vendor_ref"] == "TOY-NEW" for order in orders)
        assert plan["plan"]["expected_new_spend"] == 10


@pytest.fixture
def candidates(system, monkeypatch, tmp_path):
    connection, request, options, _ = system
    current_request, snapshot = two_route_facts()
    assert current_request == request
    connection.__dict__.update(Fake(snapshot).__dict__)
    monkeypatch.setattr(scope_guard, "collect_snapshot", lambda client, request: connection.collect(request))
    simulated_apply = ReadbackAdapter.apply

    def write(model, *, ids, vals):
        for identity in ids:
            model.database[model.model][identity].update(vals)
            if model.model == "purchase.order.line":
                line = model.database[model.model][identity]
                for move_id in line["move_ids"]:
                    # Native line-write side effects leave the other receipt
                    # dates stale. Exercise production PlanDocuments to sync them.
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
    monkeypatch.setattr(service, "collect_snapshot", lambda *args: copy.deepcopy(snapshot))
    plans = pinned_plans(options["bundle"], constrained_blueprint(snapshot, request), tmp_path)
    calls = []

    def solve(blueprint, upstream, *, product_objective):
        calls.append(product_objective)
        return copy.deepcopy(plans[product_objective])

    monkeypatch.setattr(service, "solve_blueprint", solve)
    return system, calls, plans


def prepare_candidates(system, **kwargs):
    connection, request, options, _ = system
    return service.prepare_product(connection, request, run_id=root_tests.RUN,
        owner_sha256=root_tests.OWNER, expected_revision=0, operation_id="compare-1",
        product_objectives=list(PRODUCT_OBJECTIVES), contract=root_tests._contract(), **options, **kwargs)


def comparison(system, decision):
    return read_json(system[2]["store"] / "product-comparisons" / (decision["comparison_id"] + ".json"))


def test_root_candidates_share_one_fact_read_and_baseline_then_replay_without_solving(candidates, monkeypatch):
    system, calls, _ = candidates
    connection, _, options, _ = system
    before = copy.deepcopy(connection.db)
    counts = {"collect_snapshot": 0, "capture_execution_baseline": 0}
    for name in counts:
        original = getattr(service, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(service, name, counted)
    decision = prepare_candidates(system)
    assert decision["state"] == "prepared" and decision["selected_plan_id"] is None
    assert len(decision["plans"]) == 2 and calls == list(PRODUCT_OBJECTIVES)
    assert counts == {"collect_snapshot": 1, "capture_execution_baseline": 1}
    manifests, costs = [], []
    for plan_id in decision["plans"]:
        directory = options["store"] / plan_id
        manifest = read_json(directory / "manifest.json")
        inputs = [read_json(directory / (name + ".json")) for name in
                  ("request", "snapshot", "blueprint", "plan")]
        objective = bound_objective(manifest, inputs[-1])
        constraints = load_constraints(directory, manifest, inputs[0])
        validate_projection(read_json(directory / "change-set.json"), *inputs,
                            product_objective=objective, product_constraints=constraints)
        manifests.append(manifest)
        costs.append(read_json(directory / "product-costs.json"))
    assert all(manifests[0]["artifacts"][name] == manifests[1]["artifacts"][name]
               for name in ("request", "snapshot", "blueprint", "baseline"))
    assert costs[0]["basis"] == costs[1]["basis"]
    assert [row["planned"]["total"] for row in costs] == ["62.0", "50.0"]
    assert comparison(system, decision)["state"] == "prepared"
    assert prepare_candidates(system, connection_factory=lambda: pytest.fail("replay opened ERP")) == decision
    assert len(calls) == 2 and connection.db == before and not connection.writes


def select(system, decision, index=1):
    return ProductVersions(system[2]["store"]).decide(root_tests.RUN, root_tests.OWNER,
        expected_revision=decision["revision"], operation_id="choose-1", action="select",
        plan_id=list(decision["plans"])[index])


def test_only_selected_candidate_can_borrow_no_other_approval_and_executes_without_resolve(candidates, monkeypatch):
    system, calls, _ = candidates
    connection, _, options, _ = system
    decision = prepare_candidates(system)
    first = next(iter(decision["plans"]))
    with pytest.raises(VersionError, match="product_plan_not_selected"):
        service.authorize_product(run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
            expected_revision=decision["revision"], plan_id=first, intent=root_tests.intent(), store=options["store"])
    chosen = select(system, decision)
    authorized = root_tests.authorize(options, chosen)
    grant = root_tests.grant_from(authorized["approval"])
    with pytest.raises(VersionError, match="product_plan_not_selected"):
        service.apply(connection, first, grant=grant, **options)
    assert not connection.writes
    adapter_errors = []
    original_apply = ReadbackAdapter.apply

    def checked_apply(adapter, step):
        try:
            return original_apply(adapter, step)
        except Exception as error:
            adapter_errors.append((step.kind, type(error).__name__, str(error)))
            raise

    monkeypatch.setattr(ReadbackAdapter, "apply", checked_apply)
    try:
        result = service.apply(connection, chosen["selected_plan_id"], grant=grant, **options)
    except Exception:
        assert not adapter_errors, adapter_errors
        raise
    assert result["execution_validated"] is True
    writes = list(connection.writes)
    assert writes and len(calls) == 2
    assert service.apply(connection, chosen["selected_plan_id"], grant=grant, **options) == result
    assert writes == connection.writes and len(calls) == 2
    verification = {"status": "passed", "strict_success": True, "evaluation_valid": True,
                    "evaluation_scope": "product_original_verifier"}
    receipts = []

    def verifier():
        receipts.append(True)
        return verification

    recovered = reconciliation.reconcile_original(connection, root_tests.RUN, chosen["selected_plan_id"],
        approvals=options["store"].parent / "approvals", verifier=verifier, **options)
    assert recovered["verifier"] == verification
    assert reconciliation.reconcile_original(connection, root_tests.RUN, chosen["selected_plan_id"],
        approvals=options["store"].parent / "approvals", verifier=verifier, **options) == recovered
    assert len(receipts) == 1 and writes == connection.writes and len(calls) == 2


def test_same_business_effects_from_two_objectives_make_only_one_candidate(candidates):
    system, _, plans = candidates
    plans["min_planned_cost"] = copy.deepcopy(plans["preserve_quantities"])
    plans["min_planned_cost"]["product_objective"] = "min_planned_cost"
    plans["min_planned_cost"]["plan"]["objective_kind"] = "min_new_spend"
    decision = prepare_candidates(system)
    assert len(decision["plans"]) == 1 and decision["state"] == "selected"
    attempts = comparison(system, decision)["attempts"]
    assert [row["state"] for row in attempts] == ["prepared", "duplicate"]
    assert attempts[1]["equivalent_plan_id"] == decision["selected_plan_id"]
    assert not system[0].writes


def test_business_effect_dedupe_ignores_solver_and_fresh_id_numbering_but_not_real_changes():
    changes = build_change_set(*fixture())
    renamed = copy.deepcopy(changes)
    # A new MO's incidental token is used both as target and as the PO's origin.
    renamed = json.loads(json.dumps(renamed).replace("new:mrp.production:0001", "new:mrp.production:0099"))
    renamed["operations"].reverse()
    for op in renamed["operations"]:
        op["plan_allocations"] = []
        op["operation_id"] = "irrelevant"
        if op["after"]:
            op["after"]["origin_refs"].reverse()
    assert business_fingerprint(renamed) == business_fingerprint(changes)
    changed = next(op for op in renamed["operations"] if op["after"] and op["target"]["id"] is None)
    changed["after"]["quantity"] += 1
    assert business_fingerprint(renamed) != business_fingerprint(changes)


@pytest.mark.parametrize("all_fail", [False, True])
def test_observed_failed_attempt_is_retained_without_inventing_alternative_or_global_infeasibility(
        candidates, monkeypatch, all_fail):
    system, _, _ = candidates
    original = service.solve_blueprint
    trace = {"solve_calls": [{"status": "INFEASIBLE", "phase": "toy primary"}]}

    def solve(*args, product_objective):
        if all_fail or product_objective == "min_planned_cost":
            raise SolverFailure("planning_model_infeasible", trace)
        return original(*args, product_objective=product_objective)

    monkeypatch.setattr(service, "solve_blueprint", solve)
    if all_fail:
        with pytest.raises(service.RecoveryError, match="^product_candidates_unavailable$"):
            prepare_candidates(system)
        decision = ProductVersions(system[2]["store"]).status(root_tests.RUN, root_tests.OWNER)
        assert decision["state"] == "prepare_failed" and not decision["plans"]
        assert decision["preparation"]["failure_reason"] == "product_candidates_unavailable"
    else:
        decision = prepare_candidates(system)
        assert len(decision["plans"]) == 1
    attempts = comparison(system, decision)["attempts"]
    assert attempts[-1]["state"] == "infeasible" and attempts[-1]["trace"] == trace
    assert not system[0].writes


def test_late_fact_drift_invalidates_entire_comparison(candidates, monkeypatch):
    system, calls, _ = candidates
    original = ReadbackAdapter.preflight
    count = 0

    def preflight(self, *args):
        nonlocal count
        count += 1
        if count == 3:
            system[0].db["sale.order"][1]["commitment_date"] = "2026-02-01 00:00:00"
        return original(self, *args)

    monkeypatch.setattr(ReadbackAdapter, "preflight", preflight)
    with pytest.raises(ScopeError, match="^immutable_scope_drift:sale.order$"):
        prepare_candidates(system)
    decision = ProductVersions(system[2]["store"]).status(root_tests.RUN, root_tests.OWNER)
    assert decision["state"] == "prepare_failed" and not decision["plans"]
    assert comparison(system, decision)["state"] == "failed"
    with pytest.raises(VersionError, match="product_preparation_interrupted"):
        prepare_candidates(system)
    assert len(calls) == 2 and not system[0].writes


@pytest.mark.parametrize("value", [[], True, "cost", {}, ["min_planned_cost"],
    ["min_planned_cost", "preserve_quantities"], ["preserve_quantities", "preserve_quantities"]])
def test_wire_rejects_unreviewed_objectives_before_opening_erp(system, value):
    _, request, options, _ = system
    policy = options["store"].parent / "policy.json"
    atomic_json(policy, root_tests._contract().payload())
    root = RecoveryHandlers(connection_factory=lambda: pytest.fail("invalid request opened ERP"),
        policy_file=policy, product_verifier=lambda: {}, **options)
    with pytest.raises(ProtocolError) as error:
        root.product(root_tests.RUN, "prepare", {"owner_sha256": root_tests.OWNER,
            "expected_revision": 0, "operation_id": "bad-compare", "request": request,
            "constraints": {}, "product_objectives": value})
    assert error.value.code == "invalid_field" and error.value.reason == "product_objectives_invalid"
    assert not options["store"].exists()


def test_unix_socket_prepares_selects_and_closes_one_candidate_without_dispatch(candidates, serve):
    system, calls, _ = candidates
    connection, request, options, _ = system
    policy = options["store"].parent / "policy.json"
    atomic_json(policy, root_tests._contract().payload())
    root = RecoveryHandlers(connection_factory=lambda: connection, policy_file=policy,
        product_verifier=lambda: pytest.fail("cancellation ran verifier"), **options)
    server = serve(custom=Handlers(root.prepare, root.apply, root.status, product=root.product))
    address = str(server.socket_path)
    decision = client.product(address, root_tests.RUN, "prepare", {"owner_sha256": root_tests.OWNER,
        "expected_revision": 0, "operation_id": "socket-compare", "request": request,
        "constraints": {}, "product_objectives": list(PRODUCT_OBJECTIVES)})["decision"]
    assert decision["selected_plan_id"] is None and len(decision["plans"]) == 2
    plan = list(decision["plans"])[1]
    chosen = client.product(address, root_tests.RUN, "decide", {"owner_sha256": root_tests.OWNER,
        "expected_revision": decision["revision"], "operation_id": "socket-select",
        "action": "select", "plan_id": plan})["decision"]
    authorized = client.product(address, root_tests.RUN, "authorize", {"owner_sha256": root_tests.OWNER,
        "expected_revision": chosen["revision"], "plan_id": plan, "intent": root_tests.intent()})
    payload = {"owner_sha256": root_tests.OWNER, "plan_id": plan,
               "approval": {k: v for k, v in authorized["approval"].items() if k != "schema_version"}}
    receipt = client.product(address, root_tests.RUN, "cancel_unstarted", payload)
    assert receipt["receipt"]["outcome"] == "not_started"
    assert client.product(address, root_tests.RUN, "cancel_unstarted", payload) == receipt
    assert not connection.writes and len(calls) == 2


def test_candidate_preferences_cannot_be_dropped_relabelled_or_changed_on_replay(candidates):
    system, calls, _ = candidates
    connection, request, options, _ = system
    decision = prepare_candidates(system)
    directory = options["store"] / list(decision["plans"])[1]
    manifest = read_json(directory / "manifest.json")
    plan = read_json(directory / "plan.json")
    assert bound_objective(manifest, plan) == "min_planned_cost"
    tampered = copy.deepcopy(manifest)
    tampered.pop("product_objectives")
    with pytest.raises(ValueError, match="product_objective_binding_changed"):
        bound_objective(tampered, plan)
    tampered.pop("product_objective")
    with pytest.raises(ConstraintError, match="product_constraints_binding_changed"):
        load_constraints(directory, tampered, request)
    tampered = {**manifest, "product_objective": "preserve_quantities"}
    with pytest.raises(ValueError, match="product_objective_binding_changed"):
        bound_objective(tampered, plan)
    with pytest.raises(VersionError, match="product_operation_conflict"):
        service.prepare_product(connection, request, run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
            expected_revision=0, operation_id="compare-1", contract=root_tests._contract(), **options)
    assert len(calls) == 2 and not connection.writes


def test_failed_independent_validation_is_retained_and_excluded(candidates):
    system, _, plans = candidates
    plans["min_planned_cost"]["blueprint_sha256"] = "a" * 64
    decision = prepare_candidates(system)
    assert len(decision["plans"]) == 1
    attempt = comparison(system, decision)["attempts"][1]
    assert attempt["state"] == "rejected" and attempt["reason"] == "change_set_binding_mismatch"
    assert attempt["plan"] == plans["min_planned_cost"]
    assert not system[0].writes


def test_unexpected_solver_error_does_not_publish_partial_set_or_echo_exception(candidates, monkeypatch):
    system, _, _ = candidates
    original = service.solve_blueprint

    def solve(*args, product_objective):
        if product_objective == "min_planned_cost":
            raise RuntimeError("private-canary")
        return original(*args, product_objective=product_objective)

    monkeypatch.setattr(service, "solve_blueprint", solve)
    with pytest.raises(RuntimeError, match="private-canary"):
        prepare_candidates(system)
    decision = ProductVersions(system[2]["store"]).status(root_tests.RUN, root_tests.OWNER)
    assert decision["state"] == "prepare_failed" and not decision["plans"]
    audit = comparison(system, decision)
    assert audit["state"] == "failed" and audit["attempts"][0]["state"] == "prepared"
    assert "private-canary" not in json.dumps(audit) + json.dumps(decision)
    assert not system[0].writes
