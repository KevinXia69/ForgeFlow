"""Invented cost records and root binding checks; not real financial evidence."""
import copy
from decimal import Decimal

import pytest
import test_product_versions as root_tests
from test_recovery_change_set import fixture

from forgeflow.recovery import service
from forgeflow.recovery.change_set import build_change_set
from forgeflow.recovery.product_costs import (
    CostEvidenceError,
    build_cost_report,
    load_cost_report,
    project_cost_report,
)
from forgeflow.recovery.storage import atomic_json, digest, read_json

system = root_tests.system


def inputs():
    request, snapshot, blueprint, plan = fixture()
    snapshot["collected_at_utc"] = "2026-01-01T10:00:00+00:00"
    return request, snapshot, blueprint, build_change_set(request, snapshot, blueprint, plan)


def test_original_arrangement_and_all_actions_recompute_without_invented_fees():
    data = inputs()
    before = copy.deepcopy(data)
    result = build_cost_report(*data)
    assert data == before
    assert {line["action"] for line in result["lines"]} == {"keep", "update", "cancel", "create"}
    assert result["baseline"] == {"purchase": "24", "manufacturing": "26", "total": "50", "known_subtotal": "50"}
    assert result["planned"] == {"purchase": "40", "manufacturing": "30", "total": "70", "known_subtotal": "70"}
    assert result["known_delta"] == "20" and result["comparable"] is True
    assert result["new_purchase_value"] == "0" and result["purchase_increase"] == "16"
    assert result["new_manufacturing_standard"] == "12"
    assert result["full_delta"] is result["actual_cost"] is None
    assert "cancellation_fees" in result["missing"]
    assert all(line["cancellation_fee"] is None for line in result["lines"])
    assert sum(Decimal(line["known_delta"]) for line in result["lines"]) == Decimal("20")


def test_missing_original_standard_is_unknown_not_zero():
    data = inputs()
    data[2]["manufactured_products"][0]["workcenter_choices"][0].pop("unit_cost_dollars")
    result = build_cost_report(*data)
    assert result["baseline"]["manufacturing"] is result["baseline"]["total"] is None
    assert result["baseline"]["known_subtotal"] == "42"
    assert result["planned"]["total"] == "70"
    assert result["known_delta"] is None and result["comparable"] is False
    cancelled = next(line for line in result["lines"] if line["action"] == "cancel")
    assert cancelled["before"]["missing"] == "unit_price_missing" and cancelled["known_delta"] is None


def test_new_purchase_value_does_not_net_off_a_cancelled_order():
    data = inputs()
    old = next(op for op in data[3]["operations"] if op["kind"] == "purchase_order")
    new = copy.deepcopy(old)
    new.update(action="create", before=None, target={"token": "new:purchase.order:0001", "name": None})
    old.update(action="cancel", after=None)
    data[3]["operations"].append(new)
    report = build_cost_report(*data)
    assert report["planned"]["purchase"] == "40" and report["baseline"]["purchase"] == "24"
    assert report["new_purchase_value"] == "40" and report["purchase_increase"] == "0"
    assert report["known_delta"] == "20" and report["full_delta"] is None
    assert "cancellation_fees" in report["missing"]


@pytest.mark.parametrize("price,total,delta", [(None, None, None), (0, "30", "-20"), (0.1, "32.0", "-18.0")])
def test_unknown_zero_and_decimal_prices_are_distinct(price, total, delta):
    data = inputs()
    po = next(op for op in data[3]["operations"] if op["kind"] == "purchase_order")
    po["after"]["unit_price"] = price
    report = build_cost_report(*data)
    assert report["planned"]["total"] == total and report["known_delta"] == delta
    assert report["planned"]["known_subtotal"] == ("30" if price is None else total)
    # A price decrease is not a negative additional purchasing commitment.
    assert report["purchase_increase"] == (None if price is None else "0")
    assert report["full_delta"] is None


@pytest.mark.parametrize("stamp", [None, "2026-01-01T10:00:00", "bad", 123])
def test_missing_observation_time_does_not_enable_comparison(stamp):
    data = inputs()
    data[1]["collected_at_utc"] = stamp
    report = build_cost_report(*data)
    assert report["known_delta"] is None and not report["comparable"]
    assert "snapshot_time" in report["missing"]
    assert report["baseline"]["total"] == "50"


@pytest.mark.parametrize("field,value", [("company_id", 2), ("product_uom_id", 2), ("currency_id", 2),
                                         ("product_code", "OTHER"), ("quantity", True)])
def test_no_implicit_company_unit_currency_or_product_conversion(field, value):
    data = inputs()
    po = next(op for op in data[3]["operations"] if op["kind"] == "purchase_order")
    po["after"][field] = value
    with pytest.raises(CostEvidenceError):
        build_cost_report(*data)


def test_pricing_version_is_independent_of_candidate_quantities_and_order():
    data = inputs()
    first = build_cost_report(*data)
    data[3]["operations"].reverse()
    next(op for op in data[3]["operations"] if op["action"] == "create")["after"]["quantity"] = 2
    second = build_cost_report(*data)
    assert first["planned"] != second["planned"]
    assert first["basis"] == second["basis"]


