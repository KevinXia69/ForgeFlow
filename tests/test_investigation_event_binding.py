"""Handwritten event expectations independent of the production binding predicate."""

import hashlib

import pytest
from investigation_support import business_record, make_case, make_draft, source_record

from forgeflow.investigation.event import bind_event
from forgeflow.investigation.evidence import validate_draft


def event(text):
    return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def binding(source, text, *, supplier=1, po=20, product=10):
    return bind_event(
        event(text),
        source,
        source_record(source, "supplier", supplier),
        source_record(source, "purchase_order", po),
        source_record(source, "product", product),
        lambda *args: None,
    )


@pytest.mark.parametrize(
    "text",
    [
        "Supplier B cancelled supply; Supplier A still supplies PO-20.",
        "Supplier B cancelled supply, Supplier A still supplies PO-20.",
        "Supplier A still supplies PO-20. Supplier B cancelled supply.",
        "Supplier B cancelled supply while Supplier A still supplies PO-20.",
        "Supplier B cancelled supply and Supplier A still supplies PO-20.",
        "Supplier A cancelled another supply but still supplies PO-20.",
        "Supplier A cancelled another supply and supplies PO-20.",
        "Supplier A cancelled PO-20, if approval arrives.",
        "Supplier A cancelled PO-20, but that statement is false.",
        "Supplier A says Supplier B cancelled supply for PO-20.",
        "Supplier B cancelled supply. Supplier A is not cancelling PO-20.",
    ],
)
def test_other_supplier_cancellation_never_authorizes_a(text):
    source, contract, request = make_case()
    assert binding(source, text) is not None
    draft, observations = make_draft(source, request)
    assert (
        validate_draft(draft, observations, source, contract, event=event(text))["valid"] is False
    )


@pytest.mark.parametrize("separator", ["; ", ". ", ", ", "\n"])
def test_explicit_cancel_subject_b_can_bind_its_own_unique_supply(separator):
    source, _, _ = make_case()
    text = "Supplier B cancelled supply" + separator + "Supplier A still supplies PO-20."
    assert binding(source, text, supplier=2, po=21) is None
    assert binding(source, text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "供应商 B 取消供货，供应商 A 仍供应 PO-20。",
        "供应商 A 仍供应 PO-20；供应商 B 已经取消供货。",
        "供应商 B撤销供货。供应商 A仍供应 PO-20。",
        "供应商 B 取消供货，而供应商 A 继续供应 PO-20。",
    ],
)
def test_chinese_cancel_subject_is_not_the_still_supplying_vendor(text):
    source, contract, request = make_case()
    source_record(source, "supplier", 1)["fields"]["name"] = "供应商 A"
    source_record(source, "supplier", 2)["fields"]["name"] = "供应商 B"
    assert binding(source, text) is not None
    assert binding(source, text, supplier=2, po=21) is None
    draft, obs = make_draft(source, request)
    assert not validate_draft(draft, obs, source, contract, event=event(text))["valid"]


@pytest.mark.parametrize(
    "text",
    [
        "Supplier A cancelled PO-20.",
        "Supplier A has cancelled a confirmed supply commitment.",
        "V-1 canceled purchase order PO-20. Investigate the affected orders.",
        "Supplier A cancels confirmed supply for SKU FIN.",
        "Supplier A had canceled supply of product FIN.",
    ],
)
def test_supported_single_active_cancellation_retained(text):
    source, _, _ = make_case()
    assert binding(source, text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Supplier A did not cancel PO-20.",
        "Supplier A has not cancelled PO-20.",
        "Supplier A may cancel PO-20.",
        "If Supplier A cancelled PO-20, prepare recovery.",
        "Supplier A cancelled PO-20 if approval arrives.",
        "Has Supplier A cancelled PO-20?",
        "Supplier A cancelled PO-20?",
        "Supplier A cancelled no supply for PO-20.",
        "PO-20 was cancelled by Supplier A.",
        "Supplier B cancelled supply; Supplier A cancelled PO-20.",
        "Supplier A and Supplier B cancelled PO-20.",
        "Supplier A cancelled PO-21; Supplier B still supplies PO-20.",
        "Supplier A still supplies PO-20.",
    ],
)
def test_uncertain_passive_multi_event_or_wrong_po_requires_clarification(text):
    source, _, _ = make_case()
    assert binding(source, text) is not None


