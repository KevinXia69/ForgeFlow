"""Acceptance auditing uses invented snapshots; never a real ERP pass."""
import copy
import importlib.util
from pathlib import Path

import pytest
import test_product_versions as root_tests

from forgeflow.recovery import service
from forgeflow.recovery.storage import read_json

system = root_tests.system


@pytest.fixture
def auditor():
    spec = importlib.util.spec_from_file_location("cost_auditor", Path(__file__).resolve().parents[1]
                                               / "scripts/product_cost_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(system, monkeypatch):
    collector = service.collect_snapshot

    def snapshot(*args):
        result = copy.deepcopy(collector(*args))
        result["collected_at_utc"] = "2026-01-01T10:00:00+00:00"
        return result

    monkeypatch.setattr(service, "collect_snapshot", snapshot)
    decision = root_tests.prepare(system)
    store = system[2]["store"]
    plan_id = decision["selected_plan_id"]
    report = read_json(store / plan_id / "product-costs.json")
    return store, {"plan_id": plan_id, "snapshot_sha256": report["basis"]["snapshot_sha256"],
                   "new_spend": 70, "costs": report}


def test_independent_audit_reprices_snapshot_and_projected_documents(auditor, prepared, system):
    report = auditor.audit_plan_costs(*prepared)
    assert report["status"] == "independent_cost_audit_passed"
    assert report["baseline"]["total"] == "50" and report["planned"]["total"] == "70"
    assert report["operation_counts"] == {"keep": 1, "update": 1, "cancel": 1, "create": 1}
    assert report["known_delta"] == "20" and not report["actual_financials_verified"]
    assert not system[0].writes


@pytest.mark.parametrize("field", ["new_spend", "costs", "snapshot_sha256", "plan_id"])
def test_api_report_must_match_original_binding(auditor, prepared, field):
    store, plan = prepared
    if field == "new_spend":
        plan[field] = 100
    elif field == "costs":
        plan[field]["planned"]["total"] = "100"
    else:
        plan[field] = "f" * 64
    with pytest.raises((RuntimeError, ValueError, FileNotFoundError)):
        auditor.audit_plan_costs(store, plan)


def test_correctly_signed_but_wrong_rate_does_not_fool_independent_audit(auditor, prepared, monkeypatch):
    store, plan = prepared
    original = auditor._metadata

    def wrong(center):
        capacity, metadata = original(center)
        if center["code"] == "WC-A":
            metadata["FIN"]["unit_cost"] = 999
        return capacity, metadata

    # The report remains internally coherent; independently observed original
    # center costs disagree. The audit must not merely trust report arithmetic.
    monkeypatch.setattr(auditor, "_metadata", wrong)
    with pytest.raises(RuntimeError, match="cost_acceptance_mismatch"):
        auditor.audit_plan_costs(store, plan)
