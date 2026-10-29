from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from recovery_synthetic import inputs
from test_recovery_change_set import stock_margin_fixture

from forgeflow.planning.live import build_blueprint
from forgeflow.product import local
from forgeflow.product.executor import ProductExecutionError
from forgeflow.product.local import LocalProductExecutor
from forgeflow.recovery.change_set import build_change_set, canonical_hash
from forgeflow.recovery.margin import MARGIN_BASIS
from forgeflow.recovery.product_costs import build_cost_report
from forgeflow.recovery.product_versions import owner_scope
from forgeflow.recovery.storage import digest

SHA = "a" * 64
SHA2 = "b" * 64


def _binding(case_id="2" * 32, actor="test-owner"):
    return {"revision": 2, "product": {"schema_version": 1, "run_id": f"local-{case_id}",
            "owner_sha256": owner_scope(actor), "version": 1, "comparison_id": "c" * 32,
            "inputs_sha256": "d" * 64}}


def _inputs():
    request, snapshot, _, wrapped = inputs("origin_only")
    request["margin"] = 0.275
    blueprint = build_blueprint(snapshot, request)
    wrapped["blueprint_sha256"] = canonical_hash(blueprint)
    return request, snapshot, blueprint, wrapped


def _executor(tmp_path: Path, monkeypatch, *, values=None) -> LocalProductExecutor:
    values = values or _inputs()
    scenarios = copy.deepcopy(local.SCENARIOS)
    scenarios["A"].update(product_code=values[0]["target_product_code"],
                          disruption=values[0]["disruption"])
    monkeypatch.setattr(local, "SCENARIOS", scenarios)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(values[0]))
    return LocalProductExecutor(
        socket_path="/tmp/unused.sock", store=tmp_path / "store",
        approvals=tmp_path / "approvals", request=request, container="sandbox",
    )


def _artifacts(
    store: Path,
    plan_id: str,
    *,
    margin: float | None = 0.275,
    objective_kind: str | None = "repair_plan",
    values=None,
    mutate=None,
) -> str:
    # Bind genuine ERP flows and independently recomputed costs. Corruption tests
    # may change evidence only after a valid change-set has been constructed.
    request, snapshot, blueprint, wrapped = copy.deepcopy(values or _inputs())
    changes = build_change_set(request, snapshot, blueprint, wrapped)
    wrapped["plan"].update(margin=margin, objective_kind=objective_kind)
    if mutate is not None:
        mutate(request, snapshot, blueprint, wrapped)
    changes["source_sha256"] = {name: canonical_hash(value) for name, value in (
        ("request", request), ("snapshot", snapshot), ("blueprint", blueprint), ("plan", wrapped))}
    costs = build_cost_report(request, snapshot, blueprint, changes)
    snapshot_sha = digest(snapshot)
    prepared = {
        "policy_sha256": SHA, "snapshot_sha256": snapshot_sha,
        "prepared_at": "2026-10-22T00:00:00+00:00",
    }
    product = _binding()["product"]
    product["inputs_sha256"] = digest({"request": request, "policy_sha256": SHA, "constraints": {}})
    artifacts = {"snapshot": snapshot, "request": request, "plan": wrapped,
                 "change-set": changes, "blueprint": blueprint}
    manifest = {"artifacts": {name: digest(value) for name, value in artifacts.items()},
                "product": product, "policy_sha256": SHA, "synthetic_id": plan_id,
                "margin_basis": MARGIN_BASIS, "product_constraints_sha256": digest({}),
                "product_costs_sha256": digest(costs)}
    plan_id = digest(manifest)
    directory = store / plan_id
    directory.mkdir(parents=True, exist_ok=True)
    artifacts.update(manifest=manifest, prepared=prepared,
                     **{"product-constraints": {}, "product-costs": costs})
    for name, value in artifacts.items():
        (directory / f"{name}.json").write_text(json.dumps(value))
    return plan_id


