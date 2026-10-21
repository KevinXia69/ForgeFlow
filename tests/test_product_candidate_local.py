"""Local candidate projection/selection over a real socket and invented ERP."""
from __future__ import annotations

import copy
import json

import pytest
import test_product_candidates as candidate_tests
import test_product_versions as root_tests

from forgeflow.executor.daemon import Handlers
from forgeflow.executor.handlers import RecoveryHandlers
from forgeflow.product import local
from forgeflow.product.candidates import (
    load_local_comparison,
    normalize_comparison,
    project_comparison,
)
from forgeflow.product.executor import ProductExecutionError
from forgeflow.product.service import ProductCaseService
from forgeflow.product.state import ProductCase, utc_stamp
from forgeflow.recovery import client, service
from forgeflow.recovery.product_versions import ProductVersions, owner_scope
from forgeflow.recovery.storage import atomic_json, read_json

system = root_tests.system
candidates = candidate_tests.candidates
serve = root_tests.serve
workspace = root_tests.workspace
handlers = root_tests.handlers
ACTOR = "candidate-test-owner"


@pytest.fixture
def wired_candidates(candidates, serve, monkeypatch, tmp_path):
    system, calls, plans = candidates
    connection, request, options, _ = system
    policy = options["store"].parent / "reviewed-policy.json"
    atomic_json(policy, root_tests._contract().payload())
    root = RecoveryHandlers(connection_factory=lambda: connection, policy_file=policy,
                            product_verifier=lambda: pytest.fail("selection called verifier"), **options)
    server = serve(custom=Handlers(root.prepare, root.apply, root.status, product=root.product,
                                  reconcile=root.reconcile))
    specs = copy.deepcopy(local.SCENARIOS)
    specs["B"].update(product_code=request["target_product_code"], disruption=request["disruption"])
    monkeypatch.setattr(local, "SCENARIOS", specs)
    path = tmp_path / "request.json"
    atomic_json(path, request)
    executor = local.LocalProductExecutor(socket_path=str(server.socket_path), store=options["store"],
        approvals=options["store"].parent / "approvals", request=path, container="synthetic-never-docker",
        scenario="B", enable_revisions=True, enable_candidates=True)
    case = ProductCase.new(ACTOR, "B")
    case.order["units"] = 10
    case.preparation = {"phase": "pending", "intent": executor.preparation_intent(ProductCaseService._executor_view(case))}
    return executor, case, system, calls, plans, root


def prepare(wired):
    executor, case, *_ = wired
    result = executor.prepare(ProductCaseService._executor_view(case))
    assert set(result) == {"candidate_set"}
    return normalize_comparison(case, result["candidate_set"])


def test_local_reads_two_candidates_and_projects_only_public_bound_facts(wired_candidates):
    executor, case, system, calls, _, _ = wired_candidates
    comparison = prepare(wired_candidates)
    assert comparison["selected_plan_id"] is None
    assert [plan["costs"]["planned"]["total"] for plan in comparison["plans"]] == ["62.0", "50.0"]
    assert [plan["objective"] for plan in comparison["plans"]] == ["preserve_quantities", "min_planned_cost"]
    # The non-capacity profile must still check individual customer budgets.
    for plan in comparison["plans"]:
        budget = next(row for row in plan["checks"] if row["code"] == "budget")
        assert budget["basis"] == {"kind": "sales_order_value_per_customer_cap_v1",
            "plan_id": plan["plan_id"], "snapshot_sha256": plan["snapshot_sha256"]}
        assert budget["status"] == "pass" and budget["actual"] == budget["threshold"] == 1
    public = project_comparison(comparison)
    serialized = json.dumps(public)
    for private in (ACTOR, owner_scope(ACTOR), "executor_binding", "root_revision", "comparison_sha256", "solver_trace"):
        assert private not in serialized
    assert len(public["attempts"]) == 2 and all(item["status"] == "prepared" for item in public["attempts"])
    assert executor.prepare(ProductCaseService._executor_view(case))["candidate_set"] == comparison
    assert len(calls) == 2 and system[0].writes == []


