"""Admission, persistent budgets, retries, and actual prepared-request bindings."""

import copy
import json

import pytest
from investigation_support import EVENT, make_case, make_draft, prepared_artifacts, source_record

from forgeflow.investigation.service import InvestigationError, InvestigationService, Limits
from forgeflow.planning.live import LiveInputError
from forgeflow.planning.tool import SolverFailure
from forgeflow.recovery import service as recovery
from forgeflow.recovery.storage import atomic_json, digest, read_json

RUN_ID = "episode-1"


def setup_service(tmp_path, *, limits=None):
    source, contract, request = make_case()
    service = InvestigationService(
        event=EVENT,        source=source, contract=contract, store=tmp_path / "recovery", bundle=tmp_path / "bundle",
        connection_factory=lambda: object(), limits=limits, clock=lambda: 1000,
    )
    return service, source, request


def observed_draft(service, source, request):
    return make_draft(source, request, query=lambda kind, arguments: service.dispatch(
        RUN_ID, "query", {"kind": kind, "arguments": arguments}))[0]


def validate(service, draft):
    return service.dispatch(RUN_ID, "validate", {"draft": draft})


def prepare(service, validated):
    return service.dispatch(RUN_ID, "prepare", {"draft_id": validated["draft_id"]})


def state(service):
    return read_json(service.root / "state.json")


def forbidden_prepare(*args, **kwargs):
    pytest.fail("recovery.prepare must not receive an unvalidated or stale request")


def test_success_persists_exact_validated_request_and_evidence_without_execution_claims(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)
    calls = []

    def real_boundary(connection, actual_request, **kwargs):
        calls.append(copy.deepcopy(actual_request))
        assert kwargs["contract"] is service.contract
        return prepared_artifacts(kwargs["store"], actual_request)

    monkeypatch.setattr(recovery, "prepare", real_boundary)
    draft = observed_draft(service, source, request)
    checked = validate(service, draft)
    assert checked["valid"] is True
    result = prepare(service, checked)
    assert result["status"] == "prepared"
    assert result["usage"]["prepares"] == 1
    assert calls == [request]
    review = read_json(service.root / "review.json")
    assert review["request"] == request
    assert review["request_sha256"] == digest(request)
    assert review["draft_id"] == checked["draft_id"]
    assert review["draft_revision"] == 1
    assert review["odoo_writes"] is False
    assert review["execution_validated"] is False
    assert review["benchmark_success"] is None
    assert set(review["evidence"]) == {
        ref["observation_id"] for ref in draft["evidence"]
    } | {draft["supply_observation_id"]}
    assert result["prepared"]["review_sha256"] == digest(review)
    replayed = prepare(service, checked)
    assert replayed["prepared"] == result["prepared"]
    assert replayed["status"] == "prepared"
    assert replayed["usage"] == result["usage"]
    assert len(calls) == 1


def test_no_evidence_or_model_reported_success_cannot_reach_prepared(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)
    monkeypatch.setattr(recovery, "prepare", forbidden_prepare)
    checked = validate(service, {"request": request, "evidence": [], "reason": "trust me"})
    assert checked["valid"] is False
    assert checked["draft_id"] is None
    with pytest.raises(InvestigationError, match="validated_draft_required"):
        service.dispatch(RUN_ID, "prepare", {"draft_id": "0" * 64})
    with pytest.raises(InvestigationError, match="invalid_investigation_request"):
        service.dispatch(RUN_ID, "finish", {"status": "prepared", "reason": "I solved it", "question": ""})
    assert not (service.root / "review.json").exists()
    assert state(service)["usage"]["prepares"] == 0


@pytest.mark.parametrize("newer_valid", [True, False])
def test_only_the_latest_validated_draft_can_be_prepared(tmp_path, monkeypatch, newer_valid):
    service, source, request = setup_service(tmp_path)
    monkeypatch.setattr(recovery, "prepare", forbidden_prepare)
    draft = observed_draft(service, source, request)
    first = validate(service, draft)
    second_draft = copy.deepcopy(draft)
    second_draft["reason"] = "new revision"
    if not newer_valid:
        second_draft["evidence"] = []
    second = validate(service, second_draft)
    assert second["valid"] is newer_valid
    assert second["draft_revision"] == 2
    with pytest.raises(InvestigationError, match="validated_draft_required"):
        prepare(service, first)
    assert state(service)["usage"]["prepares"] == 0


