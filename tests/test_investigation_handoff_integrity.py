"""Real continuation prepare cannot hand off corrupted source evidence."""

import hashlib
from dataclasses import replace

import pytest
from test_investigation_dev_dataset import observed_draft
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.investigation.cases import InvestigationCases
from forgeflow.investigation.fixtures import FixtureConnection, supplier_cancellation_control
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import PartialPolicy, policy_from_payload
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import OdooSource
from forgeflow.recovery.storage import atomic_json, read_json


@pytest.fixture
def prepared_case(tmp_path, real_bundle):
    fixture = supplier_cancellation_control()
    contract = fixture.contract.payload()
    policy = policy_from_payload({"kind": "forgeflow.investigation_policy", "schema_version": 1,
        "policy_id": "intake-control", "task_id": contract["task_id"],
        **{k: contract[k] for k in ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")},
        "orders": {"1": {"authorized": True, "budget_cap": None}}, "sources": {"policy": "task-owner"}})
    connection = FixtureConnection(fixture.records)
    before = connection.fingerprint()
    def factory(value):
        return OdooSource(connection, value.source_policy() if isinstance(value, PartialPolicy) else value)
    event = {"text": fixture.event, "sha256": hashlib.sha256(fixture.event.encode()).hexdigest()}
    cases = InvestigationCases(tmp_path / "cases", source_factory=factory, generation=lambda: "generation-1",
        currency_probe=lambda _: {"currency": "USD", "company_id": 1, "provenance": "fixed-fixture",
                                 "record_currencies": {"sales_order:1": "USD", "purchase_order:1": "USD"}})
    view = cases.create("owner", operation_id="create", template=task_template(fixture.contract), policy=policy, event=event)
    def service(config):
        return InvestigationService(source=factory(config["policy"]), contract=config["policy"], event=event,
            store=config["store"], bundle=real_bundle, connection_factory=lambda: connection)
    parent = cases.episode_config(view["case_id"], "owner", view["current_episode"])
    service(parent).dispatch(parent["run_id"], "finish", {"status": "needs_clarification", "reason": "Missing cap",
        "question": "What budget do you authorize for SO-1?"})
    view = cases.record_terminal(view["case_id"], "owner", parent["run_id"])
    reply = cases.submit_reply(view["case_id"], "owner", operation_id="reply", expected_revision=view["revision"], text="SO-1 budget 2000 USD")
    view = cases.propose(view["case_id"], "owner", reply_id=reply["reply_id"], proposal={
        "kind": "budget_authorization_proposal", "order_id": "1", "amount": "2000.00", "currency": "USD"})
    receipt = cases.confirm(view["case_id"], "owner", operation_id="confirm", expected_revision=view["revision"],
        draft_sha256=view["draft"]["draft_sha256"], parent_policy_sha256=view["ticket"]["parent_policy_sha256"], confirmed=True)
    child = cases.episode_config(view["case_id"], "owner", receipt["child_episode"])
    investigator = service(child)
    child_fixture = replace(fixture, case_id=child["run_id"], policy=child["policy"])
    draft = observed_draft(child_fixture, investigator)
    checked = investigator.dispatch(child["run_id"], "validate", {"draft": draft})
    assert checked["valid"] is True
    result = investigator.dispatch(child["run_id"], "prepare", {"draft_id": checked["draft_id"]})
    assert result["status"] == "prepared"
    view = cases.record_terminal(view["case_id"], "owner", child["run_id"])
    assert connection.fingerprint() == before and connection.write_attempts == []
    return cases, view, investigator.store / result["prepared"]["plan_id"]


def test_real_valid_prepare_has_idempotent_non_execution_handoff(prepared_case):
    cases, view, _ = prepared_case
    args = {"operation_id": "handoff", "expected_revision": view["revision"]}
    receipt = cases.handoff(view["case_id"], "owner", **args)
    assert receipt["phase"] == "pending"
    assert cases.handoff(view["case_id"], "owner", **args) == receipt
    assert cases.get(view["case_id"], "owner")["status"] == "handoff_pending"


@pytest.mark.parametrize("name", ["request", "snapshot", "blueprint", "plan", "change-set", "baseline", "binding", "prepared"])
def test_real_prepared_changed_artifact_blocks_handoff(prepared_case, name):
    cases, view, directory = prepared_case
    path = directory / (name + ".json")
    payload = read_json(path)
    payload["tampered"] = True
    atomic_json(path, payload, replace=True)
    with pytest.raises(ValueError, match="binding_mismatch"):
        cases.handoff(view["case_id"], "owner", operation_id="handoff", expected_revision=view["revision"])
    assert cases.get(view["case_id"], "owner")["handoff"] is None


def test_currency_change_after_confirmed_prepare_blocks_handoff(prepared_case):
    cases, view, _ = prepared_case
    old = cases.currency_probe
    def changed(template):
        value = old(template)
        value['currency'] = 'EUR'
        value['record_currencies'] = {key: 'EUR' for key in value['record_currencies']}
        return value
    cases.currency_probe = changed
    with pytest.raises(ValueError, match='currency_binding_changed'):
        cases.handoff(view['case_id'], 'owner', operation_id='handoff', expected_revision=view['revision'])
    assert cases.get(view['case_id'], 'owner')['handoff'] is None