@pytest.mark.parametrize("fault", [None, "binding", "count"])
def test_candidate_store_preserves_budget_basis_and_refuses_inconsistent_claims(wired_candidates, tmp_path, fault):
    from forgeflow.product.candidate_store import CandidateStore

    _, case, *_ = wired_candidates
    comparison = prepare(wired_candidates)
    store = CandidateStore(tmp_path / "cases")
    if fault:
        budget = next(row for row in comparison["plans"][1]["checks"] if row["code"] == "budget")
        if fault == "binding":
            budget["basis"]["plan_id"] = comparison["plans"][0]["plan_id"]
        else:
            budget["actual"] = 0
        with pytest.raises(ValueError, match="invalid_budget_check"):
            store.save(case, comparison)
        return
    reference = store.save(case, comparison)
    loaded = store.load(case, reference)
    assert loaded == comparison
    public = project_comparison(loaded)
    assert [plan["checks"] for plan in public["plans"]] == [plan["checks"] for plan in comparison["plans"]]


def test_local_selection_lost_reply_reuses_original_intent_and_never_solves_again(wired_candidates, monkeypatch):
    executor, case, system, calls, _, root = wired_candidates
    comparison = prepare(wired_candidates)
    view = ProductCaseService._executor_view(case)
    selected_id = comparison["plans"][1]["plan_id"]
    intent = executor.selection_intent(view, comparison, selected_id)
    real_product = client.product

    def lost(*args, **kwargs):
        real_product(*args, **kwargs)
        raise client.ExecutorError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(client, "product", lost)
        with pytest.raises(ProductExecutionError, match="unavailable"):
            executor.continue_selection(view, comparison, intent)
    monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("selection recovery opened ERP"))
    chosen = executor.continue_selection(view, comparison, copy.deepcopy(intent))
    assert chosen["selected_plan_id"] == selected_id
    assert chosen["root_revision"] == comparison["root_revision"] + 1
    assert chosen["comparison_sha256"] == comparison["comparison_sha256"]
    assert [p["prepared_at"] for p in chosen["plans"]] == [p["prepared_at"] for p in comparison["plans"]]
    assert executor.continue_selection(view, comparison, intent) == chosen
    assert normalize_comparison(case, chosen) == chosen
    assert len(calls) == 2 and system[0].writes == []
    registry = ProductVersions(system[2]["store"]).status(f"local-{case.case_id}", owner_scope(ACTOR))
    assert registry["selected_plan_id"] == selected_id
    assert not list(executor.approvals.glob("approval-*.json"))


@pytest.mark.parametrize("fault", ["audit_changed", "audit_missing", "audit_symlink", "plan_objective", "mixed_snapshot"])
def test_local_rejects_missing_changed_or_mixed_comparison_before_any_selection(wired_candidates, fault):
    executor, case, system, calls, _, _ = wired_candidates
    comparison = prepare(wired_candidates)
    directory = executor.store / "product-comparisons"
    path = directory / (comparison["comparison_id"] + ".json")
    if fault == "audit_changed":
        value = read_json(path)
        value["attempts"][1]["state"] = "duplicate"
        atomic_json(path, value, replace=True)
    elif fault == "audit_missing":
        path.unlink()
    elif fault == "audit_symlink":
        copy_path = directory / "original.json"
        path.rename(copy_path)
        path.symlink_to(copy_path)
    else:
        plan_id = comparison["plans"][1]["plan_id"]
        name = "plan.json" if fault == "plan_objective" else "manifest.json"
        path = executor.store / plan_id / name
        value = read_json(path)
        if fault == "plan_objective":
            value["product_objective"] = "preserve_quantities"
        else:
            value["artifacts"]["snapshot"] = "a" * 64
        atomic_json(path, value, replace=True)
    with pytest.raises(ProductExecutionError, match="product_comparison_invalid|plan_not_bound"):
        executor.prepare(ProductCaseService._executor_view(case))
    assert len(calls) == 2 and system[0].writes == []
    registry = ProductVersions(executor.store).status(f"local-{case.case_id}", owner_scope(ACTOR))
    assert registry["state"] == "prepared" and registry["selected_plan_id"] is None