def test_changed_facts_between_validation_and_prepare_prevent_planner_call(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)
    monkeypatch.setattr(recovery, "prepare", forbidden_prepare)
    checked = validate(service, observed_draft(service, source, request))
    source_record(source, "purchase_order", 20)["fields"]["supplier_id"] = "2"
    result = prepare(service, checked)
    assert result["valid"] is False
    assert result["status"] == "investigating"
    assert result["usage"]["prepares"] == 0
    assert "supply_binding_changed" in {issue["code"] for issue in result["issues"]}


def test_changes_during_prepare_cannot_be_presented_as_a_fresh_review(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)

    def changed_boundary(connection, actual_request, **kwargs):
        summary = prepared_artifacts(kwargs["store"], actual_request)
        source_record(source, "sales_order", 30)["fields"]["commitment_date"] = "2026-01-09 12:00:00"
        return summary

    monkeypatch.setattr(recovery, "prepare", changed_boundary)
    checked = validate(service, observed_draft(service, source, request))
    result = prepare(service, checked)
    assert result["status"] == "investigating"
    assert result["prepared"] is None
    assert state(service)["attempts"][-1]["result"] == "facts_changed_during_prepare"
    assert not (service.root / "review.json").exists()


@pytest.mark.parametrize("entity,identity,field,value", [
    ("sales_order_line", 300, "quantity", 11),
    ("purchase_order_line", 200, "unit_price", 61),
])
def test_relationship_quantity_or_price_changes_during_prepare_invalidate_review(
    tmp_path, monkeypatch, entity, identity, field, value,
):
    service, source, request = setup_service(tmp_path)

    def changed_boundary(connection, actual_request, **kwargs):
        summary = prepared_artifacts(kwargs["store"], actual_request)
        source_record(source, entity, identity)["fields"][field] = value
        return summary

    monkeypatch.setattr(recovery, "prepare", changed_boundary)
    result = prepare(service, validate(service, observed_draft(service, source, request)))
    assert result["status"] == "investigating"
    assert result["prepared"] is None
    assert state(service)["attempts"][-1]["result"] == "facts_changed_during_prepare"
    assert not (service.root / "review.json").exists()


@pytest.mark.parametrize("mismatch", ["request", "manifest_request", "manifest_identity"])
def test_prepared_request_and_manifest_must_match_actual_admitted_request(tmp_path, monkeypatch, mismatch):
    service, source, request = setup_service(tmp_path)

    def changed_boundary(connection, actual_request, **kwargs):
        wrong = {**actual_request, "margin": 0.5}
        params = {"actual_request": wrong} if mismatch == "request" else (
            {"manifest_request": wrong} if mismatch == "manifest_request" else {})
        summary = prepared_artifacts(kwargs["store"], actual_request, **params)
        if mismatch == "manifest_identity":
            path = kwargs["store"] / summary["plan_id"] / "manifest.json"
            manifest = read_json(path)
            manifest["tampered"] = True
            atomic_json(path, manifest, replace=True)
        return summary

    monkeypatch.setattr(recovery, "prepare", changed_boundary)
    result = prepare(service, validate(service, observed_draft(service, source, request)))
    assert result["status"] == "technical_failure"
    assert result["prepared"] is None
    assert not (service.root / "review.json").exists()
    assert state(service)["attempts"][-1]["result"] == "prepared_request_binding_changed"


