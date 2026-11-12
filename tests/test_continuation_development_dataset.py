"""Hand-authored development expectations and real rule/solver case chains."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from forgeflow.contract import contract_from_payload
from forgeflow.investigation.fixtures import FixtureConnection
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.report import trusted_summary
from forgeflow.investigation.sources import OdooSource
from forgeflow.investigation.transport import PAGE_BYTES, page_size

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/continuation/development-v1.json"
SUITE_SHA256 = "9fad230ce78dee0faa627cd66bde9eb2f1c89a850e2cbd51b54b71e03d6a9941"
spec = importlib.util.spec_from_file_location("continuation_development_runner", ROOT / "scripts/continuation_evaluate.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

# Independent literals, not outputs of the tested parser/binder/request grader.
EXPECTATIONS = [
    ("dev-01", "direct_clear", [1], {"1": 1800}, "V-OLD", "V-OLD|FIN|1|60.0|1", "1", "1800.00"),
    ("dev-02", "multiple_orders_clear", [1, 2], {"1": 2000, "2": 1700}, "V-OLD", "V-OLD|FIN|1|60.0|1", "2", "1700.00"),
    ("dev-03", "component_quote_then_cap", [1], {"1": 1600}, "V-RAW", "V-RAW|RAW|1|2.0|1", "1", "1600.00"),
    ("dev-04", "byte_pagination_fact_then_cap", [1], {"1": 1900}, "V-OLD", "V-OLD|FIN|1|60.0|1", "1", "1900.00"),
    ("dev-05", "retained_purchase_correction", [1], {"1": 1800}, "V-OLD", "V-OLD|FIN|1|60.0|1", "1", "1800.00"),
    ("dev-06", "multiple_offer_correction", [1], {"1": 1700}, "V-OLD", "V-OLD|FIN|1|60.0|1", "1", "1700.00"),
]


def suite():
    return json.loads(DATA.read_text())


def test_development_bytes_and_independent_expectations_are_fixed():
    assert hashlib.sha256(DATA.read_bytes()).hexdigest() == SUITE_SHA256
    value = suite()
    assert value["schema_version"] == 1 and value["split"] == "development"
    assert value["suite_id"] == "continuation-development-v1"
    assert len(value["cases"]) == len(EXPECTATIONS) == 6
    for case, (identity, family, orders, caps, vendor, offer, missing, amount) in zip(value["cases"], EXPECTATIONS):
        assert (case["case_id"], case["family"], str(case["missing_order_id"])) == (identity, family, missing)
        assert case["expected"] == {
            "terminal": "prepared", "budget": amount,
            "request": {"target_product_code": "FIN", "sales_order_ids": orders,
                        "anchor_date": "2026-01-01", "margin": 0.2, "sales_order_budget_caps": caps,
                        "disruption": {"kind": "supplier_cancellation", "vendor_ref": vendor, "offer_key": offer}},
            "allowed_offer_keys": [offer],
        }
        assert case["script"][-1] == {"trigger": "draft_ready", "confirm": True,
                                      "confirm_if": {"order_id": missing, "amount": amount, "currency": "USD"}}
        policy = runner.missing_policy(contract_from_payload(case["contract"]), case["missing_order_id"])
        assert policy.payload()["orders"][missing] == {"authorized": True, "budget_cap": None}
        assert isinstance(policy, PartialPolicy)
    assert [[step["trigger"] for step in case["script"]] for case in value["cases"]] == [
        ["missing_budget", "draft_ready"], ["missing_budget", "draft_ready"],
        ["missing_budget", "clarification", "draft_ready"],
        ["missing_budget", "clarification", "draft_ready"],
        ["missing_budget", "draft_ready", "draft_ready"],
        ["missing_budget", "draft_ready", "draft_ready"],
    ]


def test_multiorder_uses_distinct_real_customers_and_preserves_known_budget():
    case = suite()["cases"][1]
    orders = case["records"]["sale.order"]
    assert [(row["id"], row["partner_id"]) for row in orders] == [(1, 1), (2, 12)]
    assert next(row for row in case["records"]["res.partner"] if row["id"] == 12)["ref"] == "C2"
    policy = runner.missing_policy(contract_from_payload(case["contract"]), 2)
    assert policy.payload()["orders"]["1"] == {"authorized": True, "budget_cap": 2000}


def test_long_origin_case_really_byte_pages_and_keeps_same_company_stock_separate():
    case = suite()["cases"][3]
    source = OdooSource(FixtureConnection(case["records"]), contract_from_payload(case["contract"]))
    rows, cursor, pages = [], None, 0
    while True:
        arguments = {"entity": "supplier", "id": "3", "limit": 50}
        if cursor:
            arguments["cursor"] = cursor
        page = source.query("relations", arguments)
        assert page_size(page["records"], page["next_cursor"]) <= PAGE_BYTES
        rows.extend(page["records"])
        pages += 1
        if page["complete"]:
            break
        cursor = page["next_cursor"]
    assert pages > 1
    assert len({row["id"] for row in rows if row["entity"] == "purchase_order"}) == 25
    records = case["records"]
    assert {row["company_id"] for row in records["purchase.order"]} == {1}
    assert {row["product_id"] for row in records["purchase.order.line"] if row["id"] >= 200} == {200}
    assert all("SO-1" not in row["origin"] for row in records["purchase.order"] if row["id"] >= 200)


@pytest.mark.parametrize("index", range(6), ids=[row[0] for row in EXPECTATIONS])
def test_six_families_use_real_rule_queries_confirmation_and_preparation(tmp_path, monkeypatch, index):
    def no_model(*args, **kwargs):
        pytest.fail("Development rule acceptance must not call a provider")

    monkeypatch.setattr(runner.PiCaseWorker, "episode", no_model)
    monkeypatch.setattr(runner.PiCaseWorker, "_model", no_model)
    case = suite()["cases"][index]
    output = tmp_path / case["case_id"]
    result = runner.run_case(case, arm="rules", output=output,
                             dataset={"sha256": SUITE_SHA256, "split": "development", "case_id": case["case_id"]})
    assert result["passed"], result
    assert result["prepared"] and result["request_correct"] and result["terminal"] == "prepared"
    assert result["usage"]["tokens"] == 0
    assert result["usage"]["validations"] == result["usage"]["prepares"] == 1
    assert result["episodes"] == 2
    state_file = next((output / "trusted/cases").glob("*/case.json"))
    state = json.loads(state_file.read_text())
    assert state["confirmation"] is not None
    episode = state["episodes"][state["current_episode"]]
    store = state_file.parent / "episodes" / episode["episode_id"] / "store"
    service_state = json.loads((store / "investigation/state.json").read_text())
    review = json.loads((store / "investigation/review.json").read_text())
    trusted = trusted_summary(service_state, review, store=store)
    assert trusted["status"] == "prepared"
    assert review["request"] == case["expected"]["request"]
    assert result["checks"]["parent_immutable"]
    assert result["checks"]["no_business_writes"] and result["checks"]["business_tables_unchanged"]
