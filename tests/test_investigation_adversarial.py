"""Adversarial event, admission, deadline, and report binding regressions.

The wrong-offer control uses real CP-SAT and preparation. Other service tests
replace only the preparation boundary and persist real request/manifest files.
"""

import copy
import hashlib

import pytest
from investigation_support import make_case, make_draft, prepared_artifacts
from test_investigation_dev_dataset import observed_draft, service_for
from test_investigation_fixtures import real_bundle as real_bundle
from test_investigation_service import (
    observed_draft as observed_unit_draft,
)
from test_investigation_service import (
    prepare as prepare_unit,
)
from test_investigation_service import (
    setup_service,
)
from test_investigation_service import (
    validate as validate_unit,
)

from forgeflow.investigation.evidence import validate_draft
from forgeflow.investigation.fixtures import development_cases
from forgeflow.investigation.report import render_report
from forgeflow.investigation.service import Limits
from forgeflow.planning.live import LiveInputError
from forgeflow.planning.tool import SolverFailure
from forgeflow.recovery import service as recovery
from forgeflow.recovery.storage import read_json


def event(text):
    return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}


@pytest.mark.parametrize("text", [
    "Supplier A cancelled purchase order PO-21.",
    "Supplier A cancelled purchase order PO-DOES-NOT-EXIST.",
    "Supplier A cancelled supply of product OTHER.",
])
def test_explicit_unmatched_order_or_product_anchor_cannot_be_silently_ignored(text):
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    result = validate_draft(draft, observations, source, contract, event=event(text))
    assert result["valid"] is False, "An explicit contradictory identity must not fall back to the only candidate"
    assert any(issue["field"].startswith("disruption") for issue in result["issues"])


@pytest.mark.parametrize("text", [
    "Supplier A or Supplier B cancelled a supply commitment.",
    "Supplier A cancelled PO-20 and PO-21.",
    "Supplier A did not cancel PO-20.",
    "Supplier A might cancel PO-20.",
    "If Supplier A cancels PO-20, prepare a recovery plan.",
])
def test_nonunique_or_unconfirmed_event_cannot_be_resolved_by_guessing_contract(text):
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    result = validate_draft(draft, observations, source, contract, event=event(text))
    assert result["valid"] is False


def test_real_quote_for_another_same_vendor_purchase_cannot_cancel_the_wrong_commitment(real_bundle, tmp_path):
    case = development_cases()[2]
    connection, service = service_for(case, tmp_path, real_bundle)
    original = connection.fingerprint()
    draft = observed_draft(case, service)
    assert draft["request"]["disruption"]["offer_key"] == "V-OLD|FIN|1|60.0|1"
    wrong = service.dispatch(case.case_id, "query", {
        "kind": "record", "arguments": {"entity": "supplier_offer", "id": 4},
    })["records"][0]
    assert wrong["fields"]["offer_key"] == "V-OLD|FIN|1|55.0|1"
    draft["request"]["disruption"]["offer_key"] = wrong["fields"]["offer_key"]
    next(ref for ref in draft["evidence"] if ref["field"] == "disruption.offer_key")["observation_id"] = wrong["observation_id"]
    checked = service.dispatch(case.case_id, "validate", {"draft": draft})
    if checked["valid"]:
        result = service.dispatch(case.case_id, "prepare", {"draft_id": checked["draft_id"]})
        wrong_targets = None
        if result["prepared"]:
            blueprint = read_json(service.store / result["prepared"]["plan_id"] / "blueprint.json")
            wrong_targets = blueprint["repair_context"]["broken_purchase_order_refs"]
        assert result["status"] != "prepared", (
            "The event names PO-CANCEL, but admitted quotation prepared cancellation of " + str(wrong_targets)
        )
    else:
        assert any(issue["field"].startswith("disruption") for issue in checked["issues"])
    assert connection.fingerprint() == original


def test_omitting_offer_does_not_allow_planner_to_resolve_a_different_purchase(real_bundle, tmp_path):
    case = development_cases()[2]
    connection, service = service_for(case, tmp_path, real_bundle)
    draft = observed_draft(case, service)
    draft["request"]["disruption"].pop("offer_key")
    draft["evidence"] = [ref for ref in draft["evidence"] if ref["field"] != "disruption.offer_key"]
    checked = service.dispatch(case.case_id, "validate", {"draft": draft})
    if checked["valid"]:
        result = service.dispatch(case.case_id, "prepare", {"draft_id": checked["draft_id"]})
        assert result["status"] != "prepared"
    assert not connection.write_attempts


