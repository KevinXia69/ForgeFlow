from __future__ import annotations

import json

from forgeflow.cli import main
from forgeflow.contract import build_contract


def contract_payload(**overrides):
    values = dict(
        contract_id="cli-check-v1",
        task_id="synthetic_task",
        task_type="supplier_cancellation",
        product_code="FIN",
        anchor_date="2026-01-01",
        margin=0.2,
        margin_basis="portfolio_new_spend_margin_at_selling_price",
        budgets={"1": 2000.0},
        sales_order_ids=(1,),
        disruption={"kind": "supplier_cancellation", "vendor_ref": "V1", "vendor_name": "Vendor One"},
        original_commitment_policy="keep_commitments_that_still_work",
        sources={"budgets": "synthetic"},
    )
    values.update(overrides)
    return build_contract(**values).payload()


def request_payload(**overrides):
    values = {
        "target_product_code": "FIN",
        "sales_order_ids": [1],
        "anchor_date": "2026-01-01",
        "margin": 0.2,
        "sales_order_budget_caps": {"1": 2000},
        "disruption": {"kind": "supplier_cancellation", "vendor_ref": "V1"},
    }
    values.update(overrides)
    return values


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def test_matching_request_exits_zero(tmp_path, capsys):
    contract = write(tmp_path, "contract.json", contract_payload())
    request = write(tmp_path, "request.json", request_payload())
    assert main(["contract", "--file", str(contract), "--request", str(request)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert len(output["policy_sha256"]) == 64
    assert output["request"]["issues"] == []


def test_conflicting_request_exits_nonzero_and_names_the_field(tmp_path, capsys):
    contract = write(tmp_path, "contract.json", contract_payload())
    request = write(tmp_path, "request.json", request_payload(
        sales_order_budget_caps={"1": 1900},
    ))
    assert main(["contract", "--file", str(contract), "--request", str(request)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["request"]["issues"][0]["code"] == "budget_mismatch"


def test_changes_are_checked_when_supplied(tmp_path, capsys):
    contract = write(tmp_path, "contract.json", contract_payload(
        task_type="workcenter_outage",
        disruption={"kind": "workcenter_outage", "workcenter_code": "WC-A",
                    "workcenter_name": "Assembly A"},
        forbidden_workcenters=("WC-A",),
    ))
    changes = write(tmp_path, "changes.json", {"operations": [
        {"action": "create", "after": {"workcenter_code": "WC-A"}, "kind": "manufacturing_order"},
    ]})
    assert main(["contract", "--file", str(contract), "--changes", str(changes)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["changes"]["issues"][0]["code"] == "forbidden_workcenter_in_plan"


def test_invalid_json_is_a_configuration_error(tmp_path, capsys):
    contract = tmp_path / "contract.json"
    contract.write_text("{not json")
    assert main(["contract", "--file", str(contract)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "configuration_error"