def test_an_order_anchor_in_a_non_cancel_clause_cannot_disambiguate():
    source, _, _ = make_case()
    source.records.extend(
        [
            business_record(
                "purchase_order",
                22,
                name="PO-22",
                state="purchase",
                supplier_id="1",
                origin="SO-30",
            ),
            business_record(
                "purchase_order_line",
                202,
                order_id="22",
                product_id="10",
                quantity=5,
                unit_price=60,
                received_quantity=0,
                planned_date="2026-01-02 10:00:00",
            ),
        ]
    )
    assert (
        binding(source, "Supplier A cancelled supply. PO-20 remains active.")
        == "event_supply_ambiguous"
    )
    assert binding(source, "Supplier A cancelled PO-20. PO-22 remains active.") is None


def test_a_product_anchor_in_a_non_cancel_clause_cannot_disambiguate():
    source, _, _ = make_case()
    source.records.extend(
        [
            business_record(
                "purchase_order", 22, name="PO-22", state="purchase", supplier_id="1", origin=""
            ),
            business_record(
                "purchase_order_line",
                202,
                order_id="22",
                product_id="11",
                quantity=5,
                unit_price=60,
                received_quantity=0,
                planned_date="2026-01-02 10:00:00",
            ),
        ]
    )
    assert (
        binding(source, "Supplier A cancelled supply. Product FIN remains available.")
        == "event_supply_ambiguous"
    )
    assert (
        binding(
            source, "Supplier A cancelled supply of product FIN. Product OTHER remains available."
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "供应商 A取消采购单PO-20。",
        "供应商 A已经取消采购订单 PO-20。",
        "供应商 A已撤销产品FIN的供货。",
    ],
)
def test_chinese_explicit_active_cancellation(text):
    source, _, _ = make_case()
    source_record(source, "supplier", 1)["fields"]["name"] = "供应商 A"
    assert binding(source, text) is None


@pytest.mark.parametrize(
    "text",
    [
        "供应商 A没有取消采购单PO-20。",
        "供应商 A尚未取消采购单PO-20。",
        "供应商 A可能取消采购单PO-20。",
        "如果供应商 A取消采购单PO-20，请准备恢复。",
        "采购单PO-20被供应商 A取消。",
        "供应商 A取消采购单PO-20？",
        "供应商 A取消了产品OTHER的供货。",
        "供应商 A取消采购单PO-21；供应商 B继续供应PO-20。",
        "供应商 A取消PO-20，供应商 B撤销PO-21。",
        "供应商 A取消其他供货但仍供应PO-20。",
        "供应商 A取消其他供货并继续供应PO-20。",
        "供应商 A取消PO-20，如果审批通过。",
    ],
)
def test_chinese_negation_wrong_object_and_multiple_cancellations(text):
    source, _, _ = make_case()
    source_record(source, "supplier", 1)["fields"]["name"] = "供应商 A"
    source_record(source, "supplier", 2)["fields"]["name"] = "供应商 B"
    assert binding(source, text) is not None


def test_actual_demo_sentence_remains_supported():
    source, _, _ = make_case()
    source_record(source, "supplier", 1)["fields"]["name"] = "Stonewall Components"
    text = (
        "Stonewall Components has cancelled a confirmed supply commitment. "
        "Investigate the affected product, purchase and customer demand, use the current task policy, "
        "and prepare a reviewable recovery proposal supported by evidence. Do not execute ERP changes. "
        "Ask a specific clarification only if required business information remains unavailable after investigation."
    )
    assert binding(source, text) is None


def test_cancel_inside_business_identifier_is_not_a_second_predicate():
    source, _, _ = make_case()
    source_record(source, "purchase_order", 20)["fields"]["name"] = "PO-CANCEL"
    assert binding(source, "Supplier A cancelled purchase order PO-CANCEL.") is None


@pytest.mark.parametrize(
    "text",
    [
        "Supplier A cancelled the meeting about PO-20.",
        "Supplier A cancelled a discussion of supply for product FIN.",
        "Supplier A cancelled the purchase order PO-20 meeting.",
        "Supplier A cancelled the product FIN launch.",
        "Supplier A cancelled a supply planning meeting about PO-20.",
        "Supplier A cancelled a notification concerning PO-20.",
        "供应商 A取消了关于PO-20的会议。",
        "供应商 A取消了采购单PO-20的讨论。",
        "供应商 A取消了产品FIN的发布会。",
        "供应商 A撤销了供货计划会议。",
    ],
)
def test_non_supply_cancellation_objects_do_not_borrow_incidental_identity(text):
    source, contract, request = make_case()
    if text.startswith("供应商"):
        source_record(source, "supplier", 1)["fields"]["name"] = "供应商 A"
    assert binding(source, text) is not None
    draft, observations = make_draft(source, request)
    assert not validate_draft(draft, observations, source, contract, event=event(text))["valid"]
