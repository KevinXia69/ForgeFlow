"""An existing entity and a guessed budget do not constitute authorization."""
import copy
import hashlib

import pytest
from investigation_support import EVENT, business_record, make_case, make_draft, source_record

from forgeflow.investigation.event import trusted_event
from forgeflow.investigation.evidence import validate_draft
from forgeflow.investigation.policy import KIND, load_investigation_policy, policy_from_payload
from forgeflow.investigation.service import InvestigationError, InvestigationService
from forgeflow.investigation.sources import FixtureSource


def event(text):
    return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def check(source, contract, request, text):
    draft, observations = make_draft(source, request)
    return validate_draft(draft, observations, source, contract, event=event(text))


def codes(report):
    return {entry["code"] for entry in report["issues"]}


def test_true_supplier_purchase_and_hidden_contract_cannot_resolve_an_ambiguous_event():
    source, contract, request = make_case()
    source_record(source, "supplier", 2)["fields"]["name"] = "Supplier A"
    assert "event_supply_ambiguous" in codes(check(
        source, contract, request, "Supplier A cancelled its supply."))
    assert check(source, contract, request, "Supplier A cancelled PO-20.")["valid"]


def test_wrong_event_or_missing_event_cannot_authorize_real_but_irrelevant_records():
    source, contract, request = make_case()
    assert "event_supplier_unproven" in codes(check(
        source, contract, request, "Supplier B cancelled PO-21."))
    draft, observations = make_draft(source, request)
    assert "event_required" in codes(validate_draft(draft, observations, source, contract))


def test_two_supplier_commitments_need_a_decisive_business_anchor():
    source, contract, request = make_case()
    source.records += [
        business_record("purchase_order", 22, name="PO-22", state="purchase", supplier_id="1", origin="SO-30"),
        business_record("purchase_order_line", 202, order_id="22", product_id="10", quantity=5,
                        unit_price=60, received_quantity=0, planned_date="2026-01-02 10:00:00"),
    ]
    assert "event_supply_ambiguous" in codes(check(
        source, contract, request, "V-1 cancelled supply."))
    assert "event_supply_mismatch" in codes(check(
        source, contract, request, "V-1 cancelled PO-22."))
    assert check(source, contract, request, "V-1 cancelled PO-20.")["valid"]


def test_human_name_substrings_cannot_identify_the_supplier():
    source, contract, request = make_case()
    assert "event_supplier_unproven" in codes(check(
        source, contract, request, "Supplier ABC cancelled PO-20."))


@pytest.mark.parametrize("value", [None, {}, {"text": "a", "sha256": "b"},
                                  {"text": "", "sha256": hashlib.sha256(b"").hexdigest()}])
def test_event_is_required_immutable_input(value):
    with pytest.raises(ValueError):
        trusted_event(value)


def partial_payload(contract):
    return {"schema_version": 1, "kind": KIND, "policy_id": "intake-test",
            "task_id": contract.task_id, "task_type": contract.task_type,
            "anchor_date": contract.anchor_date, "margin": contract.margin,
            "margin_basis": contract.margin_basis, "time_granularity": "day",
            "orders": {"30": {"authorized": True, "budget_cap": None}},
            "sources": {"intake": "Business has not decided the budget."}}


