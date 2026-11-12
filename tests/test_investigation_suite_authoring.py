"""Authoring utilities are checked only on already exposed development controls.

No test calls a held-out recipe or constructs the eight-case suite.
"""

import importlib.util
import random
import re
from pathlib import Path

import pytest
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.investigation.fixtures import (
    FixtureConnection,
    development_cases,
    supplier_cancellation_control,
)
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.sources import OdooSource
from forgeflow.recovery.service import prepare
from forgeflow.recovery.storage import digest, read_json


@pytest.fixture
def authoring():
    path = Path(__file__).resolve().parents[1] / "scripts/investigation_case_suite.py"
    spec = importlib.util.spec_from_file_location("case_suite_authoring", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invalid_authoring_inputs_cannot_invoke_any_recipe(authoring, monkeypatch):
    def forbidden():
        raise AssertionError("No held-out construction is authorized by this test")
    monkeypatch.setattr(authoring, "development_cases", forbidden)
    with pytest.raises(ValueError, match="generation_seed"):
        authoring.build_heldout_suite("bad", "a" * 64)
    with pytest.raises(ValueError, match="configuration_digest"):
        authoring.build_heldout_suite("b" * 64, "bad")


def test_known_control_identity_transform_preserves_real_preparation(authoring, real_bundle, tmp_path):
    original = supplier_cancellation_control()
    case = authoring._opaque_case(original, random.Random(0), 0)
    assert re.fullmatch(r"p[a-f0-9]{24}", case.policy.contract_id)
    assert re.fullmatch(r"t[a-f0-9]{24}", case.policy.task_id)
    assert case.policy.anchor_date != original.policy.anchor_date
    assert case.policy.sales_order_ids != original.policy.sales_order_ids
    request = {"target_product_code": case.policy.product_code,
               "sales_order_ids": list(case.policy.sales_order_ids),
               "anchor_date": case.policy.anchor_date, "margin": case.policy.margin,
               "sales_order_budget_caps": dict(case.policy.budgets),
               "disruption": {"kind": "supplier_cancellation", "vendor_ref": case.policy.disruption["vendor_ref"]}}
    connection = FixtureConnection(case.records)
    before = connection.fingerprint()
    result = prepare(connection, request, store=tmp_path / "store", bundle=real_bundle, contract=case.policy)
    assert result["ok"] is True and result["odoo_writes"] is False
    manifest = read_json(tmp_path / "store" / result["plan_id"] / "manifest.json")
    assert digest(manifest) == result["plan_id"]
    assert connection.fingerprint() == before and connection.write_attempts == []
    purchase = case.records["purchase.order"][0]
    line = case.records["purchase.order.line"][0]
    assert purchase["order_line"] == [line["id"]]
    assert line["order_id"] == purchase["id"]


def test_known_partial_development_policy_stays_partial_and_opaque(authoring):
    original = development_cases()[4]
    case = authoring._opaque_case(original, random.Random(7), 0)
    assert isinstance(case.policy, PartialPolicy)
    assert case.contract is None
    assert re.fullmatch(r"p[a-f0-9]{24}", case.policy.contract_id)
    order_id = case.records["sale.order"][0]["id"]
    source = OdooSource(FixtureConnection(case.records), case.policy.source_policy())
    assert source.query("policy", {"order_id": order_id})["records"][0]["fields"] == {
        "authorized": True, "budget_cap": None}
    assert case.oracle["required_question_fields"] == [f"sales_order_budget_caps.{order_id}"]
    assert case.policy.payload()["orders"][str(order_id)]["budget_cap"] is None


def test_known_duplicate_name_control_retains_its_identity_ambiguity(authoring):
    original = development_cases()[1]
    case = authoring._opaque_case(original, random.Random(17), 0)
    old, alternate = case.records["res.partner"][2:4]
    assert old["name"] == alternate["name"]
    assert old["ref"] != alternate["ref"]
    assert old["id"] != alternate["id"]
    assert case.records["purchase.order"][0]["name"] in case.event
    assert "PO-CANCEL" not in case.event and "Fixture old vendor" not in case.event
