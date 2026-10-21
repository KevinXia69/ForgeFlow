"""G3: a reviewed contract binds a task and reaches the root executor."""

from __future__ import annotations

import json
import shlex

import pytest

from forgeflow.agents.pi_nebius import PiNebius, build_pi_command
from forgeflow.contract import build_contract, check_request, contract_from_payload
from forgeflow.contract_guidance import resolve_contract_guidance
from forgeflow.runner import run_task


def make_payload(task_id: str = "2281_medium_repair_plan_medium") -> dict:
    contract = build_contract(
        contract_id="ff-test-policy-v1",
        task_id=task_id,
        task_type="supplier_cancellation",
        product_code="P-TEST",
        anchor_date="2026-10-22",
        margin=0.253,
        margin_basis="portfolio_new_spend_margin_at_list_price",
        budgets={"1": 100.0},
        sales_order_ids=[1],
        disruption={
            "kind": "supplier_cancellation",
            "vendor_ref": "V-1",
            "vendor_name": "Vendor One",
        },
        original_commitment_policy="keep_unaffected_work",
        sources={"instruction": "tests/fixture.md"},
    )
    return contract.payload()


def test_contract_payload_round_trips_and_checks_the_request(tmp_path):
    payload = make_payload()
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(payload))
    contract = contract_from_payload(json.loads(path.read_text()))
    assert contract.digest() == contract_from_payload(payload).digest()
    request = {
        "target_product_code": "P-TEST",
        "anchor_date": "2026-10-22",
        "margin": 0.253,
        "sales_order_ids": [1],
        "sales_order_budget_caps": {"1": 100},
        "disruption": {"kind": "supplier_cancellation", "vendor_ref": "V-1"},
    }
    assert check_request(request, contract)["ok"] is True
    request["disruption"] = {"kind": "supplier_cancellation", "vendor_ref": "Vendor One"}
    report = check_request(request, contract)
    assert report["ok"] is False
    assert report["issues"][0]["code"] == "identifier_is_display_name"


def test_executor_start_command_carries_the_policy(monkeypatch):
    payload = make_payload()
    monkeypatch.setenv("FORGEFLOW_CONTRACT_JSON", json.dumps(payload))
    agent = object.__new__(PiNebius)
    agent.contract_payload = payload
    command = agent._executor_start_command()
    assert "--policy-file /var/lib/forgeflow/recovery/policy.json" in command
    assert "base64 -d" in command
    assert "chmod 0400 /var/lib/forgeflow/recovery/policy.json" in command


def test_run_task_rejects_a_contract_for_another_task(tmp_path):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(make_payload()))
    with pytest.raises(ValueError, match="task_id"):
        run_task(tmp_path, "A", "nop", contract_path=path)


def test_strict_contract_mode_rejects_a_run_without_one(tmp_path):
    with pytest.raises(ValueError, match="requires a reviewed task contract"):
        run_task(tmp_path, "A", "nop", require_contract=True)


def test_contract_guidance_supplies_only_reviewed_inputs_and_keeps_original_task():
    payload = make_payload()
    payload["sources"]["private_note"] = "This source text must not enter the prompt"
    record = resolve_contract_guidance("reviewed-request-v1", payload)
    request = json.loads(record["text"].splitlines()[-1])
    assert check_request(request, contract_from_payload(payload))["ok"]
    assert "This source text" not in record["text"]
    assert "policy.json" not in record["text"]
    assert "optimal_plan" not in record["text"]
    original = "Original task with 'quotes' and $(literal) characters"
    command = shlex.split(build_pi_command(
        original, "test-model", planner_profile="cp-sat-recovery-v1",
        contract_guidance="reviewed-request-v1", contract_payload=payload,
    ))
    prompt = command[command.index("--append-system-prompt") + 1]
    assert record["text"] in prompt
    assert command[command.index("--") + 1] == original
    assert resolve_contract_guidance()["text"] == ""
    assert "reviewed task policy" not in build_pi_command(
        original, "test-model", planner_profile="cp-sat-recovery-v1", contract_payload=payload,
    )


def test_contract_guidance_has_explicit_experiment_boundary(tmp_path):
    with pytest.raises(ValueError, match="requires a reviewed contract"):
        run_task(tmp_path, "A", "pi", contract_guidance="reviewed-request-v1")
    with pytest.raises(ValueError, match="Unknown contract guidance"):
        resolve_contract_guidance("arbitrary-file.txt", make_payload())
    with pytest.raises(ValueError, match="requires cp-sat-recovery-v1"):
        build_pi_command("task", "model", contract_guidance="reviewed-request-v1",
                         contract_payload=make_payload())
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(make_payload("2280_medium_repair_plan_medium")))
    with pytest.raises(ValueError, match="requires Pi"):
        run_task(tmp_path, "A", "nop", contract_path=path,
                 contract_guidance="reviewed-request-v1")


def test_contract_guidance_metadata_is_separate_and_prompt_is_not_public():
    from forgeflow.runview import experiment_labels

    record = resolve_contract_guidance("reviewed-request-v1", make_payload())
    view = experiment_labels({"contract_guidance": record})
    assert view["contract_guidance"] == {
        "profile": "reviewed-request-v1", "sha256": record["sha256"], "recorded": True,
    }
    assert "text" not in view["contract_guidance"]
    assert experiment_labels({})["contract_guidance"]["recorded"] is False
