"""Post-evaluation runtime regressions; no original evaluation is rescored."""
import copy
import hashlib

import pytest
from investigation_support import (
    EVENT,
    bind_offer,
    business_record,
    make_case,
    make_draft,
    observe,
    source_record,
)
from test_investigation_dev_dataset import observed_draft as real_draft
from test_investigation_dev_dataset import service_for
from test_investigation_fixtures import real_bundle as real_bundle
from test_investigation_service import RUN_ID, observed_draft, setup_service, state, validate

from forgeflow.investigation.evidence import validate_draft
from forgeflow.investigation.fixtures import development_cases
from forgeflow.investigation.service import InvestigationService, Limits
from forgeflow.recovery.storage import read_json


def missing_margin(service, source, request):
    draft = observed_draft(service, source, request)
    draft["evidence"] = [ref for ref in draft["evidence"] if ref["field"] != "margin"]
    return draft


def assert_repeat_stopped(result):
    assert result["status"] == "technical_failure"
    assert result["terminal"]["reason"] == "repeated_invalid_draft_without_new_evidence"
    assert result["valid"] is False and result["draft_id"] is None
    assert result["usage"]["prepares"] == 0


def test_identical_invalid_input_stops_without_replacing_the_stored_draft(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = missing_margin(service, source, request)
    first = validate(service, draft)
    assert first["status"] == "investigating" and first["valid"] is False
    saved = copy.deepcopy(state(service)["drafts"])
    second = validate(service, draft)
    assert_repeat_stopped(second)
    assert second["usage"]["validations"] == 2
    assert state(service)["drafts"] == saved
    assert state(service)["events"][-1]["action"] == "repeated_invalid_draft"


@pytest.mark.parametrize("presentation", ["reason", "citation_order", "candidates", "unresolved_reason",
                                          "same_query", "observation_alias", "irrelevant_query", "display_name",
                                          "product_display_name", "supplier_display_name"])
def test_presentation_refresh_and_irrelevant_queries_are_not_new_business_facts(tmp_path, presentation):
    service, source, request = setup_service(tmp_path)
    if presentation == "supplier_display_name":
        service.event = event("V-1 cancelled purchase order PO-20.")
    draft = missing_margin(service, source, request)
    draft["unresolved"] = [{"field": "margin", "reason": "Need field evidence"}]
    assert validate(service, draft)["valid"] is False
    changed = copy.deepcopy(draft)
    if presentation == "reason":
        changed["reason"] = "A new narrative with different wording"
    elif presentation == "citation_order":
        changed["evidence"].reverse()
    elif presentation == "candidates":
        changed["candidates"] = {"target_product_code": [draft["evidence"][0]["observation_id"]]}
    elif presentation == "unresolved_reason":
        changed["unresolved"][0]["reason"] = "A refreshed explanation, not a new fact"
    elif presentation == "same_query":
        service.clock = lambda: 1001
        service.dispatch(RUN_ID, "query", {"kind": "record", "arguments": {"entity": "sales_order", "id": 30}})
    elif presentation == "observation_alias":
        saved = state(service)
        for ref in changed["evidence"]:
            original = saved["observations"][ref["observation_id"]]
            alias = "refreshed-" + ref["observation_id"]
            saved["observations"][alias] = {**copy.deepcopy(original), "observed_at": 99999}
            ref["observation_id"] = alias
        service._save(saved)
    elif presentation == "irrelevant_query":
        source_record(source, "product", 11)["fields"]["name"] = "Unrelated changed item"
        service.dispatch(RUN_ID, "query", {"kind": "record", "arguments": {"entity": "product", "id": 11}})
    elif presentation == "display_name":
        source_record(source, "sales_order", 30)["fields"]["name"] = "Cosmetic display name"
        row = service.dispatch(RUN_ID, "query", {"kind": "record", "arguments": {"entity": "sales_order", "id": 30}})["records"][0]
        next(ref for ref in changed["evidence"] if ref["field"] == "sales_order_ids.0")["observation_id"] = row["observation_id"]
    else:
        entity, identity, field = (("product", 10, "target_product_code") if presentation == "product_display_name"
                                    else ("supplier", 1, "disruption.vendor_ref"))
        source_record(source, entity, identity)["fields"]["name"] = "New display name outside the event"
        row = service.dispatch(RUN_ID, "query", {"kind": "record", "arguments": {"entity": entity, "id": identity}})["records"][0]
        next(ref for ref in changed["evidence"] if ref["field"] == field)["observation_id"] = row["observation_id"]
    assert_repeat_stopped(validate(service, changed))


def test_duplicate_citation_aliases_do_not_reset_an_invalid_evidence_failure(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = missing_margin(service, source, request)
    draft["evidence"].append(copy.deepcopy(draft["evidence"][0]))
    assert validate(service, draft)["issues"] == [{"code": "invalid_evidence", "field": "evidence"}]
    saved = state(service)
    ref = copy.deepcopy(draft["evidence"][0])
    saved["observations"]["alias"] = copy.deepcopy(saved["observations"][ref["observation_id"]])
    service._save(saved)
    ref["observation_id"] = "alias"
    draft["evidence"].append(ref)
    assert_repeat_stopped(validate(service, draft))


@pytest.mark.parametrize("initial", [[], [{"field": "nonce-1", "reason": "Uncertain"}]])
def test_self_reported_unresolved_field_names_cannot_create_new_business_failures(tmp_path, initial):
    service, source, request = setup_service(tmp_path)
    draft = missing_margin(service, source, request)
    draft["unresolved"] = initial
    assert validate(service, draft)["valid"] is False
    draft["unresolved"] = [{"field": "nonce-2", "reason": "Different narrative"}]
    assert_repeat_stopped(validate(service, draft))


def test_removing_last_self_reported_uncertainty_allows_a_valid_draft(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = observed_draft(service, source, request)
    draft["unresolved"] = [{"field": "margin", "reason": "Needs review"}]
    assert validate(service, draft)["valid"] is False
    draft["unresolved"] = []
    result = validate(service, draft)
    assert result["valid"] is True and result["status"] == "investigating"


@pytest.mark.parametrize("field", ["field", "source_field"])
def test_unsupported_reference_field_nonces_cannot_refresh_failure(tmp_path, field):
    service, source, request = setup_service(tmp_path)
    draft = missing_margin(service, source, request)
    if field == "field":
        draft["evidence"].append(copy.deepcopy(draft["evidence"][0]))
        ref = draft["evidence"][-1]
    else:
        ref = draft["evidence"][0]
    ref[field] = "unsupported-nonce-1"
    assert validate(service, draft)["valid"] is False
    ref[field] = "unsupported-nonce-2"
    assert_repeat_stopped(validate(service, draft))


def test_unselected_budget_key_nonces_are_not_material_request_changes(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = observed_draft(service, source, request)
    budgets = draft["request"]["sales_order_budget_caps"]
    budgets["nonce-1"] = 100
    assert validate(service, draft)["valid"] is False
    budgets.pop("nonce-1")
    budgets["nonce-2"] = 200
    assert_repeat_stopped(validate(service, draft))


def test_selected_budget_change_and_removing_unknown_budget_key_are_repairable(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = observed_draft(service, source, request)
    budgets = draft["request"]["sales_order_budget_caps"]
    budgets["nonce"] = 100
    budgets["30"] = 999
    assert validate(service, draft)["valid"] is False
    budgets["30"] = request["sales_order_budget_caps"]["30"]
    assert validate(service, draft)["status"] == "investigating"
    budgets.pop("nonce")
    assert validate(service, draft)["valid"] is True


@pytest.mark.parametrize("legacy", [False, True])
def test_restart_preserves_invalid_history_including_pre_revision_state(tmp_path, legacy):
    service, source, request = setup_service(tmp_path)
    draft = missing_margin(service, source, request)
    validate(service, draft)
    if legacy:
        saved = state(service)
        saved["drafts"][0].pop("failure_input_sha256")
        service._save(saved)
    restarted = InvestigationService(source=source, contract=service.contract, event=EVENT,
        store=service.store, bundle=service.bundle, connection_factory=lambda: object(), clock=lambda: 1001)
    assert_repeat_stopped(validate(restarted, draft))
    assert restarted.dispatch(RUN_ID, "status", {})["status"] == "technical_failure"


def test_invalid_input_history_cannot_be_evaded_by_alternating_a_b_a(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = observed_draft(service, source, request)
    draft["request"]["margin"] = 0.3
    assert validate(service, draft)["status"] == "investigating"
    changed = copy.deepcopy(draft)
    changed["request"]["margin"] = 0.4
    assert validate(service, changed)["status"] == "investigating"
    assert_repeat_stopped(validate(service, draft))
    assert len(state(service)["drafts"]) == 2


def test_same_request_can_repair_wrong_and_missing_field_evidence(tmp_path):
    service, source, request = setup_service(tmp_path)
    original = observed_draft(service, source, request)
    draft = copy.deepcopy(original)
    next(ref for ref in draft["evidence"] if ref["field"] == "margin")["source_field"] = "anchor_date"
    draft["evidence"] = [ref for ref in draft["evidence"] if not ref["field"].startswith("sales_order_budget_caps")]
    assert validate(service, draft)["valid"] is False
    next(ref for ref in draft["evidence"] if ref["field"] == "margin")["source_field"] = "margin"
    repaired = validate(service, draft)
    assert repaired["status"] == "investigating" and repaired["valid"] is False
    assert repaired["usage"]["validations"] == 2
    complete = validate(service, original)
    assert complete["valid"] is True and complete["status"] == "investigating"
    assert original["request"] == draft["request"]


def test_repairing_reference_format_can_progress_before_every_reference_is_fixed(tmp_path):
    service, source, request = setup_service(tmp_path)
    original = observed_draft(service, source, request)
    draft = copy.deepcopy(original)
    for ref in draft["evidence"][:2]:
        ref["unaccepted_metadata"] = "diagnostic"
    assert validate(service, draft)["valid"] is False
    draft["evidence"][0].pop("unaccepted_metadata")
    assert validate(service, draft)["status"] == "investigating"
    assert validate(service, original)["valid"] is True


def test_real_related_quantity_change_allows_retry_with_identical_request_and_citations(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = missing_margin(service, source, request)
    first = validate(service, draft)
    source_record(source, "purchase_order_line", 200)["fields"]["quantity"] = 7
    second = validate(service, draft)
    assert second["status"] == "investigating"
    assert first["issues"] == second["issues"]
    assert state(service)["drafts"][0]["failure_input_sha256"] != state(service)["drafts"][1]["failure_input_sha256"]
    assert_repeat_stopped(validate(service, draft))


def test_reordering_selected_orders_and_corresponding_citations_is_not_progress(tmp_path):
    source, contract, request = make_case((30, 31))
    service = InvestigationService(source=source, contract=contract, event=EVENT, store=tmp_path,
        bundle=tmp_path / "bundle", connection_factory=lambda: object(), clock=lambda: 1000)
    validate(service, missing_margin(service, source, request))
    request["sales_order_ids"].reverse()
    assert_repeat_stopped(validate(service, missing_margin(service, source, request)))


def test_valid_drafts_are_not_mistaken_for_failed_replays(tmp_path):
    service, source, request = setup_service(tmp_path)
    draft = observed_draft(service, source, request)
    first, second = validate(service, draft), validate(service, draft)
    assert first["valid"] and second["valid"]
    assert second["status"] == "investigating" and second["draft_revision"] == 2


def test_overall_validation_budget_remains_thirty_six_for_distinct_inputs(tmp_path):
    service, source, request = setup_service(tmp_path)
    assert Limits().validations == 36
    draft = observed_draft(service, source, request)
    for index in range(36):
        draft["request"]["margin"] = 0.3 + index / 1000
        assert validate(service, draft)["status"] == "investigating"
    result = validate(service, draft)
    assert result["status"] == "budget_exhausted"
    assert result["terminal"]["reason"] == "validations_limit"


def event(text):
    return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def candidates():
    source, contract, request = make_case()
    source_record(source, "supplier", 2)["fields"]["name"] = "Supplier A"
    first, observations = make_draft(source, request)
    second = copy.deepcopy(first)
    second["request"]["disruption"]["vendor_ref"] = "V-2"
    supplier = source.query("record", {"entity": "supplier", "id": 2})["records"][0]
    po = source.query("record", {"entity": "purchase_order", "id": 21})["records"][0]
    next(ref for ref in second["evidence"] if ref["field"] == "disruption.vendor_ref")["observation_id"] = observe(observations, supplier)
    second["supply_observation_id"] = observe(observations, po)
    return source, contract, first, second, observations


def test_ambiguous_candidates_receive_equivalent_feedback_without_hidden_vendor_exclusion():
    source, contract, first, second, observations = candidates()
    ambiguous = event("Supplier A cancelled its supply.")
    left = validate_draft(first, observations, source, contract, event=ambiguous)
    right = validate_draft(second, observations, source, contract, event=ambiguous)
    assert not left["valid"] and not right["valid"]
    assert left["issues"] == right["issues"] == [{"code": "event_supply_ambiguous", "field": "disruption"}]


def test_no_complete_contract_checks_run_before_visible_identity_but_source_gaps_remain(monkeypatch):
    import forgeflow.investigation.evidence as evidence

    source, contract, first, _, observations = candidates()
    first["evidence"] = [ref for ref in first["evidence"] if ref["field"] != "margin"]
    source_record(source, "sales_order", 31)["fields"]["state"] = "sale"
    monkeypatch.setattr(evidence, "check_request", lambda *_: pytest.fail("Hidden reference used before event binding"))
    report = validate_draft(first, observations, source, contract, event=event("Supplier A cancelled supply."))
    assert {("missing_evidence", "margin"), ("event_supply_ambiguous", "disruption")} <= {
        (row["code"], row["field"]) for row in report["issues"]}
    missing = next(row for row in report["issues"] if row["code"] == "unselected_active_demand")
    assert missing["missing_sales_order_ids"] == [31]
    assert 31 not in contract.sales_order_ids, "The identifier is a live relationship, not the hidden contract answer"


def test_unique_visible_event_restores_contract_authorization_checks():
    source, contract, first, second, observations = candidates()
    assert validate_draft(first, observations, source, contract, event=EVENT)["valid"]
    wrong_authority = validate_draft(second, observations, source, contract,
                                   event=event("Supplier A cancelled purchase order PO-21."))
    assert not wrong_authority["valid"]
    assert {row["code"] for row in wrong_authority["issues"]} == {"vendor_ref_mismatch"}


@pytest.mark.parametrize("uncertainty,expected", [
    ("duplicate_offer", "supply_offer_ambiguous"),
    ("wrong_offer", "offer_binding_mismatch"),
    ("missing_offer_evidence", "missing_evidence"),
])
def test_unresolved_offer_identity_does_not_open_hidden_reference_checks(monkeypatch, uncertainty, expected):
    import forgeflow.investigation.evidence as evidence

    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    if uncertainty == "duplicate_offer":
        duplicate = copy.deepcopy(source_record(source, "supplier_offer", 1000))
        duplicate["id"] = "1010"
        source.records.append(duplicate)
    else:
        bind_offer(draft, observations, source, 1002 if uncertainty == "wrong_offer" else 1000)
        if uncertainty == "missing_offer_evidence":
            draft["evidence"] = [ref for ref in draft["evidence"] if ref["field"] != "disruption.offer_key"]
    monkeypatch.setattr(evidence, "check_request", lambda *_: pytest.fail("Hidden reference used before offer binding"))
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert not report["valid"]
    assert expected in {row["code"] for row in report["issues"]}


def test_unique_component_supply_does_not_reveal_which_finished_target_matches_contract(monkeypatch):
    import forgeflow.investigation.evidence as evidence

    source, contract, request = make_case((30, 31))
    source_record(source, "sales_order_line", 310)["fields"]["product_id"] = "11"
    source_record(source, "purchase_order_line", 200)["fields"]["product_id"] = "12"
    source_record(source, "supplier_offer", 1000)["fields"].update(
        product_id="12", product_template_id="102", product_code="RAW", offer_key="V-1|RAW|1|60.0|1")
    source.records.append(business_record("product", 12, name="Component", default_code="RAW", product_template_id="102"))
    for identity, parent in ((50, "10"), (51, "11")):
        source.records.extend([
            business_record("bom", identity, product_id=parent, product_template_id="100" if parent == "10" else "101",
                            quantity=1, type="normal", active=True),
            business_record("bom_line", identity, bom_id=str(identity), product_id="12", quantity=1),
        ])
    monkeypatch.setattr(evidence, "check_request", lambda *_: pytest.fail("Hidden reference used before target binding"))
    left, observations = make_draft(source, request)
    right = copy.deepcopy(left)
    right["request"]["target_product_code"] = "OTHER"
    product = source.query("record", {"entity": "product", "id": 11})["records"][0]
    next(ref for ref in right["evidence"] if ref["field"] == "target_product_code")["observation_id"] = observe(observations, product)
    reports = [validate_draft(draft, observations, source, contract, event=EVENT) for draft in (left, right)]
    assert reports[0]["issues"] == reports[1]["issues"] == [
        {"code": "component_target_ambiguous", "field": "target_product_code"}]


def test_scope_feedback_repair_still_reaches_real_prepare(real_bundle, tmp_path):
    case = development_cases()[3]
    connection, service = service_for(case, tmp_path, real_bundle)
    before = connection.fingerprint()
    first = real_draft(case, service, initial_order_only=True)
    rejected = service.dispatch(case.case_id, "validate", {"draft": first})
    assert not rejected["valid"] and rejected["status"] == "investigating"
    complete = real_draft(case, service)
    checked = service.dispatch(case.case_id, "validate", {"draft": complete})
    assert checked["valid"], checked
    prepared = service.dispatch(case.case_id, "prepare", {"draft_id": checked["draft_id"]})
    assert prepared["status"] == "prepared"
    assert connection.fingerprint() == before and not connection.write_attempts
    assert read_json(service.root / "review.json")["request"]["sales_order_ids"] == [1, 2]