@pytest.mark.parametrize("objective", [None, "min_new_spend", "unknown"])
def test_default_prepare_refuses_missing_or_unbound_objective(tmp_path, monkeypatch, objective):
    from forgeflow.product.executor import ProductExecutionError

    executor = _executor(tmp_path, monkeypatch)
    plan_id = _artifacts(executor.store, "1" * 64, objective_kind=objective)
    with pytest.raises(ProductExecutionError, match="plan_not_bound"):
        _prepare(executor, monkeypatch, plan_id)


def _prepare(executor: LocalProductExecutor, monkeypatch, plan_id: str) -> dict:
    manifest = json.loads((executor.store / plan_id / "manifest.json").read_text())
    snapshot_sha = manifest["artifacts"]["snapshot"]
    monkeypatch.setattr(
        "forgeflow.product.local.client.product",
        lambda socket, run_id, action, payload, timeout: {"decision": {
            "selected_plan_id": plan_id, "state": "selected", "revision": 2,
            "plans": {plan_id: {"product": manifest["product"], "state": "selected",
                                "policy_sha256": SHA, "snapshot_sha256": snapshot_sha}}}},
    )
    return executor.prepare({"case_id": "2" * 32, "scenario": "A", "order": {"units": 10},
                             "_owner_sha256": owner_scope("test-owner")})


def _checks(plan: dict) -> dict[str, dict]:
    return {check["code"]: check for check in plan["checks"]}


