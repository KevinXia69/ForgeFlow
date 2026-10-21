from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeflow.contract import (
    check_changes,
    check_request,
    contract_from_payload,
)

REPO = Path(__file__).resolve().parents[1]
CONTRACTS = REPO / "contracts"
RUNS = REPO / "runs"

TASK_A = "2280_medium_repair_plan_medium"
TASK_B = "2299_hard_repair_plan_hard"

INSTRUCTION_BUDGETS = {
    TASK_A: {"1": 3735.0, "2": 4231.0, "3": 4643.0, "4": 6417.0, "5": 6828.0},
    TASK_B: {
        "1": 76068.0, "2": 61473.0, "3": 77258.0, "4": 95060.0,
        "5": 63579.0, "6": 63986.0, "7": 92957.0, "8": 65686.0,
        "9": 111508.0, "10": 72147.0, "11": 72994.0, "12": 90586.0,
    },
}


def load(name):
    return contract_from_payload(json.loads((CONTRACTS / name).read_text()))


def recorded(run_id, filename):
    matches = list((RUNS / run_id).glob(f"harbor/job/*/agent/recovery/*/{filename}"))
    return json.loads(matches[0].read_text()) if matches else None


@pytest.mark.parametrize("name", [
    "2280-supplier-cancellation-v1.json",
    "2299-workcenter-outage-v1.json",
])
def test_contract_files_are_valid_and_stable(name):
    first = load(name)
    second = load(name)
    assert first.digest() == second.digest()
    assert len(first.digest()) == 64
    assert first.sources


@pytest.mark.parametrize("name,task_id", [
    ("2280-supplier-cancellation-v1.json", TASK_A),
    ("2299-workcenter-outage-v1.json", TASK_B),
])
def test_budgets_match_the_instruction_values(name, task_id):
    contract = load(name)
    assert contract.budgets == INSTRUCTION_BUDGETS[task_id]
    assert set(contract.budgets) == {str(item) for item in contract.sales_order_ids}


def test_budget_total_matches_the_instruction_sum():
    assert round(sum(INSTRUCTION_BUDGETS[TASK_B].values()), 2) == 943302.0
    # The recorded B request used the SO untaxed amounts instead. Recorded runs
    # are not committed, so this half only runs where they are present.
    recorded_request = recorded("workflow-b-off-20261020-01", "request.json")
    if recorded_request is None:
        pytest.skip("recorded runs are not present")
    assert round(sum(recorded_request["sales_order_budget_caps"].values()), 2) == 883693.8


def test_b_contract_names_the_downed_workcenter():
    contract = load("2299-workcenter-outage-v1.json")
    assert contract.forbidden_workcenters == ("P1223565D60-WC01",)
    assert contract.disruption["workcenter_name"] == "Assembly Line 1"


def test_b_change_set_does_not_schedule_the_downed_workcenter():
    if not RUNS.is_dir():
        pytest.skip("recorded runs are not present")
    changes = recorded("workflow-b-off-20261020-01", "change-set.json")
    report = check_changes(changes, load("2299-workcenter-outage-v1.json"))
    assert report["ok"] is True


@pytest.mark.skipif(not RUNS.is_dir(), reason="recorded runs are not present")
def test_a_recorded_request_satisfies_its_contract():
    request = recorded("workflow-a-off-20261020-01", "request.json")
    report = check_request(request, load("2280-supplier-cancellation-v1.json"))
    assert report["ok"] is True
    assert report["issues"] == []


@pytest.mark.skipif(not RUNS.is_dir(), reason="recorded runs are not present")
def test_b_recorded_request_is_rejected_on_exactly_the_twelve_budgets():
    request = recorded("workflow-b-off-20261020-01", "request.json")
    report = check_request(request, load("2299-workcenter-outage-v1.json"))
    assert report["ok"] is False
    assert [issue["code"] for issue in report["issues"]] == ["budget_mismatch"] * 12
    assert [issue["field"] for issue in report["issues"]] == [
        f"sales_order_budget_caps.{index}" for index in range(1, 13)
    ]
    first = report["issues"][0]
    assert first["expected"] == 76068.0
    assert first["observed"] == 69765.3
