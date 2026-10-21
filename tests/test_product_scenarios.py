"""Task-bound product configuration tests; no Docker, ERP or model calls."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.case_qa import answer_case
from forgeflow.api.settings import Settings
from forgeflow.contract import check_request, contract_from_payload
from forgeflow.product import FakeProductExecutor, ProductCaseError, ProductCaseService
from forgeflow.product.executor import ProductExecutionError
from forgeflow.product.local import LocalProductExecutor
from forgeflow.product.state import SCENARIOS, ProductCase

ROOT = Path(__file__).resolve().parents[1]


def make_executor(tmp_path, scenario="B", *, task=None, change=None):
    policy_file = "2299-workcenter-outage-v1.json" if scenario == "B" else "2280_medium_repair_plan_medium.json"
    policy = json.loads((ROOT / "contracts" / policy_file).read_text())
    request = {
        "anchor_date": policy["anchor_date"], "margin": policy["margin"],
        "sales_order_budget_caps": policy["budgets"], "sales_order_ids": policy["sales_order_ids"],
        "target_product_code": policy["product_code"], "disruption": policy["disruption"],
    }
    assert check_request(request, contract_from_payload(policy))["ok"]
    if change:
        request.update(change)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return LocalProductExecutor(socket_path="/tmp/unused-b.sock", store=tmp_path / "store",
                                approvals=tmp_path / "tokens", request=path, container="unused-b",
                                scenario=scenario, task=task)


def test_b_projection_uses_reviewed_order_window_and_workcenter():
    case = ProductCase.new("owner", "B").view()
    assert case["order"]["units"] == 228
    assert (case["order"]["due_days"], case["order"]["due_days_max"]) == (8, 12)
    assert case["disruption"] == {
        "kind": "workcenter_outage", "workcenter": "Assembly Line 1",
        "workcenter_code": "P1223565D60-WC01",
    }
    assert case["result"]["verified"] is False
    for question, forbidden in (("哪些订单受影响？", "供应商"), ("Which orders are affected?", "supplier")):
        reply = answer_case(question, case)
        assert "Assembly Line 1" in reply["answer"]
        assert "8–12" in reply["answer"]
        assert forbidden not in reply["answer"]
        assert "disruption.workcenter" in {row["field"] for row in reply["citations"]}


@pytest.mark.parametrize("scenario", ["A", "B"])
def test_local_configuration_keeps_literal_contract_and_single_task(tmp_path, scenario):
    executor = make_executor(tmp_path, scenario)
    assert executor.supported_scenarios == (scenario,)
    assert executor.task == SCENARIOS[scenario]["task"]
    if scenario == "B":
        assert executor.request["anchor_date"] == "2026-10-21"
        assert sum(executor.request["sales_order_budget_caps"].values()) == 943302


@pytest.mark.parametrize("task,change", [
    ("2280_medium_repair_plan_medium", None),
    (None, {"target_product_code": "P27637E67E7-LED-CRS-009"}),
    (None, {"disruption": {"kind": "supplier_cancellation"}}),
    (None, {"disruption": {"kind": "workcenter_outage", "workcenter_code": "WRONG"}}),
])
def test_mismatched_task_or_request_fails_at_configuration(tmp_path, task, change):
    with pytest.raises(ValueError, match="local_scenario_"):
        make_executor(tmp_path, task=task, change=change)


@pytest.mark.parametrize("scenario", [None, "A"])
def test_wrong_case_never_reaches_prepare_apply_or_status(tmp_path, monkeypatch, scenario):
    executor = make_executor(tmp_path)
    for name in ("prepare", "apply", "status"):
        monkeypatch.setattr(f"forgeflow.product.local.client.{name}", lambda *a, **k: pytest.fail("wrong task dispatched"))
    case = {"case_id": "b" * 32, "scenario": scenario}
    plan = {"plan_id": "a" * 64}
    for call in (lambda: executor.prepare(case), lambda: executor.apply(case, plan), lambda: executor.reconcile(case, plan)):
        with pytest.raises(ProductExecutionError, match="product_scenario_unavailable"):
            call()
    assert not executor.approvals.exists()


def test_switching_sandbox_preserves_old_case_reads_but_blocks_old_approval(tmp_path):
    fake = FakeProductExecutor()
    service = ProductCaseService(tmp_path / "cases", fake)
    case = service.create("owner", "A")
    case = service.prepare(case["case_id"], "owner")
    service.executor = make_executor(tmp_path)
    viewed = service.get(case["case_id"], "owner")
    assert viewed["decision"]["can_approve"] is False
    assert viewed["decision"]["reason"] == "product_scenario_unavailable"
    with pytest.raises(ProductCaseError, match="product_scenario_unavailable"):
        service.approve(case["case_id"], "owner", case["plan"]["plan_id"])
    assert service.get(case["case_id"], "owner")["approval"] is None
    assert fake.apply_calls == 0
    with pytest.raises(ProductCaseError, match="product_scenario_unavailable"):
        service.create("owner", "A")
    assert service.create("owner", "B")["scenario"] == "B"


def test_b_capability_is_separate_from_benchmark_scenarios(tmp_path, monkeypatch):
    executor = make_executor(tmp_path)
    settings = Settings(data_root=tmp_path / "runs", approvals_root=tmp_path / "cases",
                        session_secret="test-b-scenario-secret-at-least-32-characters",
                        invite_codes=("synthetic-b-test-invite",))
    monkeypatch.setattr("forgeflow.recovery.client.health", lambda *a, **k: {"ok": True})
    with TestClient(create_app(settings, product_executor=executor), base_url="https://testserver") as client:
        assert client.post("/api/auth/session", json={"invite_code": "synthetic-b-test-invite"}).status_code == 201
        body = client.get("/api/capabilities").json()
        assert [row["id"] for row in body["product_scenarios"]] == ["B"]
        assert body["environment"]["scenario"] == "B"
        assert body["environment"]["task"] == SCENARIOS["B"]["task"]