def test_action_diff_uses_bound_dates_and_only_safe_recorded_fields(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    actions = executor._plan_actions({"operations": [{
        "action": "update", "kind": "manufacturing_order", "target": {"name": "MO1"},
        "before": {"quantity": 10, "date_start": "2026-10-25 08:00:00", "state": "confirmed"},
        "after": {"quantity": 10, "date_start": "2026-10-24 09:00:00",
                  "workorder_plan": {"duration_expected_minutes": 120, "secret": "synthetic-not-public"},
                  "api_key": "synthetic-not-public"},
    }]})
    assert actions[0]["before"] == {"quantity": 10, "date_start": "2026-10-25 08:00:00", "state": "confirmed"}
    assert actions[0]["after"] == {"quantity": 10, "date_start": "2026-10-24 09:00:00", "workorder_plan": {"duration_expected_minutes": 120}}
    assert "state" not in actions[0]["after"]
    assert "synthetic-not-public" not in json.dumps(actions)


def test_prepare_reads_artifacts_and_builds_real_actions(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    plan_id = "1" * 64
    plan_id = _artifacts(executor.store, plan_id)

    plan = _prepare(executor, monkeypatch, plan_id)
    assert plan["plan_id"] == plan_id
    assert plan["margin"] == 93
    assert plan["margin_basis"] == MARGIN_BASIS
    assert plan["new_spend"] == 70
    assert plan["cost_delta"] is None
    assert plan["policy_sha256"] == SHA
    assert plan["snapshot_sha256"] == digest(json.loads((executor.store / plan_id / "snapshot.json").read_text()))
    assert [a["kind"] for a in plan["actions"]] == ["cancel_mo", "adjust_mo", "update_po", "create_mo"]
    assert plan["actions"][0]["document"] == "MO1"
    assert plan["actions"][0]["before"]["quantity"] == 4
    assert plan["actions"][0]["after"] is None
    assert plan["actions"][2]["document"] == "PO1"
    assert plan["actions"][2]["before"]["quantity"] == 12
    assert plan["actions"][2]["after"]["quantity"] == 20
    assert plan["actions"][3]["before"] is None
    assert plan["actions"][3]["after"]["quantity"] == 4

    checks = _checks(plan)
    assert set(checks) == {"margin", "budget", "demand", "policy", "on_time"}
    assert plan["on_time"] is True
    assert checks["on_time"]["status"] == "pass"
    assert checks["margin"] == {
        "code": "margin",
        "label": "New-spend margin floor",
        "status": "pass",
        "actual": "93%",
        "threshold": "27.5%",
        "source": "independent new-spend coverage check; original floor 27.5%; effective floor 27.5%; excludes finished stock revenue",
    }
    assert checks["budget"]["status"] == "pass"
    assert checks["budget"]["label"] == "Sales-order values within individual customer caps"
    assert "not purchase authorization" in checks["budget"]["source"]
    assert checks["budget"]["actual"] == checks["budget"]["threshold"] == 2
    assert checks["budget"]["basis"] == {"kind": "sales_order_value_per_customer_cap_v1",
        "plan_id": plan_id, "snapshot_sha256": plan["snapshot_sha256"]}
    assert checks["demand"]["label"] == "Demand coverage"
    assert checks["demand"]["status"] == "pass"
    assert checks["demand"]["actual"] == "10 units"
    assert checks["demand"]["threshold"] == "10 units"
    assert checks["policy"]["status"] == "pass"
    assert checks["policy"]["actual"] == SHA
    for check in checks.values():
        assert "/" not in check["source"]
        assert str(tmp_path) not in check["source"]


def test_prepare_blocks_individual_overcap_even_when_planning_cost_is_below_sum_caps(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    def over_cap(request, snapshot, blueprint, wrapped):
        request["sales_order_budget_caps"]["1"] = 399  # SO1 = 4 × 100; planned cost = 70.
        blueprint.update(build_blueprint(snapshot, request))
        wrapped["blueprint_sha256"] = canonical_hash(blueprint)

    plan_id = _artifacts(executor.store, "6" * 64, mutate=over_cap)
    executor.request = json.loads((executor.store / plan_id / "request.json").read_text())
    with pytest.raises(ProductExecutionError, match="plan_not_bound"):
        _prepare(executor, monkeypatch, plan_id)


def test_default_profile_refuses_missing_bound_sales_amount(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    def missing_price(request, snapshot, blueprint, wrapped):
        snapshot["models"]["sale.order.line"][0]["price_unit"] = None

    plan_id = _artifacts(executor.store, "7" * 64, mutate=missing_price)
    with pytest.raises(ProductExecutionError, match="plan_not_bound"):
        _prepare(executor, monkeypatch, plan_id)


@pytest.mark.parametrize("artifact", ["request", "snapshot"])
def test_default_profile_refuses_unbound_budget_inputs(tmp_path, monkeypatch, artifact):
    from forgeflow.product.executor import ProductExecutionError

    executor = _executor(tmp_path, monkeypatch)
    plan_id = _artifacts(executor.store, "7" * 64)
    path = executor.store / plan_id / f"{artifact}.json"
    value = json.loads(path.read_text())
    value["changed"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ProductExecutionError, match="plan_not_bound"):
        _prepare(executor, monkeypatch, plan_id)


@pytest.mark.parametrize("solver_margin", [None, 0.265, 0.240, 0.99])
def test_prepare_derives_margin_independently_of_solver_margin(tmp_path, monkeypatch, solver_margin):
    executor = _executor(tmp_path, monkeypatch)
    plan_id = _artifacts(executor.store, "7" * 64, margin=solver_margin)
    check = _checks(_prepare(executor, monkeypatch, plan_id))["margin"]
    assert check["status"] == "pass"
    assert check["actual"] == "93%"
    assert check["threshold"] == "27.5%"


def test_prepare_rejects_stock_revenue_false_pass_under_the_hard_request_floor(tmp_path, monkeypatch):
    values = stock_margin_fixture(unit_cost=76, floor=0.2)
    executor = _executor(tmp_path, monkeypatch, values=values)
    def raised_floor(request, snapshot, blueprint, wrapped):
        request["margin"] = 0.5
        blueprint.update(build_blueprint(snapshot, request))
        wrapped["blueprint_sha256"] = canonical_hash(blueprint)

    # 400 cost / 500 non-stock sales = 20%; full sales would falsely report 60%.
    plan_id = _artifacts(executor.store, "7" * 64, values=values, margin=0.6, mutate=raised_floor)
    executor.request = json.loads((executor.store / plan_id / "request.json").read_text())
    with pytest.raises(ProductExecutionError, match="plan_not_bound"):
        _prepare(executor, monkeypatch, plan_id)


def test_prepare_refuses_unreported_solver_cost_instead_of_hiding_it(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    def missing_cost(request, snapshot, blueprint, wrapped):
        del wrapped["plan"]["expected_new_spend"]

    plan_id = _artifacts(executor.store, "8" * 64, mutate=missing_cost)
    with pytest.raises(ProductExecutionError, match="plan_not_bound"):
        _prepare(executor, monkeypatch, plan_id)


def test_prepare_maps_executor_failure_to_stable_code(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)

    class Failure(Exception):
        code = "plan_rejected"

    def prepare(*_args, **_kwargs):
        raise Failure()

    monkeypatch.setattr("forgeflow.product.local.client.product", prepare)
    with pytest.raises(Exception) as caught:
        executor.prepare({"case_id": "3" * 32, "scenario": "A", "_owner_sha256": owner_scope("test-owner")})
    assert getattr(caught.value, "code", None) == "plan_rejected"


def test_apply_uses_exact_root_approval_and_maps_verifier(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    plan = {
        "plan_id": "4" * 64, "policy_sha256": SHA, "snapshot_sha256": SHA2,
        "actions": [{"kind": "cancel_po", "document": "P00006", "quantity": 3}],
        "margin_basis": MARGIN_BASIS,
        "executor_binding": _binding("5" * 32),
    }
    seen = {}

    def apply(socket, run_id, plan_id, approval=None, timeout=900):
        seen["approval"] = approval
        assert plan_id == plan["plan_id"]
        assert approval["plan_id"] == plan_id
        return {"execution_validated": True, "verified_steps": 2}

    monkeypatch.setattr("forgeflow.product.local.client.apply", apply)
    monkeypatch.setattr("forgeflow.product.local.client.reconcile", lambda socket, run, given, timeout: {
        "run_id": run, "case_id": "5" * 32, "plan_id": given,
        "policy_sha256": plan["policy_sha256"], "snapshot_sha256": plan["snapshot_sha256"],
        "receipt_sha256": "c" * 64, "readback_sha256": "d" * 64,
        "execution_validated": True, "verified_steps": 2,
        "verifier": {"status": "passed", "score": 100.0,
                     "checks": [{"kind": "original_verifier", "passed": True}]},
    })
    issued = datetime.now(UTC)
    case = {
        "case_id": "5" * 32,
        "scenario": "A",
        "_owner_sha256": owner_scope("test-owner"),
        "approval": {
            "nonce": "6" * 32,
            "plan_id": plan["plan_id"],
            "approver": "test-owner",
            "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(seconds=900)).isoformat(),
        },
    }
    monkeypatch.setattr("forgeflow.product.local.client.product", lambda *a, **k: {
        "approval": {"schema_version": 1, **executor._approval_payload(case, plan)}})
    grant = executor.authorize(case, plan)
    case["approval"].update(phase="dispatching", executor_grant=grant)
    execution = executor.apply(case, plan)
    assert execution["status"] == "succeeded"
    assert execution["readback"]["records"][0]["document"] == "P00006"
    assert execution["verifier"]["score"] == 100.0
    assert not (tmp_path / "approvals").exists()
    assert seen["approval"]["nonce"] == "6" * 32
    assert set(seen["approval"]) == {
        "nonce", "run_id", "plan_id", "policy_sha256", "snapshot_sha256",
        "approver", "issued_at_utc", "expires_at_utc",
    }


def test_apply_refuses_an_unbound_approval(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    plan = {
        "plan_id": "4" * 64, "policy_sha256": SHA, "snapshot_sha256": SHA2,
        "actions": [],
        "margin_basis": MARGIN_BASIS,
        "executor_binding": _binding("5" * 32),
    }
    called = []
    monkeypatch.setattr("forgeflow.product.local.client.apply", lambda *a, **k: called.append(True))
    from forgeflow.product.executor import ProductExecutionError
    with pytest.raises(ProductExecutionError) as caught:
        executor.apply({"case_id": "5" * 32, "scenario": "A", "_owner_sha256": owner_scope("test-owner")}, plan)
    assert caught.value.code == "approval_not_bound"
    assert called == []


@pytest.mark.parametrize("score, passed, rows, strict, status", [
    (99.5, True, ["PASS", "FAIL"], False, "failed"),
    (100, True, ["PASS", "PASS", "NA"], True, "passed"),
    (100, True, [], None, "unknown"),
    (100, False, ["PASS"], None, "unknown"),
    (50, False, ["FAIL"], False, "failed"),
])
def test_original_verifier_requires_complete_strict_rule_evidence(
    tmp_path, monkeypatch, score, passed, rows, strict, status,
):
    from types import SimpleNamespace

    executor = _executor(tmp_path, monkeypatch)
    reward = {"overall_score": score, "passed": passed,
              "rules": {"total": len(rows), "by_dimension": {
                  "constraint": [{"status": row} for row in rows],
              }}}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(reward))

    monkeypatch.setattr("forgeflow.product.local.subprocess.run", run)
    verifier = executor._run_verifier()
    assert verifier["strict_success"] is strict
    assert verifier["status"] == status
    assert verifier["original_passed"] is passed
    assert verifier["rules"] == reward["rules"]
    assert verifier["passed_rule_count"] == (rows.count("PASS") if rows else None)
    assert verifier["applicable_rule_count"] == (len(rows) - rows.count("NA") if rows else None)
    assert verifier["checks"][0]["passed_count"] == verifier["passed_rule_count"]
    assert verifier["checks"][0]["applicable"] == verifier["applicable_rule_count"]
    assert calls[0][-1] == "/tests/test.sh"
    assert calls[1][-1] == "/logs/verifier/reward.json"


def test_original_verifier_process_failure_is_not_a_valid_measurement(tmp_path, monkeypatch):
    from types import SimpleNamespace

    executor = _executor(tmp_path, monkeypatch)
    monkeypatch.setattr("forgeflow.product.local.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1))
    verifier = executor._run_verifier()
    assert verifier["evaluation_valid"] is False
    assert verifier["strict_success"] is None


def test_on_time_is_derived_from_bound_allocation_flow_dates():
    plan = {"allocations": [{"task_order_ref": "SO1", "deadline": 5, "demand": 3}]}
    flow = {"sales_order": {"name": "SO1"}, "available_day": 4, "quantity": 3}
    changes = {"projected_state": {"sales_order_flows": [flow]}}
    assert LocalProductExecutor._on_time(plan, changes) is True
    flow["available_day"] = 6
    assert LocalProductExecutor._on_time(plan, changes) is False
    del flow["available_day"]
    assert LocalProductExecutor._on_time(plan, changes) is None


@pytest.mark.parametrize("stock,expected_margin,actual", [(10, None, "not applicable"), (0, 100, "100%")])
def test_zero_cost_uses_actual_stock_coverage_for_margin(tmp_path, monkeypatch, stock, expected_margin, actual):
    values = stock_margin_fixture(stock=stock, unit_cost=0, raw_price=0)
    executor = _executor(tmp_path, monkeypatch, values=values)
    plan_id = _artifacts(executor.store, "9" * 64, margin=None, values=values)
    plan = _prepare(executor, monkeypatch, plan_id)
    assert plan["margin"] == expected_margin
    assert plan["cost_delta"] is None
    assert plan["new_spend"] == 0
    assert _checks(plan)["margin"]["status"] == "pass"
    assert _checks(plan)["margin"]["actual"] == actual
    assert _checks(plan)["budget"]["status"] == "pass"


def test_reconcile_only_requests_bound_original_verification_and_never_reads_shared_reward(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    seen = []

    def reconcile(socket, run, plan, timeout):
        seen.append((run, plan))
        return {"execution_validated": True, "verified_steps": 2, "run_id": run,
                "case_id": "b" * 32, "plan_id": plan, "policy_sha256": SHA, "snapshot_sha256": SHA2,
                "receipt_sha256": "c" * 64, "readback_sha256": "d" * 64,
                "verifier": {"status": "passed", "strict_success": True, "evaluation_valid": True,
                             "evaluation_scope": "product_original_verifier"}}

    monkeypatch.setattr("forgeflow.product.local.client.reconcile", reconcile)
    monkeypatch.setattr("forgeflow.product.local.client.apply", lambda *a, **k: pytest.fail("must not reapply"))
    monkeypatch.setattr(executor, "_run_verifier", lambda: pytest.fail("must not restart verifier"))
    monkeypatch.setattr(executor, "_read_verifier_reward", lambda: pytest.fail("not a case-bound reward"))
    plan = {"plan_id": "a" * 64, "actions": [], "policy_sha256": SHA, "snapshot_sha256": SHA2}
    result = executor.reconcile({"case_id": "b" * 32, "scenario": "A", "execution": {"status": "unknown"}}, plan)
    assert seen == [("local-" + "b" * 32, "a" * 64)]
    assert result["status"] == "succeeded"
    assert result["verifier"]["strict_success"] is True


@pytest.mark.parametrize("field", ["run_id", "case_id", "plan_id", "policy_sha256", "snapshot_sha256", "receipt_sha256"])
def test_reconcile_rejects_wrong_or_missing_bound_receipt(tmp_path, monkeypatch, field):
    executor = _executor(tmp_path, monkeypatch)
    record = {"execution_validated": True, "run_id": "local-" + "b" * 32,
              "case_id": "b" * 32, "plan_id": SHA, "policy_sha256": SHA,
              "snapshot_sha256": SHA2, "receipt_sha256": "c" * 64,
              "readback_sha256": "d" * 64, "verifier": {"status": "passed"}}
    record[field] = "wrong"
    monkeypatch.setattr("forgeflow.product.local.client.reconcile", lambda *a, **k: record)
    with pytest.raises(Exception, match="reconciliation_binding_changed"):
        executor.reconcile({"case_id": "b" * 32, "scenario": "A"},
                           {"plan_id": SHA, "policy_sha256": SHA, "snapshot_sha256": SHA2})


@pytest.mark.parametrize("status", ["failed", "passed", "unknown"])
def test_reconcile_preserves_existing_measured_verifier_without_rerun(tmp_path, monkeypatch, status):
    executor = _executor(tmp_path, monkeypatch)
    monkeypatch.setattr("forgeflow.product.local.client.reconcile", lambda *a, **k: pytest.fail("must not reverify"))
    execution = {"status": "succeeded", "readback": {"execution_validated": True},
                 "verifier": {"status": status}, "writes": []}
    assert executor.reconcile({"case_id": "b" * 32, "scenario": "A", "execution": execution},
                              {"plan_id": SHA}) == execution


def test_lost_verifier_response_is_unknown_and_only_recovers_bound_receipt(tmp_path, monkeypatch):
    from forgeflow.recovery.client import ExecutorError

    executor = _executor(tmp_path, monkeypatch)
    plan = {"plan_id": SHA, "policy_sha256": SHA, "snapshot_sha256": SHA2, "actions": []}
    plan["margin_basis"] = MARGIN_BASIS
    plan["executor_binding"] = _binding("b" * 32, "owner")
    issued = datetime.now(UTC)
    case = {"case_id": "b" * 32, "scenario": "A", "_owner_sha256": owner_scope("owner"), "approval": {
        "nonce": "e" * 32, "plan_id": plan["plan_id"],
        "approver": "owner", "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(seconds=900)).isoformat(),
    }}
    case["approval"].update(phase="dispatching", executor_grant=executor._approval_payload(case, plan))
    monkeypatch.setattr("forgeflow.product.local.client.apply", lambda *a, **k: {"execution_validated": True})

    def lost(*args, **kwargs):
        raise ExecutorError("unavailable")

    monkeypatch.setattr("forgeflow.product.local.client.reconcile", lost)
    execution = executor.apply(case, plan)
    assert execution["status"] == "succeeded"
    assert execution["verifier"]["status"] == "unknown"
    assert execution["verifier"]["error"] == "verification_receipt_unavailable"
    monkeypatch.setattr(executor, "_bound_verification", lambda case, plan: {"bound": True})
    monkeypatch.setattr("forgeflow.product.local.client.apply", lambda *a, **k: pytest.fail("no second apply"))
    assert executor.reconcile({**case, "execution": execution}, plan) == {"bound": True}


def test_real_local_executor_does_not_advertise_synthetic_revision_support(tmp_path, monkeypatch):
    executor = _executor(tmp_path, monkeypatch)
    assert not hasattr(executor, "prepare_revision")


@pytest.mark.parametrize("setting,enabled", [(None, False), ("0", False), ("false", False),
                                           ("no", False), ("1", True), ("true", True), (" YES ", True)])
@pytest.mark.parametrize("env_name", ["FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", "FORGEFLOW_LOCAL_REVISIONS"])
def test_from_env_explicit_revision_setting(tmp_path, monkeypatch, setting, enabled, env_name):
    _executor(tmp_path, monkeypatch)
    values = {"PRODUCT": "1", "SOCKET": "/tmp/unused.sock", "STORE": str(tmp_path / "store"),
              "APPROVALS": str(tmp_path / "approvals"), "REQUEST": str(tmp_path / "request.json"),
              "CONTAINER": "synthetic-sandbox", "SCENARIO": "A", "TASK": "2280_medium_repair_plan_medium"}
    for name, value in values.items():
        monkeypatch.setenv("FORGEFLOW_LOCAL_" + name, value)
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", raising=False)
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISIONS", raising=False)
    if setting is not None:
        monkeypatch.setenv(env_name, setting)
    executor = LocalProductExecutor.from_env()
    assert executor.supports_revisions is enabled


@pytest.mark.parametrize("setting", ["", "enabled", "2", "true-ish"])
def test_acceptance_typo_is_not_silently_enabled_or_disabled(monkeypatch, setting):
    from forgeflow.product.local import revision_acceptance_setting

    monkeypatch.setenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", setting)
    with pytest.raises(ValueError, match="invalid_revision_acceptance_setting"):
        revision_acceptance_setting()


@pytest.mark.parametrize("new,legacy,expected", [(None, None, None), ("true", None, True),
                                                ("false", None, False), ("YES", "1", True),
                                                ("0", "false", False)])
def test_explicit_product_revision_setting_and_alias_agree(monkeypatch, new, legacy, expected):
    from forgeflow.product.local import revision_capability_setting

    for name, value in (("FORGEFLOW_LOCAL_REVISIONS", new), ("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", legacy)):
        monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv(name, value)
    assert revision_capability_setting() is expected


@pytest.mark.parametrize("new,legacy,error", [("1", "0", "conflicting_revision_settings"),
                                              ("0", "true", "conflicting_revision_settings"),
                                              ("enabled", None, "invalid_revision_capability_setting"),
                                              ("", None, "invalid_revision_capability_setting")])
def test_product_revision_setting_rejects_conflict_or_typo(monkeypatch, new, legacy, error):
    from forgeflow.product.local import revision_capability_setting

    monkeypatch.setenv("FORGEFLOW_LOCAL_REVISIONS", new)
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", raising=False)
    if legacy is not None:
        monkeypatch.setenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", legacy)
    with pytest.raises(ValueError, match=error):
        revision_capability_setting()


@pytest.mark.parametrize("setting,enabled", [(None, False), ("0", False), ("false", False),
                                           ("no", False), ("1", True), ("true", True), (" YES ", True)])
def test_from_env_requires_explicit_candidate_opt_in(tmp_path, monkeypatch, setting, enabled):
    _executor(tmp_path, monkeypatch)
    values = {"PRODUCT": "1", "SOCKET": "/tmp/unused.sock", "STORE": str(tmp_path / "store"),
              "APPROVALS": str(tmp_path / "approvals"), "REQUEST": str(tmp_path / "request.json"),
              "CONTAINER": "synthetic-sandbox", "SCENARIO": "A", "TASK": "2280_medium_repair_plan_medium"}
    for name, value in values.items():
        monkeypatch.setenv("FORGEFLOW_LOCAL_" + name, value)
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", raising=False)
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISIONS", raising=False)
    monkeypatch.delenv("FORGEFLOW_LOCAL_CANDIDATES", raising=False)
    if setting is not None:
        monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATES", setting)
    executor = LocalProductExecutor.from_env()
    assert executor.supports_candidates is enabled
    assert executor.supports_revisions is False


@pytest.mark.parametrize("setting", ["", "enabled", "2", "true-ish"])
def test_candidate_setting_typo_is_rejected(monkeypatch, setting):
    from forgeflow.product.local import candidate_capability_setting

    monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATES", setting)
    with pytest.raises(ValueError, match="invalid_candidate_capability_setting"):
        candidate_capability_setting()
