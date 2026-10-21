from forgeflow.api.case_draft import draft_view, validate_case_draft


def test_draft_accepts_reviewed_contract() -> None:
    body = {"kind": "supplier_cancellation", "product_code": "P27637E67E7-LED-CRS-009", "units": 50, "due_days": 8}
    result = validate_case_draft(body)
    assert result.valid and result.scenario == "A"
    assert draft_view(body)["writes_nothing"] is True


def test_draft_rejects_unreviewed_objects_and_quantities() -> None:
    result = validate_case_draft({"kind": "supplier_cancellation", "product_code": "OTHER", "units": 3, "due_days": 99})
    assert not result.valid
    assert {"product_not_in_contract", "quantity_not_in_contract", "due_date_not_in_contract"} <= set(result.errors)
    assert not validate_case_draft({"kind": "unknown", "product_code": "x", "units": 1, "due_days": 1}).valid
