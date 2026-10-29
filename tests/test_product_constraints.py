"""Product-only tightening, invented business records; not ERP acceptance."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_product_versions as root_tests
from test_recovery_change_set import fixture, stock_margin_fixture

from forgeflow.executor.handlers import RecoveryHandlers
from forgeflow.executor.protocol import ProtocolError
from forgeflow.planning import tool
from forgeflow.planning.tool import SolverFailure
from forgeflow.product import reconciliation, unstarted
from forgeflow.recovery import scope_guard, service
from forgeflow.recovery.change_set import (
    ChangeSetError,
    build_change_set,
    canonical_hash,
    validate_projection,
)
from forgeflow.recovery.product_constraints import (
    ConstraintError,
    canonical_constraints,
    check_product_margin,
    constrained_blueprint,
    load_constraints,
)
from forgeflow.recovery.product_versions import ProductVersions, VersionError
from forgeflow.recovery.storage import atomic_json, digest, read_json

system = root_tests.system


@pytest.mark.parametrize("value", [False, [], "30%", {"unknown": 30},
                                  {"minimum_margin_pct": True}, {"minimum_margin_pct": "30"},
                                  {"minimum_margin_pct": float("nan")},
                                  {"minimum_margin_pct": float("inf")},
                                  {"minimum_margin_pct": -1}, {"minimum_margin_pct": 100},
                                  {"minimum_margin_pct": 30.001}])
def test_invalid_constraints_have_fixed_reason(value):
    with pytest.raises(ConstraintError, match="^product_constraints_invalid$"):
        canonical_constraints(value, {"margin": 0.2})


@pytest.mark.parametrize("floor", [0.3, 19.99, 20])
def test_percent_points_cannot_silently_relax_original_fraction(floor):
    with pytest.raises(ConstraintError, match="^product_constraints_must_tighten$"):
        canonical_constraints({"minimum_margin_pct": floor}, {"margin": 0.2})


def test_canonical_units_and_decimal_boundary():
    assert canonical_constraints({"minimum_margin_pct": 30}) == {"minimum_margin_pct": 30.0}
    assert digest(canonical_constraints({"minimum_margin_pct": 30})) == digest(
        canonical_constraints({"minimum_margin_pct": 30.0}))
    constraints, request = {"minimum_margin_pct": 30.01}, {"margin": 0.2}
    assert check_product_margin(Decimal("69.99"), Decimal("100"), constraints, request)["product_margin_satisfied"]
    with pytest.raises(ConstraintError, match="^product_constraint_not_satisfied$"):
        check_product_margin(Decimal("69.991"), Decimal("100"), constraints, request)


def test_tightened_floor_uses_only_nonstock_coverage():
    request, snapshot, _, plan = stock_margin_fixture()
    constraints = {"minimum_margin_pct": 95}
    blueprint = constrained_blueprint(snapshot, request, constraints)
    plan["blueprint_sha256"] = canonical_hash(blueprint)
    # Cost 35 / backed sales 500 is 93%; full sales would falsely give 96.5%.
    with pytest.raises(ConstraintError, match="product_constraint_not_satisfied"):
        build_change_set(request, snapshot, blueprint, plan, product_constraints=constraints)


def test_zero_spend_zero_coverage_satisfies_tightened_inequality_without_percentage():
    request, snapshot, _, plan = stock_margin_fixture(stock=10)
    constraints = {"minimum_margin_pct": 95}
    blueprint = constrained_blueprint(snapshot, request, constraints)
    plan["blueprint_sha256"] = canonical_hash(blueprint)
    result = build_change_set(request, snapshot, blueprint, plan, product_constraints=constraints)
    checks = result["projected_state"]["checks"]
    assert checks["product_margin_satisfied"] is True
    assert checks["combined_new_spend_margin"] is None
    assert checks["new_spend_revenue"] == "0"


def test_tightened_blueprint_keeps_request_and_checks_actual_document_costs():
    request, snapshot, original, plan = fixture()
    before = copy.deepcopy((request, snapshot, original))
    constraints = {"minimum_margin_pct": 90}
    blueprint = constrained_blueprint(snapshot, request, constraints)
    assert blueprint["margin"] == 0.9 and request["margin"] == original["margin"] == 0.2
    assert {**blueprint, "margin": original["margin"]} == original
    plan["blueprint_sha256"] = canonical_hash(blueprint)
    changes = build_change_set(request, snapshot, blueprint, plan, product_constraints=constraints)
    checks = validate_projection(changes, request, snapshot, blueprint, plan, product_constraints=constraints)
    assert checks["required_margin"] == 0.2 and checks["product_minimum_margin_pct"] == 90
    assert checks["combined_new_spend_margin"] == 0.93 and checks["projected_new_spend"] == 70
    assert (request, snapshot, original) == before

    # A solver cannot attest to satisfying a constraint by lying about margin.
    constraints = {"minimum_margin_pct": 95}
    blueprint = constrained_blueprint(snapshot, request, constraints)
    plan["blueprint_sha256"] = canonical_hash(blueprint)
    plan["plan"]["margin"] = 0.99
    with pytest.raises(ConstraintError, match="^product_constraint_not_satisfied$"):
        build_change_set(request, snapshot, blueprint, plan, product_constraints=constraints)


@pytest.mark.parametrize("fault", ["missing_zero", "missing_positive", "unknown", "duplicate"])
def test_stock_report_may_omit_only_snapshot_proven_zero_stock(fault):
    request, snapshot, blueprint, plan = fixture()
    if fault in {"missing_zero", "missing_positive"}:
        plan["plan"]["stock_report"] = [{"code": "FIN", "used": 0}]
        if fault == "missing_positive":
            snapshot["models"]["stock.quant"][1]["quantity"] = 1
            blueprint = constrained_blueprint(snapshot, request)
            plan["blueprint_sha256"] = canonical_hash(blueprint)
    else:
        plan["plan"]["stock_report"].append({"code": "FIN" if fault == "duplicate" else "UNKNOWN", "used": 0})
    if fault == "missing_zero":
        result = build_change_set(request, snapshot, blueprint, plan)
        assert result["projected_state"]["stock_used"]["RAW"] == 0
    else:
        with pytest.raises(ChangeSetError, match="Stock report scope mismatch"):
            build_change_set(request, snapshot, blueprint, plan)


def test_actual_pinned_solver_honors_tightened_floor_and_rejects_impossible_fixture(system, tmp_path):
    # The fixture only builds the approved bundle; solve/check run in a new
    # isolated Python process, so its synthetic solver monkeypatch is absent.
    bundle = system[2]["bundle"].resolve()
    input_file = tmp_path / "invented-constraint-records.json"
    input_file.write_text(json.dumps(fixture()[:2]))
    script = """
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from forgeflow.planning.tool import SolverFailure, solve_blueprint
from forgeflow.recovery.product_constraints import constrained_blueprint
from forgeflow.recovery.change_set import build_change_set, validate_projection
request, snapshot = json.loads(Path(sys.argv[2]).read_text())
constraints = {"minimum_margin_pct": 90}
blueprint = constrained_blueprint(snapshot, request, constraints)
plan = solve_blueprint(blueprint, root / "upstream")
changes = build_change_set(request, snapshot, blueprint, plan, product_constraints=constraints)
checks = validate_projection(changes, request, snapshot, blueprint, plan, product_constraints=constraints)
assert checks["projected_new_spend"] == 70 and checks["combined_new_spend_margin"] == 0.93
assert checks["product_margin_satisfied"] is True and request["margin"] == 0.2
assert plan["trace"]["solve_calls"]
assert all(call["status"] == "OPTIMAL" for call in plan["trace"]["solve_calls"])
try:
    solve_blueprint(constrained_blueprint(snapshot, request, {"minimum_margin_pct": 95}), root / "upstream")