def test_genuinely_missing_budget_is_queryable_but_cannot_prepare(tmp_path):
    original, contract, request = make_case()
    policy = policy_from_payload(partial_payload(contract))
    source = FixtureSource(original.records, policy.source_policy())
    def forbidden():
        pytest.fail("partial policy must never reach preparation")
    service = InvestigationService(source=source, contract=policy, event=EVENT,
                                   store=tmp_path / "store", bundle=tmp_path / "bundle",
                                   connection_factory=forbidden)
    def query(kind, arguments):
        return service.dispatch("partial", "query", {"kind": kind, "arguments": arguments})
    assert query("policy", {"order_id": 30})["records"][0]["fields"] == {
        "authorized": True, "budget_cap": None}
    draft, _ = make_draft(source, request, query=query)
    result = service.dispatch("partial", "validate", {"draft": draft})
    assert not result["valid"] and result["draft_id"] is None
    assert {"complete_policy_required", "evidence_value_mismatch"} <= codes(result)
    with pytest.raises(InvestigationError, match="validated_draft_required"):
        service.dispatch("partial", "prepare", {"draft_id": "a" * 64})
    outcome = service.dispatch("partial", "finish", {
        "status": "needs_clarification", "reason": "budget_missing",
        "question": "What approved recovery-spend budget applies to sales order 30?"})
    assert outcome["status"] == "needs_clarification"
    assert outcome["usage"]["prepares"] == 0
    assert service.contract is None


def test_partial_policy_never_synthesizes_a_full_contract():
    _, contract, _ = make_case()
    payload = partial_payload(contract)
    payload["orders"]["30"]["budget_cap"] = 1000
    policy = load_investigation_policy(payload)
    assert policy.payload()["kind"] == KIND
    changed = policy.payload()
    changed["orders"]["30"]["budget_cap"] = 5
    assert policy.payload()["orders"]["30"]["budget_cap"] == 1000


def test_changed_event_cannot_resume_same_episode(tmp_path):
    source, contract, _ = make_case()
    args = dict(source=source, contract=contract, store=tmp_path / "store", bundle=tmp_path / "bundle",
                connection_factory=lambda: None)
    InvestigationService(**args, event=EVENT).dispatch("run", "status", {})
    with pytest.raises(InvestigationError, match="binding_changed"):
        InvestigationService(**args, event=event("Supplier A cancelled PO-21.")).dispatch("run", "status", {})


def test_mutated_partial_policy_is_rejected_before_reads():
    _, contract, _ = make_case()
    payload = partial_payload(contract)
    malformed = copy.deepcopy(payload)
    malformed["orders"]["30"]["budget_cap"] = -1
    with pytest.raises(ValueError):
        policy_from_payload(malformed)


def test_unresolved_candidate_drafts_are_saved_but_never_validated_for_preparation():
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    draft["candidates"] = {"disruption.vendor_ref": [draft["evidence"][-1]["observation_id"]]}
    draft["unresolved"] = [{"field": "disruption.vendor_ref", "reason": "Which supplier cancelled?"}]
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert not report["valid"] and "unresolved_field" in codes(report)
    draft["candidates"]["disruption.vendor_ref"] = ["model-invented-source"]
    assert "invalid_candidates" in codes(validate_draft(draft, observations, source, contract, event=EVENT))


def test_report_rejects_a_mismatched_review_and_escapes_business_text(tmp_path, monkeypatch):
    from investigation_support import prepared_artifacts

    from forgeflow.investigation.report import render_report
    from forgeflow.recovery import service as recovery
    from forgeflow.recovery.storage import read_json
    source, contract, request = make_case()
    service = InvestigationService(source=source, contract=contract, event=EVENT,
                                   store=tmp_path / "store", bundle=tmp_path / "bundle",
                                   connection_factory=lambda: None)
    def query(kind, arguments):
        return service.dispatch("report", "query", {"kind": kind, "arguments": arguments})
    draft, _ = make_draft(source, request, query=query)
    draft["reason"] = "<script>alert('bad')</script> | raw business note"
    monkeypatch.setattr(recovery, "prepare", lambda connection, request, **kw:
                        prepared_artifacts(kw["store"], request))
    checked = service.dispatch("report", "validate", {"draft": draft})
    service.dispatch("report", "prepare", {"draft_id": checked["draft_id"]})
    state = read_json(service.root / "state.json")
    review = read_json(service.root / "review.json")
    report = render_report(state, review)
    assert review["plan_id"] in report and "<script>" not in report
    review["request"]["margin"] = 0.1
    with pytest.raises(ValueError, match="review_binding_mismatch"):
        render_report(state, review)