@pytest.mark.parametrize("fault", ["private_attempt", "unknown_objective", "wrong_cost_basis", "foreign_owner",
                                   "different_revision", "unknown_selected"])
def test_comparison_normalization_rejects_private_or_inconsistent_records(wired_candidates, fault):
    _, case, *_ = wired_candidates
    comparison = prepare(wired_candidates)
    if fault == "private_attempt":
        comparison["attempts"][0]["private"] = "canary"
    elif fault == "unknown_objective":
        comparison["plans"][1]["objective"] = "global_minimum"
    elif fault == "wrong_cost_basis":
        comparison["plans"][1]["costs"]["basis"]["pricing_sha256"] = "a" * 64
    elif fault == "foreign_owner":
        comparison["plans"][0]["executor_binding"]["product"]["owner_sha256"] = "b" * 64
    elif fault == "different_revision":
        comparison["plans"][0]["executor_binding"]["revision"] += 1
    else:
        comparison["selected_plan_id"] = "a" * 64
    with pytest.raises(ValueError):
        normalize_comparison(case, comparison)


def test_changed_capability_does_not_silently_replace_pending_preparation(wired_candidates):
    executor, case, system, calls, _, _ = wired_candidates
    executor.supports_candidates = False
    with pytest.raises(ProductExecutionError, match="product_binding_changed"):
        executor.prepare(ProductCaseService._executor_view(case))
    assert not calls and not system[0].writes


def test_comparison_order_comes_from_bound_audit_not_transport_mapping_order(wired_candidates):
    executor, case, *_ = wired_candidates
    expected = prepare(wired_candidates)
    view = ProductCaseService._executor_view(case)
    decision = executor._product(view, "status", {"owner_sha256": view["_owner_sha256"]})["decision"]
    for reverse in (False, True):
        decision["plans"] = {key: decision["plans"][key] for key in sorted(decision["plans"], reverse=reverse)}
        assert load_local_comparison(executor, view, decision, {}) == expected


def test_local_revision_keeps_objective_set_and_retires_all_previous_candidates(
        wired_candidates, monkeypatch, tmp_path):
    executor, case, system, calls, _, _ = wired_candidates
    comparison = prepare(wired_candidates)
    view = ProductCaseService._executor_view(case)
    first = comparison["plans"][0]["plan_id"]
    intent = executor.selection_intent(view, comparison, first)
    chosen = executor.continue_selection(view, comparison, intent)
    case.plan = next(plan for plan in chosen["plans"] if plan["plan_id"] == first)
    case.preparation["phase"] = "completed"
    changes = {"minimum_margin_pct": 90}
    view = ProductCaseService._executor_view(case)
    revision_intent = executor.revision_intent(view, case.plan, changes)
    case.preparation = {"kind": "revision", "phase": "pending", "intent": revision_intent,
                        "changes": changes, "reason": "test a tighter floor", "requested_at": utc_stamp()}
    outputs = {}

    def solve(blueprint, upstream, *, product_objective):
        calls.append(product_objective)
        if not outputs:
            outputs.update(candidate_tests.pinned_plans(system[2]["bundle"], blueprint, tmp_path))
        return copy.deepcopy(outputs[product_objective])

    monkeypatch.setattr(service, "solve_blueprint", solve)
    outcome = executor.continue_revision(ProductCaseService._executor_view(case))
    assert outcome["status"] == "prepared" and outcome["version"] == 2
    target = copy.deepcopy(case)
    target.revision = {"version": 2, "changes": changes}
    revised = normalize_comparison(target, outcome["plan"]["candidate_set"])
    assert revised["selected_plan_id"] is None and revised["comparison_id"] != comparison["comparison_id"]
    assert [plan["costs"]["planned"]["total"] for plan in revised["plans"]] == ["62.0", "50.0"]
    assert len(calls) == 4 and not system[0].writes
    current = ProductVersions(system[2]["store"]).status(f"local-{case.case_id}", owner_scope(ACTOR))
    for plan in comparison["plans"]:
        assert current["plans"][plan["plan_id"]]["state"] == "retired"
    with pytest.raises(ProductExecutionError, match="product_plan_retired|product_revision_conflict"):
        executor.continue_selection(view, comparison, intent)
