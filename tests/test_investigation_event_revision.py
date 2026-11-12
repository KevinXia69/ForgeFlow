"""Independent expectations for statement corrections and typed identities."""

from dataclasses import replace

import pytest
from investigation_support import make_case, make_draft, source_record
from test_investigation_event_binding import binding, event
from test_investigation_service import RUN_ID, forbidden_prepare, observed_draft, setup_service

from forgeflow.investigation.evidence import validate_draft
from forgeflow.investigation.service import InvestigationError
from forgeflow.recovery import service as recovery


@pytest.mark.parametrize("tail", [
    ". That statement is false.", "\nThat statement is incorrect.", ". This is false.",
    ". The statement above is false.", ". That statement is not true.", ". It is not true.",
    ". Correction: it will still deliver.", "。该声明不实。", "。更正：供应商仍将交货。",
    "; that statement is false.", ". Supplier A still supplies PO-20.",
    ". It will deliver PO-20.", ". If approval arrives.",
])
def test_retracted_or_conditional_assertion_cannot_validate(tail):
    source, contract, request = make_case()
    text = "Supplier A cancelled PO-20" + tail
    assert binding(source, text) == "event_cancellation_unproven"
    draft, observations = make_draft(source, request)
    assert not validate_draft(draft, observations, source, contract, event=event(text))["valid"]


@pytest.mark.parametrize("code", ["P123", "PO123", "PO-123", "ITEM123"])
@pytest.mark.parametrize("form", ["SKU {}", 'product "{}"', "product '{}'", "产品{}的供货"])
def test_typed_product_identifiers_are_not_order_references(code, form):
    source, contract, request = make_case()
    source_record(source, "product", 10)["fields"]["default_code"] = code
    for row in source.records:
        if row["entity"] == "supplier_offer" and row["fields"]["product_id"] == "10":
            row["fields"]["product_code"] = code
            row["fields"]["offer_key"] = row["fields"]["offer_key"].replace("|FIN|", f"|{code}|")
    contract = replace(contract, product_code=code)
    source._policy = source._policy.copy()
    request["target_product_code"] = code
    text = ("Supplier A cancelled " if not form.startswith("产品") else "Supplier A取消") + form.format(code)
    assert binding(source, text) is None
    draft, observations = make_draft(source, request)
    assert validate_draft(draft, observations, source, contract, event=event(text))["valid"]


def test_explicit_wrong_order_is_not_erased_by_product_spelling():
    source, _, _ = make_case()
    source_record(source, "product", 10)["fields"]["default_code"] = "PO-21"
    assert binding(source, "Supplier A cancelled purchase order PO-21.") is not None
    assert binding(source, "Supplier A cancelled SKU PO-21.") is None


def test_unrelated_vendor_continuing_and_instructions_remain_supported():
    source, _, _ = make_case()
    assert binding(source, "Supplier A cancelled PO-20. Supplier B still supplies PO-21.") is None
    assert binding(source, "Supplier A cancelled PO-20. Ask for clarification if needed.") is None


@pytest.mark.parametrize("text", [
    "Supplier A cancelled PO-20. That statement is false.",
    "Supplier A cancelled PO-20. Correction: it will still deliver.",
    "Supplier A cancelled PO-20。该声明不实。",
    "Supplier B cancelled supply; Supplier A still supplies PO-20.",
])
def test_actual_validation_to_prepare_path_rejects_invalid_events(tmp_path, monkeypatch, text):
    service, source, request = setup_service(tmp_path)
    service.event = event(text)
    monkeypatch.setattr(recovery, "prepare", forbidden_prepare)
    draft = observed_draft(service, source, request)
    checked = service.dispatch(RUN_ID, "validate", {"draft": draft})
    assert checked["valid"] is False and checked["draft_id"] is None
    with pytest.raises(InvestigationError, match="validated_draft_required"):
        service.dispatch(RUN_ID, "prepare", {"draft_id": "0" * 64})
    assert service.dispatch(RUN_ID, "status", {})["usage"]["prepares"] == 0