@pytest.mark.parametrize("presentation", ["same", "reverse_evidence", "new_reason", "reread", "cosmetic_name"])
def test_retry_without_new_semantic_evidence_is_stopped(tmp_path, monkeypatch, presentation):
    service, source, request = setup_service(tmp_path)
    calls = []

    def ambiguous(connection, actual_request, **kwargs):
        calls.append(copy.deepcopy(actual_request))
        raise LiveInputError("private solver prose", code="broken_offer_ambiguous")

    monkeypatch.setattr(recovery, "prepare", ambiguous)
    draft = observed_draft(service, source, request)
    first = prepare(service, validate(service, draft))
    assert first["status"] == "investigating"
    revised = copy.deepcopy(draft)
    if presentation == "reverse_evidence":
        revised["evidence"].reverse()
    elif presentation == "new_reason":
        revised["reason"] = "different prose is not new evidence"
    elif presentation in {"reread", "cosmetic_name"}:
        if presentation == "cosmetic_name":
            source_record(source, "sales_order", 30)["fields"]["name"] = "Renamed Sales Order"
        revised = observed_draft(service, source, request)
    checked = validate(service, revised)
    assert checked["valid"] is True
    result = prepare(service, checked)
    assert result["status"] == "technical_failure"
    assert result["terminal"]["reason"] == "repeated_failure_without_new_evidence"
    assert result["usage"]["prepares"] == 1
    assert len(calls) == 1


def test_new_critical_fact_observation_allows_a_bounded_new_attempt(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)
    calls = []

    def boundary(connection, actual_request, **kwargs):
        calls.append(copy.deepcopy(actual_request))
        if len(calls) == 1:
            raise LiveInputError("offer ambiguous", code="broken_offer_ambiguous")
        return prepared_artifacts(kwargs["store"], actual_request)

    monkeypatch.setattr(recovery, "prepare", boundary)
    first = prepare(service, validate(service, observed_draft(service, source, request)))
    assert first["status"] == "investigating"
    source_record(source, "sales_order", 30)["fields"]["commitment_date"] = "2026-01-12 12:00:00"
    second = prepare(service, validate(service, observed_draft(service, source, request)))
    assert second["status"] == "prepared"
    assert second["usage"]["prepares"] == 2
    assert len(calls) == 2


def test_retry_cannot_cycle_back_to_an_earlier_failed_input(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)
    calls = []

    def ambiguous(connection, actual_request, **kwargs):
        calls.append(actual_request)
        raise LiveInputError("offer ambiguous", code="broken_offer_ambiguous")

    monkeypatch.setattr(recovery, "prepare", ambiguous)
    for due in ("2026-01-10 12:00:00", "2026-01-12 12:00:00"):
        source_record(source, "sales_order", 30)["fields"]["commitment_date"] = due
        result = prepare(service, validate(service, observed_draft(service, source, request)))
        assert result["status"] == "investigating"
    source_record(source, "sales_order", 30)["fields"]["commitment_date"] = "2026-01-10 12:00:00"
    result = prepare(service, validate(service, observed_draft(service, source, request)))
    assert result["status"] == "technical_failure"
    assert result["terminal"]["reason"] == "repeated_failure_without_new_evidence"
    assert result["usage"]["prepares"] == 2
    assert len(calls) == 2


def test_prepare_budget_still_applies_when_new_evidence_is_available(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path, limits=Limits(prepares=1))
    calls = []

    def ambiguous(connection, actual_request, **kwargs):
        calls.append(actual_request)
        raise LiveInputError("offer ambiguous", code="broken_offer_ambiguous")

    monkeypatch.setattr(recovery, "prepare", ambiguous)
    first = prepare(service, validate(service, observed_draft(service, source, request)))
    assert first["status"] == "investigating"
    source_record(source, "sales_order", 30)["fields"]["commitment_date"] = "2026-01-12 12:00:00"
    result = prepare(service, validate(service, observed_draft(service, source, request)))
    assert result["status"] == "budget_exhausted"
    assert result["terminal"]["reason"] == "prepares_limit"
    assert result["usage"]["prepares"] == 1
    assert len(calls) == 1


def test_recoverable_missing_order_feedback_contains_only_safe_business_ids(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)

    def missing(connection, actual_request, **kwargs):
        raise LiveInputError("SECRET REFERENCE REQUEST", code="unselected_active_demand", details={
            "missing_sales_order_ids": [31, 31, -1, True, "32", 2**60],
            "reference_request": {"sales_order_ids": [31], "sales_order_budget_caps": {"31": 900}},
        })

    monkeypatch.setattr(recovery, "prepare", missing)
    result = prepare(service, validate(service, observed_draft(service, source, request)))
    assert result["status"] == "investigating"
    assert result["feedback"]["reason"] == "unselected_active_demand"
    assert result["feedback"]["missing_sales_order_ids"] == [31]
    assert "SECRET" not in json.dumps(result)
    assert "reference_request" not in json.dumps(result)
    assert "900" not in json.dumps(result)