except SolverFailure as error:
    assert str(error) == "planning_model_infeasible"
    assert error.trace["solve_calls"][-1]["status"] == "INFEASIBLE"
else:
    raise AssertionError("Impossible invented fixture unexpectedly returned a plan")
import procurement.solver
assert Path(procurement.solver.__file__).resolve().is_relative_to(root / "upstream")
print(json.dumps({"checks": checks, "solver": plan["solver"], "trace": plan["trace"],
                  "impossible_fixture": "INFEASIBLE", "scope": "invented records, no ERP"}))
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(bundle), str(input_file)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["impossible_fixture"] == "INFEASIBLE"
    assert not list(Path(bundle).rglob("__pycache__"))


def test_actual_pinned_solver_rejects_stock_denominator_counterexample(system, tmp_path):
    bundle = system[2]["bundle"].resolve()
    input_file = tmp_path / "stock-margin-inputs.json"
    input_file.write_text(json.dumps(stock_margin_fixture(unit_cost=76, floor=0.5)[:2]))
    script = """
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from forgeflow.planning.tool import SolverFailure, solve_blueprint
from forgeflow.recovery.product_constraints import constrained_blueprint
request, snapshot = json.loads(Path(sys.argv[2]).read_text())
try:
    solve_blueprint(constrained_blueprint(snapshot, request), root / 'upstream')
except SolverFailure as error:
    assert str(error) == 'planning_model_infeasible'
    assert error.trace['solve_calls'][-1]['status'] == 'INFEASIBLE'
    print('INFEASIBLE')
else:
    raise AssertionError('Stock revenue incorrectly made the real solver feasible')
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(bundle), str(input_file)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "INFEASIBLE"


@pytest.mark.parametrize("phase,status,fixed_spend,infeasible", [
    ("scenario=0 primary", "INFEASIBLE", None, True),
    ("scenario=0 primary retry", "INFEASIBLE", None, True),
    ("scenario=0 primary", "UNKNOWN", None, False),
    ("scenario=0 spend secondary", "INFEASIBLE", None, False),
    ("scenario=0 primary", "INFEASIBLE", 7000, False),
])
def test_only_observed_primary_infeasibility_is_classified(monkeypatch, tmp_path, phase, status, fixed_spend, infeasible):
    payload = {"models": [{"fixed_spend_cents": fixed_spend}],
               "solve_calls": [{"phase": phase, "status": status}]}

    @contextmanager
    def capture():
        yield SimpleNamespace(model_dump=lambda **kwargs: payload)

    def fail():
        raise RuntimeError("status=INFEASIBLE private-canary; untrusted prose")

    replacement = SimpleNamespace(
        ScenarioBlueprint=SimpleNamespace(model_validate=lambda value: value),
        ScenarioGenerator=lambda *args, **kwargs: SimpleNamespace(solve_plan_for_objective=fail),
        capture_procurement_solver_trace=capture,
    )
    monkeypatch.setitem(sys.modules, "procurement.solver", replacement)
    with pytest.raises(RuntimeError) as caught:
        tool.solve_blueprint({"objective_kind": "repair_plan"}, tmp_path)
    if infeasible:
        assert isinstance(caught.value, SolverFailure)
        assert str(caught.value) == "planning_model_infeasible" and caught.value.trace == payload
    else:
        assert type(caught.value) is RuntimeError and "private-canary" in str(caught.value)


def bind_synthetic_solver(monkeypatch):
    original = service.solve_blueprint

    def solve(blueprint, upstream):
        plan = original(blueprint, upstream)
        plan["blueprint_sha256"] = canonical_hash(blueprint)
        return plan

    monkeypatch.setattr(service, "solve_blueprint", solve)


def prepare(system, constraints, *, revision=0, operation="prepare-constraints"):
    connection, request, options, _ = system
    return service.prepare_product(connection, request, run_id=root_tests.RUN,
                                   owner_sha256=root_tests.OWNER, expected_revision=revision,
                                   operation_id=operation, constraints=constraints,
                                   contract=root_tests._contract(), **options)


def test_invalid_constraint_does_not_open_erp_or_retire_current_plan(system):
    decision = root_tests.prepare(system)
    _, request, options, _ = system
    with pytest.raises(ConstraintError, match="product_constraints_must_tighten"):
        service.prepare_product(None, request, run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
                                expected_revision=decision["revision"], operation_id="invalid-revision",
                                constraints={"minimum_margin_pct": 19}, contract=root_tests._contract(),
                                connection_factory=lambda: pytest.fail("no ERP connection"), **options)
    assert ProductVersions(options["store"]).status(root_tests.RUN, root_tests.OWNER) == decision


def test_root_binds_constraints_and_preserves_contract_through_apply_and_readonly_reconcile(system, monkeypatch):
    bind_synthetic_solver(monkeypatch)
    connection, request, options, _ = system
    request_before, policy_before = copy.deepcopy(request), root_tests._contract().payload()
    decision = prepare(system, {"minimum_margin_pct": 90})
    plan_id = decision["selected_plan_id"]
    directory = options["store"] / plan_id
    manifest = read_json(directory / "manifest.json")
    assert read_json(directory / "request.json") == request == request_before
    assert read_json(directory / "product-policy.json") == policy_before
    assert load_constraints(directory, manifest, request) == {"minimum_margin_pct": 90}
    assert manifest["product_constraints_sha256"] == digest({"minimum_margin_pct": 90.0})
    assert connection.writes == []
    grant = root_tests.grant_from(root_tests.authorize(options, decision)["approval"])
    result = service.apply(connection, plan_id, grant=grant, require_grant=True, **options)
    assert result["execution_validated"] is True
    before, writes = copy.deepcopy(connection.db), list(connection.writes)
    monkeypatch.setattr(scope_guard, "collect_snapshot", lambda client, request: connection.collect(request))
    receipt = reconciliation.reconcile_original(connection, root_tests.RUN, plan_id, **options,
        approvals=options["store"].parent / "approvals",
        verifier=lambda: {"status": "passed", "strict_success": True, "evaluation_valid": True,
                          "evaluation_scope": "product_original_verifier"})
    assert receipt["execution_validated"] is True
    assert before == connection.db and writes == connection.writes


@pytest.mark.parametrize("floor", [None, 80])
def test_later_version_cannot_drop_or_relax_tighter_floor(system, monkeypatch, floor):
    bind_synthetic_solver(monkeypatch)
    first = prepare(system, {"minimum_margin_pct": 90})
    before = copy.deepcopy(first)
    with pytest.raises(ConstraintError, match="product_constraints_relaxed"):
        prepare(system, {} if floor is None else {"minimum_margin_pct": floor},
                revision=first["revision"], operation="relax")
    assert ProductVersions(system[2]["store"]).status(root_tests.RUN, root_tests.OWNER) == before
    assert system[0].writes == []


def test_successful_tightening_and_completed_replay_keep_exact_version_without_erp(system, monkeypatch):
    bind_synthetic_solver(monkeypatch)
    first = prepare(system, {"minimum_margin_pct": 90})
    old_grant = root_tests.manual_grant(system[2], first)
    second = prepare(system, {"minimum_margin_pct": 91}, revision=first["revision"], operation="tighter")
    assert second["version"] == 2 and second["selected_plan_id"] != first["selected_plan_id"]
    assert second["plans"][first["selected_plan_id"]]["state"] == "retired"
    _, request, options, _ = system
    replay = service.prepare_product(None, request, run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
        expected_revision=first["revision"], operation_id="tighter", constraints={"minimum_margin_pct": 91.0},
        contract=root_tests._contract(), connection_factory=lambda: pytest.fail("completed replay opens ERP"), **options)
    assert replay == second
    with pytest.raises(VersionError, match="product_plan_retired"):
        service.apply(system[0], first["selected_plan_id"], grant=old_grant, **options)
    assert system[0].writes == []


@pytest.mark.parametrize("fault", ["missing", "changed", "hash", "downgrade"])
def test_private_constraint_registry_corruption_cannot_silently_relax(system, monkeypatch, fault):
    bind_synthetic_solver(monkeypatch)
    first = prepare(system, {"minimum_margin_pct": 90})
    path = system[2]["store"] / "product-decisions" / f"{root_tests.RUN}.json"
    record = read_json(path)
    if fault == "missing":
        record.pop("constraints")
    elif fault == "changed":
        record["constraints"] = {}
    elif fault == "hash":
        record["constraints_sha256"] = "0" * 64
    else:
        record["schema_version"] = 1
    atomic_json(path, record, replace=True)
    with pytest.raises(VersionError, match="product_decision_corrupt"):
        prepare(system, {}, revision=first["revision"], operation="relaxed")
    assert system[0].writes == []


@pytest.mark.parametrize("constraints,reason", [({"minimum_margin_pct": "private-canary"}, "product_constraints_invalid"),
                                              ({"minimum_margin_pct": 0.3}, "product_constraints_must_tighten")])
def test_product_handler_rejects_invalid_constraint_with_fixed_reason_before_erp(system, constraints, reason):
    _, request, options, _ = system
    policy = options["store"].parent / "policy.json"
    atomic_json(policy, root_tests._contract().payload())
    handler = RecoveryHandlers(connection_factory=lambda: pytest.fail("must not connect ERP"),
                               policy_file=policy, product_verifier=lambda: None, **options)
    with pytest.raises(ProtocolError) as caught:
        handler.product(root_tests.RUN, "prepare", {"owner_sha256": root_tests.OWNER, "expected_revision": 0,
            "operation_id": "invalid", "request": request, "constraints": constraints})
    assert caught.value.reason == reason
    assert "private-canary" not in json.dumps(caught.value.to_dict("product"))


def test_product_solver_failure_has_bounded_reason_and_retains_failed_version(system, monkeypatch):
    _, request, options, _ = system
    policy = options["store"].parent / "policy.json"
    atomic_json(policy, root_tests._contract().payload())
    handler = RecoveryHandlers(connection_factory=lambda: system[0], policy_file=policy,
                               product_verifier=lambda: None, **options)

    def fail(*args):
        raise SolverFailure("planning_model_infeasible", {"private-canary": True})

    monkeypatch.setattr(service, "solve_blueprint", fail)
    with pytest.raises(ProtocolError) as caught:
        handler.product(root_tests.RUN, "prepare", {"owner_sha256": root_tests.OWNER, "expected_revision": 0,
            "operation_id": "infeasible", "request": request, "constraints": {"minimum_margin_pct": 95}})
    assert caught.value.reason == "planning_model_infeasible" and caught.value.status == 422
    assert "private-canary" not in json.dumps(caught.value.to_dict("product"))
    state = ProductVersions(options["store"]).status(root_tests.RUN, root_tests.OWNER)
    assert state["state"] == "prepare_failed" and not state["plans"] and system[0].writes == []


def test_failed_tightening_retires_old_plan_without_resurrecting_weaker_constraints(system, monkeypatch):
    bind_synthetic_solver(monkeypatch)
    first = prepare(system, {"minimum_margin_pct": 90})
    with pytest.raises(ConstraintError, match="product_constraint_not_satisfied"):
        prepare(system, {"minimum_margin_pct": 95}, revision=first["revision"], operation="too-tight")
    registry = ProductVersions(system[2]["store"])
    failed = registry.status(root_tests.RUN, root_tests.OWNER)
    assert failed["state"] == "prepare_failed" and failed["selected_plan_id"] is None
    assert failed["plans"][first["selected_plan_id"]]["state"] == "retired"
    with pytest.raises(ConstraintError, match="product_constraints_relaxed"):
        prepare(system, {"minimum_margin_pct": 90}, revision=failed["revision"], operation="weaker")
    with pytest.raises(VersionError, match="product_preparation_interrupted"):
        prepare(system, {"minimum_margin_pct": 95}, revision=first["revision"], operation="too-tight")
    assert system[0].writes == []


@pytest.mark.parametrize("action", ["apply", "reconcile", "cancel"])
@pytest.mark.parametrize("fault", ["missing", "changed", "corrupt", "link", "fifo"])
def test_constraint_artifact_fault_stops_all_root_paths_before_dispatch(system, monkeypatch, action, fault):
    import os

    bind_synthetic_solver(monkeypatch)
    decision = prepare(system, {"minimum_margin_pct": 90})
    connection, _, options, _ = system
    token = root_tests.authorize(options, decision)["approval"]
    grant = root_tests.grant_from(token)
    plan_id = decision["selected_plan_id"]
    directory = options["store"] / plan_id
    path = directory / "product-constraints.json"
    path.unlink()
    if fault == "changed":
        atomic_json(path, {})
    elif fault == "corrupt":
        path.write_text("{")
    elif fault == "link":
        path.symlink_to(directory / "request.json")
    elif fault == "fifo":
        os.mkfifo(path)
    with pytest.raises(ConstraintError, match="product_constraints_binding_changed"):
        if action == "apply":
            service.apply(connection, plan_id, grant=grant, **options)
        elif action == "reconcile":
            reconciliation.reconcile_original(connection, root_tests.RUN, plan_id, **options,
                approvals=options["store"].parent / "approvals",
                verifier=lambda: pytest.fail("must not run verifier"))
        else:
            unstarted.cancel_unstarted(run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
                plan_id=plan_id, approval={k: v for k, v in token.items() if k != "schema_version"},
                approvals=options["store"].parent / "approvals", **options)
    assert connection.writes == []
    assert not (options["store"].parent / "approvals" / f"approval-{grant.approval.nonce}.used").exists()


def test_legacy_artifact_absence_and_unbound_constraints(tmp_path):
    assert load_constraints(tmp_path, {}, {"margin": 0.2}) == {}
    atomic_json(tmp_path / "product-constraints.json", {"minimum_margin_pct": 90})
    with pytest.raises(ConstraintError, match="product_constraints_binding_changed"):
        load_constraints(tmp_path, {}, {"margin": 0.2})


@pytest.mark.parametrize("tamper", ["context", "hash", "policy", "request"])
def test_constraint_binding_checks_original_request_policy_and_version_context(system, monkeypatch, tamper):
    bind_synthetic_solver(monkeypatch)
    decision = prepare(system, {"minimum_margin_pct": 90})
    directory = system[2]["store"] / decision["selected_plan_id"]
    manifest, request = read_json(directory / "manifest.json"), copy.deepcopy(system[1])
    if tamper == "context":
        manifest["product"]["inputs_sha256"] = "f" * 64
    elif tamper == "hash":
        manifest["product_constraints_sha256"] = "f" * 64
    elif tamper == "policy":
        manifest["policy_sha256"] = "f" * 64
    else:
        request["margin"] = 0.3
    with pytest.raises(ConstraintError, match="product_constraints_binding_changed"):
        load_constraints(directory, manifest, request)
