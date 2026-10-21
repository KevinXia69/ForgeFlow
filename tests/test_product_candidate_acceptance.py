"""Candidate acceptance audits use invented ERP, not an actual benchmark pass."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import test_product_candidate_local as local_tests

from forgeflow.product.candidates import project_comparison
from forgeflow.product.service import ProductCaseService
from forgeflow.recovery.storage import atomic_json, read_json

system = local_tests.system
candidates = local_tests.candidates
serve = local_tests.serve
workspace = local_tests.workspace
handlers = local_tests.handlers
wired_candidates = local_tests.wired_candidates


@pytest.fixture
def auditor(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("candidate_auditor", scripts / "product_candidate_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(wired_candidates):
    comparison = local_tests.prepare(wired_candidates)
    executor, case, *_ = wired_candidates
    return executor.store, case.case_id, project_comparison(comparison), comparison


def test_audit_checks_unselected_candidates_without_mutations(auditor, prepared, wired_candidates):
    store, case_id, public, _ = prepared
    before = {p: p.read_bytes() for p in store.rglob("*.json")}
    report = auditor.audit_comparison(store, case_id, public)
    assert report["candidate_count"] == 2 and report["selected_plan_id"] is None
    assert report["distinct_business_effects"] and report["current_root_choice_matches"]
    assert [r["planned"]["total"] for r in report["candidates"]] == ["62.0", "50.0"]
    assert all(r["projection_and_contract_valid"] for r in report["candidates"])
    assert not report["erp_or_mutation_called"]
    assert before == {p: p.read_bytes() for p in store.rglob("*.json")}
    assert wired_candidates[2][0].writes == []


@pytest.mark.parametrize("field", ["selected_plan_id", "version", "objective", "costs", "attempt", "missing_candidate"])
def test_audit_refuses_changed_public_evidence(auditor, prepared, field):
    store, case_id, public, _ = prepared
    public = copy.deepcopy(public)
    if field == "selected_plan_id":
        public[field] = public["plans"][0]["plan_id"]
    elif field == "version":
        public[field] += 1
    elif field == "objective":
        public["plans"][0][field] = "min_planned_cost"
    elif field == "costs":
        public["plans"][0][field]["planned"]["total"] = "1"
    elif field == "attempt":
        public["attempts"][0]["status"] = "infeasible"
    else:
        public["plans"].pop()
    with pytest.raises(RuntimeError):
        auditor.audit_comparison(store, case_id, public)


def test_audit_refuses_stale_choice_but_accepts_confirmed_choice(auditor, prepared, wired_candidates):
    store, case_id, public, comparison = prepared
    executor, case, *_ = wired_candidates
    view = ProductCaseService._executor_view(case)
    intent = executor.selection_intent(view, comparison, comparison["plans"][1]["plan_id"])
    selected = executor.continue_selection(view, comparison, intent)
    with pytest.raises(RuntimeError, match="candidate_current_choice_changed"):
        auditor.audit_comparison(store, case_id, public)
    result = auditor.audit_comparison(store, case_id, project_comparison(selected))
    assert result["selected_plan_id"] == selected["selected_plan_id"]


@pytest.mark.parametrize("name", ["snapshot", "baseline", "plan", "change-set", "product-policy"])
def test_audit_refuses_changed_bound_artifacts(auditor, prepared, name):
    store, case_id, public, _ = prepared
    path = store / public["plans"][1]["plan_id"] / (name + ".json")
    value = read_json(path)
    value["tampered"] = True
    atomic_json(path, value, replace=True)
    with pytest.raises((RuntimeError, ValueError)):
        auditor.audit_comparison(store, case_id, public)


def test_audit_recomputes_order_readiness_from_bound_facts_without_changing_legacy_records(auditor, prepared):
    from forgeflow.product.readiness import build_order_readiness

    store, case_id, public, _ = prepared
    before = {path: path.read_bytes() for path in store.rglob("*.json")}
    for plan in public["plans"]:
        directory = store / plan["plan_id"]
        request, blueprint, record, changes = (read_json(directory / (name + ".json"))
                                               for name in ("request", "blueprint", "plan", "change-set"))
        plan["order_readiness"] = build_order_readiness(request, blueprint, record["plan"], changes,
            plan_id=plan["plan_id"], snapshot_sha256=plan["snapshot_sha256"])
    assert auditor.audit_comparison(store, case_id, public)["candidate_count"] == 2
    assert before == {path: path.read_bytes() for path in store.rglob("*.json")}
    public["plans"][0]["order_readiness"]["orders"][0]["buffer_days"] += 1
    with pytest.raises(RuntimeError, match="candidate_order_readiness_changed"):
        auditor.audit_comparison(store, case_id, public)


def test_audit_rejects_self_consistent_but_forged_margin_floor(auditor, prepared):
    from forgeflow.product.capacity import build_capacity_summary
    from forgeflow.product.decision_metrics import build_decision_metrics
    from forgeflow.product.readiness import build_order_readiness

    store, case_id, public, _ = prepared
    before = {path: path.read_bytes() for path in store.rglob("*.json")}
    for plan in public["plans"]:
        directory = store / plan["plan_id"]
        request, snapshot, blueprint, record, changes = (read_json(directory / (name + ".json"))
            for name in ("request", "snapshot", "blueprint", "plan", "change-set"))
        keys = {"plan_id": plan["plan_id"], "snapshot_sha256": plan["snapshot_sha256"]}
        plan["capacity"] = build_capacity_summary(request, blueprint, changes, **keys)
        plan["order_readiness"] = build_order_readiness(request, blueprint, record["plan"], changes, **keys)
        plan["decision_metrics"] = build_decision_metrics(request, snapshot, blueprint, {}, plan["costs"],
            plan["capacity"], plan["order_readiness"], **keys)
    assert auditor.audit_comparison(store, case_id, public)["candidate_count"] == 2
    assert before == {path: path.read_bytes() for path in store.rglob("*.json")}
    # Every number is internally consistent, but the claimed floor is not bound to this version.
    plan["decision_metrics"] = build_decision_metrics(request, snapshot, blueprint, {"minimum_margin_pct": 30},
        plan["costs"], plan["capacity"], plan["order_readiness"], **keys)
    with pytest.raises(RuntimeError, match="candidate_decision_metrics_changed"):
        auditor.audit_comparison(store, case_id, public)


@pytest.mark.parametrize("current", [True, False])
def test_explicit_decision_gate_checks_every_candidate_and_preserves_legacy_default(auditor, prepared, current):
    from forgeflow.product.capacity import build_capacity_summary
    from forgeflow.product.decision_metrics import build_decision_metrics
    from forgeflow.product.readiness import build_order_readiness

    store, case_id, public, _ = prepared
    before = {path: path.read_bytes() for path in store.rglob("*.json")}
    assert auditor.audit_comparison(store, case_id, public, current=current)["candidate_count"] == 2
    with pytest.raises(RuntimeError, match="candidate_decision_metrics_required"):
        auditor.audit_comparison(store, case_id, public, current=current, require_decision_metrics=True)
    for plan in public["plans"]:
        directory = store / plan["plan_id"]
        request, snapshot, blueprint, record, changes = (read_json(directory / (name + ".json"))
            for name in ("request", "snapshot", "blueprint", "plan", "change-set"))
        keys = {"plan_id": plan["plan_id"], "snapshot_sha256": plan["snapshot_sha256"]}
        plan["capacity"] = build_capacity_summary(request, blueprint, changes, **keys)
        plan["order_readiness"] = build_order_readiness(request, blueprint, record["plan"], changes, **keys)
        plan["decision_metrics"] = build_decision_metrics(request, snapshot, blueprint, {}, plan["costs"],
            plan["capacity"], plan["order_readiness"], **keys)
    report = auditor.audit_comparison(store, case_id, public, current=current, require_decision_metrics=True)
    assert report["decision_metrics_required"] and report["candidate_count"] == 2
    # The second, unselected candidate must not evade the gate; history uses it too.
    for field in ("decision_metrics", "capacity", "order_readiness"):
        for empty in (None, {}):
            changed = copy.deepcopy(public)
            changed["plans"][1][field] = empty
            with pytest.raises(RuntimeError, match="candidate_decision_metrics_required"):
                auditor.audit_comparison(store, case_id, changed, current=current, require_decision_metrics=True)
        changed = copy.deepcopy(public)
        changed["plans"][1].pop(field)
        with pytest.raises(RuntimeError, match="candidate_decision_metrics_required"):
            auditor.audit_comparison(store, case_id, changed, current=current, require_decision_metrics=True)
    changed = copy.deepcopy(public)
    changed["plans"][1]["decision_metrics"]["margin"]["cost_headroom"] = "0"
    with pytest.raises(RuntimeError, match="candidate_decision_metrics_changed"):
        auditor.audit_comparison(store, case_id, changed, current=current, require_decision_metrics=True)
    assert before == {path: path.read_bytes() for path in store.rglob("*.json")}


@pytest.fixture
def capture_gate(monkeypatch, tmp_path):
    """Exercise CLI wiring with no Docker, ERP, model or private runtime reads."""
    import sys
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[1] / "scripts/capture_browser_product_acceptance.py"
    spec = importlib.util.spec_from_file_location("decision_capture", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = {"plan_id": "a" * 64, "policy_sha256": "b" * 64, "snapshot_sha256": "c" * 64,
        "checks": [{"code": "budget", "status": "pass", "actual": 1, "threshold": 1,
                    "basis": {"kind": "sales_order_value_per_customer_cap_v1", "plan_id": "a" * 64,
                              "snapshot_sha256": "c" * 64}}], **{field: {"recorded": True}
            for field in ("decision_metrics", "capacity", "order_readiness", "impact_chain")}}
    row = {"plan": copy.deepcopy(plan), "comparison": {"plans": [copy.deepcopy(plan)], "selected_plan_id": plan["plan_id"]}}
    case = {**copy.deepcopy(row), "case_id": "test-case", "state": "awaiting_approval", "history": [copy.deepcopy(row)]}
    calls = []

    def audit(store, case_id, comparison, **options):
        calls.append(options)
        return {"candidate_count": 1}

    monkeypatch.setitem(sys.modules, "product_candidate_acceptance", SimpleNamespace(audit_comparison=audit))
    launcher = {"_instance": lambda: {}, "_revision_capability": lambda record: True,
        "_candidate_capability": lambda record: True, "_container_info": lambda: {},
        "_check_container": lambda info, record: {"id": "test-container"},
        "CONTAINER": "test-container", "SANDBOX": tmp_path, "PORT": 1, "INVITE": "invented",
        "SCENARIO": "A", "TASK": "invented"}
    monkeypatch.setattr(module.runpy, "run_path", lambda *args, **kwargs: launcher)
    monkeypatch.setattr(module, "sources", lambda: {"invented.py": "digest"})

    def snapshot(argv, **options):
        atomic_json(Path(argv[argv.index("--out") + 1]), {"tables": {}})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", snapshot)

    class Http:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=201)

        def get(self, path):
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: [{"case_id": "test-case"}] if path == "/api/product/cases" else case)

    monkeypatch.setattr(module.httpx, "Client", Http)
    monkeypatch.setattr(sys, "argv", [str(script), "--out", str(tmp_path), "--phase", "initial", "--require-decision-metrics"])
    return module, case, calls, tmp_path


def test_capture_decision_flag_implies_candidate_audit_for_current_and_history(capture_gate):
    module, _, calls, out = capture_gate
    module.main()
    assert calls == [{"current": True, "require_decision_metrics": True, "require_budget_basis": False, "require_impact_chain": False},
                     {"current": False, "require_decision_metrics": True, "require_budget_basis": False, "require_impact_chain": False}]
    report = read_json(out / "initial.json")
    assert report["candidate_audits_required"] and report["decision_metrics_required"]


@pytest.mark.parametrize("historical", [False, True])
def test_capture_decision_gate_rejects_selected_copy_missing_or_changed(capture_gate, historical):
    module, case, _, out = capture_gate
    row = case["history"][0] if historical else case
    row["plan"]["decision_metrics"] = {"recorded": False}
    with pytest.raises(SystemExit, match="selected decision facts missing or different"):
        module.main()
    assert not (out / "initial.json").exists()


def test_budget_basis_gate_covers_unselected_and_legacy_candidates(auditor, prepared):
    store, case_id, public, _ = prepared
    before = {path: path.read_bytes() for path in store.rglob('*.json')}
    report = auditor.audit_comparison(store, case_id, public, require_budget_basis=True)
    assert report['candidate_count'] == 2 and report['budget_basis_required']
    assert before == {path: path.read_bytes() for path in store.rglob('*.json')}
    budget = next(c for c in public['plans'][1]['checks'] if c['code'] == 'budget')
    budget.pop('basis')
    assert auditor.audit_comparison(store, case_id, public)['candidate_count'] == 2
    with pytest.raises(RuntimeError, match='candidate_budget_basis_required'):
        auditor.audit_comparison(store, case_id, public, require_budget_basis=True)


def test_budget_audit_rejects_self_consistent_counts_not_matching_bound_orders(auditor, prepared):
    store, case_id, public, _ = prepared
    budget = next(c for c in public['plans'][1]['checks'] if c['code'] == 'budget')
    budget['actual'] += 1
    budget['threshold'] += 1
    with pytest.raises(RuntimeError, match='candidate_budget_check_changed'):
        auditor.audit_comparison(store, case_id, public, require_budget_basis=True)


def test_capture_budget_flag_implies_audit_for_current_and_history(capture_gate, monkeypatch):
    import sys

    module, _, calls, out = capture_gate
    monkeypatch.setattr(sys, "argv", [sys.argv[0], "--out", str(out), "--phase", "initial", "--require-budget-basis"])
    module.main()
    assert calls == [{"current": True, "require_decision_metrics": False, "require_budget_basis": True, "require_impact_chain": False},
                     {"current": False, "require_decision_metrics": False, "require_budget_basis": True, "require_impact_chain": False}]
    report = read_json(out / "initial.json")
    assert report["candidate_audits_required"] and report["budget_basis_required"]


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("fault", ["missing_basis", "duplicate", "count", "snapshot", "policy", "choice"])
def test_capture_budget_flag_refuses_changed_selected_mirror(capture_gate, monkeypatch, historical, fault):
    import sys

    module, case, _, out = capture_gate
    row = case["history"][0] if historical else case
    budget = row["plan"]["checks"][0]
    if fault == "missing_basis":
        budget.pop("basis")
    elif fault == "duplicate":
        row["plan"]["checks"].append(copy.deepcopy(budget))
    elif fault == "count":
        budget["actual"] = 0
    elif fault in {"snapshot", "policy"}:
        row["plan"][fault + "_sha256"] = "d" * 64
    else:
        row["comparison"]["selected_plan_id"] = "d" * 64
    monkeypatch.setattr(sys, "argv", [sys.argv[0], "--out", str(out), "--phase", "initial", "--require-budget-basis"])
    with pytest.raises(SystemExit, match="selected budget check missing or different"):
        module.main()
    assert not (out / "initial.json").exists()


@pytest.fixture
def budget_reprojection(auditor, prepared, wired_candidates):
    """Real immutable artifact checks with invented ERP and a read-only replay."""
    from forgeflow.product.capacity import build_capacity_summary
    from forgeflow.product.decision_metrics import build_decision_metrics
    from forgeflow.product.readiness import build_order_readiness

    store, case_id, _, comparison = prepared
    executor, case, *_ = wired_candidates
    view = ProductCaseService._executor_view(case)
    intent = executor.selection_intent(view, comparison, comparison["plans"][1]["plan_id"])
    public = project_comparison(executor.continue_selection(view, comparison, intent))
    for plan in public["plans"]:
        directory = store / plan["plan_id"]
        request, snapshot, blueprint, record, changes = (read_json(directory / (name + ".json"))
            for name in ("request", "snapshot", "blueprint", "plan", "change-set"))
        keys = {"plan_id": plan["plan_id"], "snapshot_sha256": plan["snapshot_sha256"]}
        plan["capacity"] = build_capacity_summary(request, blueprint, changes, **keys)
        plan["order_readiness"] = build_order_readiness(request, blueprint, record["plan"], changes, **keys)
        plan["decision_metrics"] = build_decision_metrics(request, snapshot, blueprint, {}, plan["costs"],
            plan["capacity"], plan["order_readiness"], **keys)
        budget = next(row for row in plan["checks"] if row["code"] == "budget")
        budget.pop("basis")
        budget.update(label="Planned cost versus customer budget caps", actual="$50.00", threshold="$2,000.00",
                      source="solver planned cost compared with summed sales-order customer budget caps; per-order caps remain policy checks, not purchase authorization")
    selected = next(plan for plan in public["plans"] if plan["plan_id"] == public["selected_plan_id"])
    row = {"plan": copy.deepcopy(selected), "comparison": copy.deepcopy(public)}
    original = {**copy.deepcopy(row), "case_id": case_id, "history": [copy.deepcopy(row)],
                "approval": {"plan_id": selected["plan_id"], "comment": "preserve recorded approval"},
                "execution": {"status": "synthetic-recorded-result"}}
    script = Path(__file__).resolve().parents[1] / "scripts/diagnose_budget_checks.py"
    spec = importlib.util.spec_from_file_location("budget_reprojector", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, store, original


def test_budget_reprojection_keeps_bound_sources_and_saved_case_unchanged(budget_reprojection, wired_candidates):
    module, store, original = budget_reprojection
    before = copy.deepcopy(original)
    source_bytes = {path: path.read_bytes() for path in store.rglob("*.json")}
    result = module.reproject_case(store, original)
    assert original == before and source_bytes == {path: path.read_bytes() for path in store.rglob("*.json")}
    assert result["bound_sources_unchanged"] is True and result["bound_sources_sha256"]
    assert [row["current"] for row in result["comparisons"]] == [True, False]
    replay = result["replay_case"]
    assert replay["approval"] == original["approval"] and replay["execution"] == original["execution"]
    for row, report in zip([replay, *replay["history"]], result["comparisons"], strict=True):
        assert report["audit"]["budget_basis_required"] and report["audit"]["decision_metrics_required"]
        selected = next(plan for plan in row["comparison"]["plans"] if plan["plan_id"] == row["plan"]["plan_id"])
        assert row["plan"] == selected
        for plan in row["comparison"]["plans"]:
            budget = next(check for check in plan["checks"] if check["code"] == "budget")
            assert budget["basis"]["plan_id"] == plan["plan_id"] and budget["actual"] == budget["threshold"] == 1
        assert all("basis" not in check["original_check"] for check in report["checks"])
    assert wired_candidates[2][0].writes == []


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("fault", ["snapshot", "policy", "choice", "budget", "duplicate"])
def test_budget_reprojection_refuses_tampered_selected_mirror(budget_reprojection, historical, fault):
    module, store, original = budget_reprojection
    row = original["history"][0] if historical else original
    budget = next(check for check in row["plan"]["checks"] if check["code"] == "budget")
    if fault in {"snapshot", "policy"}:
        row["plan"][fault + "_sha256"] = "a" * 64
    elif fault == "choice":
        other = next(plan for plan in row["comparison"]["plans"] if plan["plan_id"] != row["plan"]["plan_id"])
        row["plan"] = copy.deepcopy(other)
    elif fault == "budget":
        budget["actual"] = "$0.00"
    else:
        row["plan"]["checks"].append(copy.deepcopy(budget))
    before = copy.deepcopy(original)
    with pytest.raises(RuntimeError, match="budget_selected_(binding_changed|check_changed|check_missing)"):
        module.reproject_case(store, original)
    assert original == before


def test_budget_reprojection_detects_source_changed_between_repeated_history_paths(budget_reprojection, monkeypatch):
    module, store, original = budget_reprojection
    real_sha = module.sha
    target = store / original["plan"]["plan_id"] / "manifest.json"
    seen = 0

    def changed(path):
        nonlocal seen
        if path == target:
            seen += 1
            # After current evidence was read, the same historical path differs.
            return real_sha(path) if seen == 1 else "f" * 64
        return real_sha(path)

    monkeypatch.setattr(module, "sha", changed)
    with pytest.raises(RuntimeError, match="budget_sources_changed"):
        module.reproject_case(store, original)