def test_change_run_id_or_restart_service_cannot_reset_episode_budget(tmp_path):
    service, source, request = setup_service(tmp_path, limits=Limits(queries=1))
    query = {"kind": "policy", "arguments": {}}
    assert service.dispatch(RUN_ID, "query", query)["usage"]["queries"] == 1
    with pytest.raises(InvestigationError, match="investigation_binding_changed"):
        service.dispatch("episode-2", "query", query)
    restarted = InvestigationService(
        event=EVENT,        source=source, contract=service.contract, store=service.store, bundle=service.bundle,
        connection_factory=lambda: object(), limits=Limits(queries=1), clock=lambda: 1000,
    )
    result = restarted.dispatch(RUN_ID, "query", query)
    assert result["status"] == "budget_exhausted"
    assert result["usage"]["queries"] == 1
    assert result["terminal"]["reason"] == "queries_limit"


def test_change_limits_cannot_reset_or_expand_an_existing_episode(tmp_path):
    service, source, request = setup_service(tmp_path, limits=Limits(queries=1))
    service.dispatch(RUN_ID, "status", {})
    restarted = InvestigationService(
        event=EVENT,        source=source, contract=service.contract, store=service.store, bundle=service.bundle,
        connection_factory=lambda: object(), limits=Limits(queries=100), clock=lambda: 1000,
    )
    with pytest.raises(InvestigationError, match="investigation_limits_changed"):
        restarted.dispatch(RUN_ID, "status", {})


@pytest.mark.parametrize("reason,status", [
    ("planning_model_infeasible", "infeasible"), ("solver_incomplete", "technical_failure"),
    ("private failure text", "technical_failure"),
])
def test_solver_status_is_not_guessed_from_failure_prose(tmp_path, monkeypatch, reason, status):
    service, source, request = setup_service(tmp_path)

    def failure(connection, actual_request, **kwargs):
        raise SolverFailure(reason, {})

    monkeypatch.setattr(recovery, "prepare", failure)
    result = prepare(service, validate(service, observed_draft(service, source, request)))
    assert result["status"] == status
    assert result["prepared"] is None
    if reason == "private failure text":
        assert reason not in json.dumps(result), "Unknown solver prose must not escape in feedback"


def test_invalid_draft_does_not_poison_a_later_valid_revision(tmp_path):
    service, source, request = setup_service(tmp_path)
    assert validate(service, None)["valid"] is False
    draft = observed_draft(service, source, request)
    result = validate(service, draft)
    assert result["valid"] is True
    assert result["draft_revision"] == 2


def test_interrupted_prepare_is_not_replayed_after_restart(tmp_path, monkeypatch):
    service, source, request = setup_service(tmp_path)
    checked = validate(service, observed_draft(service, source, request))
    saved = state(service)
    saved["status"] = "preparing"
    saved["usage"]["prepares"] = 1
    atomic_json(service.root / "state.json", saved, replace=True)
    monkeypatch.setattr(recovery, "prepare", forbidden_prepare)
    result = prepare(service, checked)
    assert result["status"] == "technical_failure"
    assert result["terminal"]["reason"] == "preparation_interrupted"
    assert result["usage"]["prepares"] == 1


def test_clarification_requires_a_question_and_stays_an_agent_report(tmp_path):
    service, source, request = setup_service(tmp_path)
    with pytest.raises(InvestigationError, match="specific_question_required"):
        service.dispatch(RUN_ID, "finish", {"status": "needs_clarification", "reason": "missing_budget", "question": " "})
    result = service.dispatch(RUN_ID, "finish", {
        "status": "needs_clarification", "reason": "missing_budget",
        "question": "What budget is authorized for order 30?",
    })
    assert result["status"] == "needs_clarification"
    assert result["terminal"]["basis"] == "agent_reported"
    assert result["prepared"] is None
