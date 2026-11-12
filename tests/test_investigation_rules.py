"""A deterministic baseline derives requests only through the public tool API."""

from dataclasses import replace

import pytest
from test_investigation_dev_dataset import service_for
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.contract import contract_from_payload
from forgeflow.investigation.fixtures import development_cases, supplier_cancellation_control
from forgeflow.investigation.rules import run_rules
from forgeflow.investigation.service import Limits
from forgeflow.recovery.storage import read_json


@pytest.mark.parametrize("case", development_cases(), ids=lambda case: case.case_id)
def test_rules_use_event_and_public_tools_for_real_development_outcomes(case, real_bundle, tmp_path):
    connection, service = service_for(case, tmp_path, real_bundle)
    before = connection.fingerprint()
    result = run_rules(case.event, lambda action, payload: service.dispatch(case.case_id, action, payload))
    assert result["method"] == "rules-v2"
    assert result["result"]["status"] == case.oracle["expected_status"], result
    assert result["trace"]
    assert all(entry["action"] in {"query", "validate", "prepare", "finish"} for entry in result["trace"])
    assert connection.fingerprint() == before
    assert connection.write_attempts == []
    state = read_json(service.root / "state.json")
    assert state["usage"]["queries"] <= state["limits"]["queries"]
    if case.oracle["expected_status"] == "prepared":
        review = read_json(service.root / "review.json")
        assert review["bindings_verified"] is True
        assert state["usage"]["prepares"] == 1
        assert not any(step["action"] == "finish" for step in result["trace"])
    elif case.case_id == "dev-05-missing-budget":
        assert "budget" in result["result"]["terminal"]["question"].lower()
        assert "SO-1" in result["result"]["terminal"]["question"]
        assert state["usage"]["prepares"] == 0
    elif case.case_id == "dev-06-ambiguous-event":
        question = result["result"]["terminal"]["question"]
        assert "PO-CANCEL" in question and "PO-OTHER" in question
        assert state["usage"]["prepares"] == 0


def test_rules_do_not_require_a_known_supplier_or_purchase_name(real_bundle, tmp_path):
    case = supplier_cancellation_control()
    next(row for row in case.records["res.partner"] if row["id"] == 3)["name"] = "Orion Parts"
    case.records["purchase.order"][0]["name"] = "PO-729"
    policy = case.policy.payload()
    policy["disruption"]["vendor_name"] = "Orion Parts"
    case = replace(case, event="Orion Parts cancelled purchase order PO-729.",
                   policy=contract_from_payload(policy))
    connection, service = service_for(case, tmp_path, real_bundle)
    result = run_rules(case.event, lambda action, payload: service.dispatch(case.case_id, action, payload))
    assert result["result"]["status"] == "prepared", result
    names = [step["payload"]["arguments"].get("query") for step in result["trace"] if step["action"] == "query"]
    assert "Orion Parts" in names and "PO-729" in names
    assert not connection.write_attempts


def test_rules_obey_the_same_episode_query_limit(real_bundle, tmp_path):
    case = supplier_cancellation_control()
    connection, service = service_for(case, tmp_path, real_bundle)
    service.limits = Limits(queries=1)
    result = run_rules(case.event, lambda action, payload: service.dispatch(case.case_id, action, payload))
    assert result["result"]["status"] == "budget_exhausted"
    assert result["result"]["usage"]["queries"] == 1
    assert result["result"]["usage"]["prepares"] == 0
    assert not connection.write_attempts


def test_unrecognized_event_is_recorded_as_rule_failure_without_guessing():
    calls = []

    def dispatch(action, payload):
        calls.append((action, payload))
        assert action == "finish"
        return {"status": payload["status"], "terminal": {"reason": payload["reason"]}, "prepared": None}

    result = run_rules("Something happened to the parts.", dispatch)
    assert result["result"]["status"] == "technical_failure"
    assert result["result"]["terminal"]["reason"] == "rule_event_pattern_unrecognized"
    assert len(calls) == 1


def test_dispatch_failure_is_preserved_without_echoing_private_exception_text():
    def unavailable(action, payload):
        raise RuntimeError("private backend diagnostic")

    result = run_rules("Orion Parts cancelled PO-729.", unavailable)
    assert result["result"]["status"] == "technical_failure"
    assert [entry["action"] for entry in result["trace"]] == ["query", "finish"]
    assert all(entry["error_type"] == "RuntimeError" for entry in result["trace"])
    assert "private backend diagnostic" not in str(result)