@pytest.mark.parametrize("fault", ["total", "price", "delta", "actual", "fee", "secret", "metadata", "nan", "currency"])
def test_public_projection_refuses_inconsistent_or_unreviewed_fields(fault):
    report = build_cost_report(*inputs())
    if fault == "total":
        report["planned"]["total"] = "1"
    elif fault == "price":
        report["lines"][0]["before"]["unit_price"] = "100"
    elif fault == "delta":
        report["known_delta"] = "0"
    elif fault == "actual":
        report["actual_cost"] = "70"
    elif fault == "fee":
        report["lines"][0]["cancellation_fee"] = "0"
    elif fault == "secret":
        report["raw_erp_rows"] = {"private": "do not expose"}
    elif fault == "metadata":
        report["lines"][0]["before"]["private_path"] = "/private"
    elif fault == "nan":
        report["lines"][0]["before"]["unit_price"] = "NaN"
    else:
        report["basis"]["currency"] = "EUR"
    with pytest.raises(CostEvidenceError, match="^product_costs_invalid$"):
        project_cost_report(report)


def test_root_artifact_binds_costs_and_executes_original_plan(system):
    decision = root_tests.prepare(system)
    connection, _, options, _ = system
    selected = decision["selected_plan_id"]
    directory = options["store"] / selected
    manifest = read_json(directory / "manifest.json")
    report = load_cost_report(directory, manifest)
    assert digest(report) == manifest["product_costs_sha256"]
    assert report["planned"]["total"] == "70"
    assert not connection.writes
    grant = root_tests.grant_from(root_tests.authorize(options, decision)["approval"])
    result = service.apply(connection, selected, grant=grant, require_grant=True, **options)
    assert result["execution_validated"] is True


@pytest.mark.parametrize("fault", ["tamper", "missing", "symlink"])
def test_changed_cost_artifact_refuses_dispatch(system, fault):
    decision = root_tests.prepare(system)
    connection, _, options, _ = system
    selected = decision["selected_plan_id"]
    grant = root_tests.grant_from(root_tests.authorize(options, decision)["approval"])
    directory = options["store"] / selected
    path = directory / "product-costs.json"
    report = read_json(path)
    path.unlink()
    if fault == "tamper":
        report["planned"]["total"] = "1"
        atomic_json(path, report)
    elif fault == "symlink":
        target = directory / "elsewhere.json"
        atomic_json(target, report)
        path.symlink_to(target)
    with pytest.raises(CostEvidenceError, match="product_costs_binding_changed"):
        service.apply(connection, selected, grant=grant, require_grant=True, **options)
    assert not connection.writes


def test_legacy_missing_costs_stay_missing_and_unbound_extra_is_rejected(tmp_path):
    assert load_cost_report(tmp_path, {}) is None
    atomic_json(tmp_path / "product-costs.json", build_cost_report(*inputs()))
    with pytest.raises(CostEvidenceError, match="product_costs_binding_changed"):
        load_cost_report(tmp_path, {})


def test_decimal_calculation_does_not_depend_on_callers_rounding_context():
    from decimal import localcontext

    data = inputs()
    po = next(op for op in data[3]["operations"] if op["kind"] == "purchase_order")
    po["after"]["unit_price"] = "0.123456789012345678901234"
    with localcontext() as context:
        context.prec = 6
        report = build_cost_report(*data)
        projected = project_cost_report(report)
    line = next(line for line in projected["lines"] if line["kind"] == "purchase_order")
    assert line["after"]["amount"] == "2.469135780246913578024680"


@pytest.mark.parametrize("question,original,unknown", [
    ("方案成本是什么？", "原安排规划成本为 USD 50.00", "完整费用差额未知"),
    ("What does the plan cost?", "Original arrangement planned cost is USD 50.00", "full cost difference is unknown"),
])
def test_qa_uses_same_bound_cost_facts_without_claiming_actual_savings(question, original, unknown):
    from forgeflow.api.case_qa import answer_case

    report = build_cost_report(*inputs())
    case = {"state": "awaiting_approval", "plan": {"new_spend": 70, "costs": report,
            "actions": [], "checks": [], "cost_delta": None}}
    reply = answer_case(question, case)
    assert original in reply["answer"] and unknown in reply["answer"]
    assert "$" not in reply["answer"].split("\n\n")[-1]
    citations = {row["field"]: row["value"] for row in reply["citations"]}
    assert citations["plan.costs.known_delta"] == "20"
    assert citations["plan.costs.new_purchase_value"] == "0"
    assert citations["plan.costs.new_manufacturing_standard"] == "12"
    assert "USD 12.00" in reply["answer"]
    assert "plan.cost_delta" not in citations and "plan.costs.full_delta" not in citations


def test_public_plan_refuses_cost_evidence_from_a_different_snapshot():
    from forgeflow.product.state import _project_plan

    report = build_cost_report(*inputs())
    plan = {"plan_id": "a" * 64, "actions": [], "margin": None, "on_time": None,
            "cost_delta": None, "costs": report, "policy_sha256": "c" * 64,
            "snapshot_sha256": "b" * 64, "prepared_at": "2026-01-01"}
    with pytest.raises(ValueError, match="product_costs_binding_changed"):
        _project_plan(plan)
    plan["snapshot_sha256"] = report["basis"]["snapshot_sha256"]
    assert _project_plan(plan)["costs"] == report
