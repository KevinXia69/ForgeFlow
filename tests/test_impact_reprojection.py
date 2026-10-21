"""Impact audits and retrospective copies over invented bound artifact stores."""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest
import test_product_candidate_acceptance as acceptance

from forgeflow.recovery.storage import atomic_json, read_json

system = acceptance.system
candidates = acceptance.candidates
serve = acceptance.serve
workspace = acceptance.workspace
handlers = acceptance.handlers
wired_candidates = acceptance.wired_candidates
auditor = acceptance.auditor
prepared = acceptance.prepared
budget_reprojection = acceptance.budget_reprojection
capture_gate = acceptance.capture_gate


@pytest.fixture
def impact_reprojection(budget_reprojection):
    _, store, original = budget_reprojection
    for row in [original, *original["history"]]:
        for plan in [row["plan"], *row["comparison"]["plans"]]:
            plan.pop("impact_chain", None)
    script = Path(__file__).resolve().parents[1] / "scripts/diagnose_impact_chain.py"
    spec = importlib.util.spec_from_file_location("impact_reprojector", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, store, original


def strip_new_chains(case):
    result = copy.deepcopy(case)
    for row in [result, *result["history"]]:
        for plan in [row["plan"], *row["comparison"]["plans"]]:
            plan.pop("impact_chain", None)
    return result


def test_impact_reprojection_preserves_budget_approval_execution_sources_and_selected_history(impact_reprojection, wired_candidates):
    module, store, original = impact_reprojection
    before = copy.deepcopy(original)
    source_bytes = {path: path.read_bytes() for path in store.rglob("*.json")}
    report = module.reproject_case(store, original)
    assert original == before
    assert source_bytes == {path: path.read_bytes() for path in store.rglob("*.json")}
    assert strip_new_chains(report["replay_case"]) == original
    assert report["bound_sources_unchanged"] is True
    assert any(path.endswith("product-constraints.json") for path in report["bound_sources_sha256"])
    assert any(path.endswith("product-costs.json") for path in report["bound_sources_sha256"])
    for current, row, evidence in zip((True, False), [report["replay_case"], *report["replay_case"]["history"]],
                                     report["comparisons"], strict=True):
        assert evidence["current"] is current
        assert evidence["audit"]["impact_chain_required"] and evidence["audit"]["decision_metrics_required"]
        assert evidence["audit"]["budget_basis_required"] is False  # old budgets are retained, not upgraded
        selected = next(plan for plan in row["comparison"]["plans"] if plan["plan_id"] == row["plan"]["plan_id"])
        assert row["plan"] == selected
        assert all(item["nodes"] > 0 and item["edges"] > 0 for item in evidence["chains"])
        assert all(item["original_chain_recorded"] is False for item in evidence["chains"])
    assert wired_candidates[2][0].writes == []
    second = module.reproject_case(store, report["replay_case"])
    assert second["replay_case"] == report["replay_case"]
    assert all(item["original_chain_recorded"] for row in second["comparisons"] for item in row["chains"])


@pytest.mark.parametrize("current", [False, True])
def test_impact_gate_requires_every_candidate_and_rebuilds_present_fields_even_without_flag(impact_reprojection, auditor, current):
    module, store, original = impact_reprojection
    public = copy.deepcopy(original["comparison"])
    assert auditor.audit_comparison(store, original["case_id"], public, current=current)["candidate_count"] == 2
    with pytest.raises(RuntimeError, match="candidate_impact_chain_required"):
        auditor.audit_comparison(store, original["case_id"], public, current=current, require_impact_chain=True)
    replay = module.reproject_case(store, original)["replay_case"]
    public = replay["comparison"]
    assert auditor.audit_comparison(store, original["case_id"], public, current=current,
                                    require_impact_chain=True)["impact_chain_required"]
    unselected = next(plan for plan in public["plans"] if plan["plan_id"] != public["selected_plan_id"])
    for missing in (None, {}):
        changed = copy.deepcopy(public)
        next(plan for plan in changed["plans"] if plan["plan_id"] == unselected["plan_id"])["impact_chain"] = missing
        with pytest.raises(RuntimeError, match="candidate_impact_chain_required"):
            auditor.audit_comparison(store, original["case_id"], changed, current=current, require_impact_chain=True)
    unselected["impact_chain"]["edges"].pop()
    with pytest.raises(RuntimeError, match="candidate_impact_chain_changed"):
        auditor.audit_comparison(store, original["case_id"], public, current=current)


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("fault", ["snapshot", "policy", "choice", "budget", "readiness", "missing_selected"])
def test_impact_reprojection_refuses_selected_parent_or_other_fact_tampering(impact_reprojection, historical, fault):
    module, store, original = impact_reprojection
    row = original["history"][0] if historical else original
    if fault in {"snapshot", "policy"}:
        row["plan"][fault + "_sha256"] = "a" * 64
    elif fault == "choice":
        row["plan"] = copy.deepcopy(next(plan for plan in row["comparison"]["plans"]
                                         if plan["plan_id"] != row["comparison"]["selected_plan_id"]))
    elif fault == "budget":
        next(check for check in row["plan"]["checks"] if check["code"] == "budget")["actual"] = "$0"
    elif fault == "readiness":
        row["plan"]["order_readiness"]["orders"][0]["buffer_days"] += 1
    else:
        row["plan"] = None
    before = copy.deepcopy(original)
    with pytest.raises(RuntimeError, match="impact_selected_(binding_changed|mirror_changed|plan_missing)"):
        module.reproject_case(store, original)
    assert original == before


@pytest.mark.parametrize("name", ["snapshot", "product-constraints", "product-policy", "product-costs"])
def test_impact_reprojection_refuses_tampered_bound_input_before_copy_is_returned(impact_reprojection, name):
    module, store, original = impact_reprojection
    path = store / original["plan"]["plan_id"] / (name + ".json")
    value = read_json(path)
    value["tampered"] = True
    atomic_json(path, value, replace=True)
    with pytest.raises((RuntimeError, ValueError)):
        module.reproject_case(store, original)
    assert "impact_chain" not in original["plan"]


def test_impact_reprojection_preserves_first_hash_across_history(impact_reprojection, monkeypatch):
    module, store, original = impact_reprojection
    target = store / original["plan"]["plan_id"] / "product-constraints.json"
    original_sha, count = module.sha, 0

    def changed(path):
        nonlocal count
        if path == target:
            count += 1
            return original_sha(path) if count == 1 else "f" * 64
        return original_sha(path)

    monkeypatch.setattr(module, "sha", changed)
    with pytest.raises(RuntimeError, match="impact_sources_changed"):
        module.reproject_case(store, original)


def test_independent_audit_refuses_self_consistent_but_forged_baseline_quantity(impact_reprojection, auditor):
    from forgeflow.product.impact import project_impact_chain

    module, store, original = impact_reprojection
    public = module.reproject_case(store, original)["replay_case"]["comparison"]
    plan = next(plan for plan in public["plans"] if plan["plan_id"] != public["selected_plan_id"])
    chain = plan["impact_chain"]
    operation = next(row for row in chain["source_facts"]["operations"] if row["before"] is not None)
    operation["before"]["quantity"] += 1
    node = next(row for row in chain["nodes"] if row["key"] == "baseline:" + operation["entity_ref"])
    node["quantity"] += 1
    node["facts"]["quantity"] += 1
    # Internal projection arithmetic can agree while disagreeing with original inputs.
    assert project_impact_chain(chain, plan_id=plan["plan_id"], snapshot_sha256=plan["snapshot_sha256"],
                                readiness=plan["order_readiness"]) == chain
    with pytest.raises(RuntimeError, match="candidate_impact_chain_changed"):
        auditor.audit_comparison(store, original["case_id"], public, require_impact_chain=True)


def test_independent_impact_audit_refuses_boolean_integer_substitution(impact_reprojection, auditor):
    module, store, original = impact_reprojection
    public = module.reproject_case(store, original)["replay_case"]["comparison"]
    public["plans"][0]["impact_chain"]["schema_version"] = True
    with pytest.raises(RuntimeError, match="candidate_impact_chain_changed"):
        auditor.audit_comparison(store, original["case_id"], public)


def test_capture_impact_flag_audits_current_history_and_implies_candidates(capture_gate, monkeypatch):
    module, _, calls, out = capture_gate
    monkeypatch.setattr(sys, "argv", [sys.argv[0], "--out", str(out), "--phase", "initial", "--require-impact-chain"])
    module.main()
    assert calls == [{"current": True, "require_decision_metrics": False, "require_budget_basis": False, "require_impact_chain": True},
                     {"current": False, "require_decision_metrics": False, "require_budget_basis": False, "require_impact_chain": True}]
    report = read_json(out / "initial.json")
    assert report["candidate_audits_required"] and report["impact_chain_required"]


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("fault", ["missing", "changed", "numeric_type", "snapshot", "policy", "choice", "missing_selected", "missing_comparison"])
def test_capture_impact_flag_refuses_wrong_selected_mirror(capture_gate, monkeypatch, historical, fault):
    module, case, _, out = capture_gate
    row = case["history"][0] if historical else case
    if fault == "missing":
        row["plan"].pop("impact_chain")
    elif fault == "changed":
        row["plan"]["impact_chain"]["recorded"] = False
    elif fault == "numeric_type":
        row["plan"]["impact_chain"]["recorded"] = 1
    elif fault in {"snapshot", "policy"}:
        row["plan"][fault + "_sha256"] = "d" * 64
    elif fault == "choice":
        row["comparison"]["selected_plan_id"] = "d" * 64
    elif fault == "missing_selected":
        row["plan"] = None
    else:
        row.pop("comparison")
    monkeypatch.setattr(sys, "argv", [sys.argv[0], "--out", str(out), "--phase", "initial", "--require-impact-chain"])
    with pytest.raises(SystemExit, match="selected impact chain missing or different|impact-chain acceptance requires every"):
        module.main()
    assert not (out / "initial.json").exists()


@pytest.mark.parametrize("fault", [None, "checkpoint_changed", "wrong_instance", "existing_output"])
def test_impact_reprojection_cli_keeps_checkpoint_and_does_not_start_services(impact_reprojection, monkeypatch, tmp_path, fault):
    import shutil

    module, store, original = impact_reprojection
    sandbox = tmp_path / "managed"
    shutil.copytree(store, sandbox / "store")
    checkpoint = tmp_path / "checkpoint.json"
    out = tmp_path / "impact.json"
    atomic_json(checkpoint, {"scenario": "B", "task": "invented", "container_id": "invented-container", "cases": [original]})
    before = checkpoint.read_bytes()
    launcher = {"_require_dedicated_revision_sandbox": lambda: None, "_instance": lambda: {"container_id": "invented-container"},
                "_candidate_capability": lambda record: True, "_check_candidate_bundle": lambda *args: None,
                "_candidate_profile": lambda *args: "invented", "SANDBOX": sandbox,
                "SCENARIO": "B", "TASK": "invented"}
    monkeypatch.setattr(module.runpy, "run_path", lambda *args, **kwargs: launcher)
    monkeypatch.setattr(sys, "argv", [str(module.__file__), "--checkpoint", str(checkpoint), "--out", str(out)])
    if fault == "checkpoint_changed":
        real_sha, seen = module.sha, 0

        def changed(path):
            nonlocal seen
            if path == checkpoint:
                seen += 1
                return real_sha(path) if seen == 1 else "f" * 64
            return real_sha(path)

        monkeypatch.setattr(module, "sha", changed)
    elif fault == "wrong_instance":
        launcher["SCENARIO"] = "A"
    elif fault == "existing_output":
        out.write_text("preserve this file")
    if fault:
        with pytest.raises(RuntimeError, match={"checkpoint_changed": "impact_checkpoint_changed",
                "wrong_instance": "impact_wrong_checkpoint", "existing_output": "impact_output_exists"}[fault]):
            module.main()
        assert not out.exists() or out.read_text() == "preserve this file"
    else:
        module.main()
        result = read_json(out)
        assert result["checkpoint_unchanged"] and not result["saved_case_modified"]
        assert not result["recorded_budget_checks_modified"] and not result["erp_or_model_called"]
        assert "not_new_ERP_or_verifier" in result["scope"]
        assert strip_new_chains(result["cases"][0]["replay_case"]) == original
    assert checkpoint.read_bytes() == before