def test_query_that_finishes_past_episode_deadline_does_not_release_new_observations(tmp_path):
    service, source, request = setup_service(tmp_path, limits=Limits(seconds=10))
    now = [1000]
    service.clock = lambda: now[0]
    service.dispatch("episode-1", "status", {})

    class DelayedSource:
        def query(self, kind, arguments):
            result = source.query(kind, arguments)
            now[0] = 1011
            return result

    service.source = DelayedSource()
    result = service.dispatch("episode-1", "query", {"kind": "policy", "arguments": {}})
    assert result["status"] == "budget_exhausted"
    assert result["terminal"]["reason"] == "time_limit"
    assert not result.get("records")


def test_preparation_finishing_past_episode_deadline_is_not_qualified_prepared(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path, limits=Limits(seconds=10))
    now = [1000]
    service.clock = lambda: now[0]
    checked = validate_unit(service, observed_unit_draft(service, source, request))

    def delayed_prepare(connection, actual_request, **kwargs):
        summary = prepared_artifacts(kwargs["store"], actual_request)
        now[0] = 1011
        return summary

    monkeypatch.setattr(recovery, "prepare", delayed_prepare)
    result = prepare_unit(service, checked)
    assert result["status"] == "budget_exhausted"
    assert result["terminal"]["reason"] == "time_limit"
    assert result["prepared"] is None
    assert not (service.root / "review.json").exists()


@pytest.mark.parametrize("kind", ["solver", "recoverable_input"])
def test_late_preparation_exception_cannot_bypass_episode_time_budget(tmp_path, monkeypatch, kind):
    service, source, request = setup_service(tmp_path, limits=Limits(seconds=10))
    now = [1000]
    service.clock = lambda: now[0]
    checked = validate_unit(service, observed_unit_draft(service, source, request))

    def delayed_failure(connection, actual_request, **kwargs):
        now[0] = 1011
        if kind == "solver":
            raise SolverFailure("planning_model_infeasible", {})
        raise LiveInputError("late missing demand", code="unselected_active_demand",
                             details={"missing_sales_order_ids": [31]})

    monkeypatch.setattr(recovery, "prepare", delayed_failure)
    result = prepare_unit(service, checked)
    assert result["status"] == "budget_exhausted"
    assert result["terminal"]["reason"] == "time_limit"
    assert result["prepared"] is None


@pytest.fixture
def prepared_exports(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)

    def prepare_boundary(connection, actual_request, **kwargs):
        return prepared_artifacts(kwargs["store"], actual_request)

    monkeypatch.setattr(recovery, "prepare", prepare_boundary)
    result = prepare_unit(service, validate_unit(service, observed_unit_draft(service, source, request)))
    assert result["status"] == "prepared"
    return read_json(service.root / "state.json"), read_json(service.root / "review.json")


def test_untampered_export_still_renders_bound_review(prepared_exports):
    state, review = prepared_exports
    rendered = render_report(state, review)
    assert review["plan_id"] in rendered
    assert state["event"]["text"] in rendered


@pytest.mark.parametrize("mutation", [
    "event_text", "event_inner_hash", "policy_hash", "contract_hash", "prepared_request_hash",
    "latest_draft_identity", "latest_draft_request", "latest_draft_validity",
])
def test_report_rejects_inconsistent_export_metadata_even_with_original_review_hash(prepared_exports, mutation):
    original, review = prepared_exports
    state = copy.deepcopy(original)
    if mutation == "event_text":
        state["event"]["text"] = "Supplier B cancelled an unrelated purchase."
    elif mutation == "event_inner_hash":
        state["event"]["sha256"] = "0" * 64
    elif mutation == "policy_hash":
        state["policy_sha256"] = "0" * 64
    elif mutation == "contract_hash":
        state["contract_sha256"] = "0" * 64
    elif mutation == "prepared_request_hash":
        state["prepared"]["request_sha256"] = "0" * 64
    elif mutation == "latest_draft_identity":
        state["drafts"][-1]["draft_id"] = "0" * 64
    elif mutation == "latest_draft_request":
        state["drafts"][-1]["draft"]["request"]["margin"] = 0.75
    else:
        state["drafts"][-1]["validation"]["valid"] = False
    with pytest.raises(ValueError, match="binding|event"):
        render_report(state, review)


def test_partial_policy_cannot_be_upgraded_via_a_model_supplied_contract(real_bundle, tmp_path):
    case = development_cases()[4]
    connection, service = service_for(case, tmp_path, real_bundle)
    draft = observed_draft(case, service, invent_budget=True)
    draft["contract"] = development_cases()[0].policy.payload()
    checked = service.dispatch(case.case_id, "validate", {"draft": draft})
    assert checked["valid"] is False
    assert checked["draft_id"] is None
    assert service.contract is None
    assert not connection.write_attempts
