"""Diagnostic summaries omit contact/free-text fields and cannot accept references."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
from test_product_candidates import two_route_facts

from forgeflow.recovery.product_constraints import constrained_blueprint


@pytest.fixture
def diagnosis(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("candidate_diagnosis", scripts / "diagnose_product_candidates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_business_summary_is_read_only_and_excludes_contacts_and_free_text(diagnosis):
    request, snapshot = two_route_facts()
    blueprint = constrained_blueprint(snapshot, request)
    for key in ("customers", "vendors", "workcenters"):
        for row in blueprint[key]:
            row.update(name="PRIVATE_CANARY", email="PRIVATE_CANARY", note="PRIVATE_CANARY",
                       description="PRIVATE_CANARY", owner="PRIVATE_CANARY")
    before = copy.deepcopy(blueprint)
    result = diagnosis.summarize_blueprint(blueprint)
    assert blueprint == before
    assert "PRIVATE_CANARY" not in json.dumps(result)
    assert result["manufacturing"][0]["choices"][-1]["unit_cost_dollars"] == 1
    assert {row["workcenter_code"] for row in result["manufacturing"][0]["choices"]} == {"WC-A", "WC-B", "WC-C"}
    assert result["existing_manufacturing"] and result["existing_purchase"] and result["orders"]


def test_diagnosis_never_accepts_reference_plan_inputs(diagnosis):
    request, snapshot = two_route_facts()
    blueprint = constrained_blueprint(snapshot, request)
    blueprint["repair_context"]["baseline_plan"] = {"do_not_read": "PRIVATE_CANARY"}
    with pytest.raises(RuntimeError, match="reference_plan_not_allowed"):
        diagnosis.summarize_blueprint(blueprint)


def test_diagnosis_rejects_nested_or_excessive_output_values(diagnosis):
    with pytest.raises(RuntimeError, match="invalid_scalar"):
        diagnosis.fields({"code": {"unexpected": "PRIVATE_CANARY"}}, ("code",))
    with pytest.raises(RuntimeError, match="invalid_rows"):
        diagnosis.rows([{}] * 501, ("code",))
