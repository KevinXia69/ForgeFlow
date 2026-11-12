"""A real solved plan drives the default card, never model prose."""
import copy
import subprocess
import sys

import pytest
from test_investigation_dev_dataset import observed_draft, service_for
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.investigation.fixtures import development_cases
from forgeflow.investigation.report import PLAN_COST_LABEL, render_report, trusted_summary
from forgeflow.recovery.storage import atomic_json, read_json


@pytest.fixture
def real_prepared(tmp_path, real_bundle):
    case = development_cases()[0]
    connection, service = service_for(case, tmp_path, real_bundle)
    draft = observed_draft(case, service)
    draft["reason"] = "Solver chose V-NEW, ten units at 65, total 650; already executed."
    checked = service.dispatch(case.case_id, "validate", {"draft": draft})
    prepared = service.dispatch(case.case_id, "prepare", {"draft_id": checked["draft_id"]})
    assert prepared["status"] == "prepared"
    assert not connection.write_attempts
    return service, read_json(service.root / "state.json"), read_json(service.root / "review.json")


def test_trusted_card_uses_all_bound_artifacts_despite_false_model_explanation(real_prepared):
    service, state, review = real_prepared
    card = trusted_summary(state, review, store=service.store)
    assert card["plan_cost"]["value"] == 60
    assert card["plan_cost"]["label"] == PLAN_COST_LABEL
    assert card["plan_cost"]["incremental_cash"] is None
    assert card["plan_cost"]["legacy_field"] == "expected_new_spend"
    assert card["phase"] == "plan_awaiting_approval"
    assert card["execution_validated"] is False
    assert card["approval_required"] is True
    assert card["model_explanation_authoritative"] is False
    assert card["orders"] == [{"id": 1, "budget_cap": 2000}]
    plan = read_json(service.store / card["plan_id"] / "plan.json")["plan"]
    assert card["purchase_orders"] == plan["purchase_orders"]
    assert len(card["artifact_sha256"]) == 7
    report = render_report(state, review, store=service.store)
    assert f"{PLAN_COST_LABEL}：60" in report
    assert "预计新增支出" not in report
    assert "未经规划器验证" in report
    assert "方案待审批" in report


@pytest.mark.parametrize("artifact", ["request", "snapshot", "blueprint", "plan", "change-set", "baseline", "binding"])
def test_card_rejects_any_changed_bound_artifact(real_prepared, artifact):
    service, state, review = real_prepared
    path = service.store / review["plan_id"] / (artifact + ".json")
    value = read_json(path)
    value["tampered"] = True
    atomic_json(path, value, replace=True)
    with pytest.raises(ValueError, match="binding_mismatch"):
        trusted_summary(state, review, store=service.store)


def test_prepared_card_does_not_fall_back_to_review_without_artifacts(real_prepared):
    _, state, review = real_prepared
    with pytest.raises(ValueError, match="prepared_artifacts_required"):
        trusted_summary(state, review)
    forged = copy.deepcopy(review)
    forged["summary"]["expected_new_spend"] = 650
    with pytest.raises(ValueError, match="review_binding_mismatch"):
        trusted_summary(state, forged)


def test_host_default_export_is_root_only_and_separate_from_model_logs():
    from forgeflow.agents.pi_nebius import (
        _build_investigation_result_command,
        _build_log_export_command,
    )
    command = _build_investigation_result_command()
    assert "os.geteuid() == 0" in command
    assert "trusted_summary(state, review, store=store)" in command
    assert "0o400" in command
    assert "pi.txt" not in command
    exported = _build_log_export_command()
    assert "terminal-control.json" in exported
    assert "trusted-summary.json" in exported
    assert "root_read_only=True" in exported


def test_extracted_bundle_imports_service_client_transport_and_report_in_isolation(real_bundle):
    result = subprocess.run([sys.executable, "-I", "-B", "-c", """
import pathlib,sys
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root))
from forgeflow.investigation import service,client,transport,report
for module in (service,client,transport,report):
    assert pathlib.Path(module.__file__).is_relative_to(root)
""", str(real_bundle)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
