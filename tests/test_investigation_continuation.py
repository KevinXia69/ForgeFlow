"""Independent acceptance of the missing-budget policy boundary and lifecycle."""
import copy
import json
from dataclasses import asdict

import pytest
from investigation_support import EVENT, make_case, source_record

from forgeflow.investigation.cases import CaseError, CaseLimits, InvestigationCases
from forgeflow.investigation.intake import IntakeError, inspect_intake, task_template
from forgeflow.investigation.policy import PartialPolicy, policy_from_payload
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import FixtureSource
from forgeflow.recovery.storage import read_json


def partial(contract):
    c = contract.payload()
    return policy_from_payload({"kind": "forgeflow.investigation_policy", "schema_version": 1,
        "policy_id": "missing-budget", "task_id": c["task_id"],
        **{k: c[k] for k in ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")},
        "orders": {str(i): {"authorized": True, "budget_cap": None if i == 30 else c["budgets"][str(i)]}
                   for i in c["sales_order_ids"]}, "sources": {"policy": "task-owner"}})


def make_system(tmp_path):
    source, contract, request = make_case((30, 31))
    policy = partial(contract)
    environment = {"generation": "erp-generation-1", "currency": "USD"}

    def factory(p):
        return FixtureSource(source.records, p.source_policy() if isinstance(p, PartialPolicy) else p)

    def currency(_):
        return {"currency": "USD", "company_id": 1, "provenance": "fixture-defined-single-currency",
                "record_currencies": {"orders-and-offers": environment["currency"]}}

    cases = InvestigationCases(tmp_path / "cases", source_factory=factory,
        generation=lambda: environment["generation"], currency_probe=currency, clock=lambda: 100)
    view = cases.create("owner", operation_id="create-1", template=task_template(contract),
                        policy=policy, event=EVENT)
    return {"cases": cases, "view": view, "source": source, "contract": contract,
            "request": request, "policy": policy, "factory": factory, "environment": environment}


@pytest.fixture
def system(tmp_path):
    return make_system(tmp_path)


def waiting(system):
    cases, view = system["cases"], system["view"]
    config = cases.episode_config(view["case_id"], "owner", view["current_episode"])
    service = InvestigationService(source=system["factory"](system["policy"]), contract=system["policy"],
        store=config["store"], bundle=config["store"] / "unused", connection_factory=lambda: None, event=EVENT)
    service.dispatch(config["run_id"], "finish", {"status": "needs_clarification",
        "reason": "Missing authorized budget", "question": "What is the authorized budget of SO-30?"})
    return cases.record_terminal(view["case_id"], "owner", view["current_episode"])


def proposed(system, amount="800", text="SO-30 budget 800 USD"):
    view = waiting(system)
    cases, identity = system["cases"], view["case_id"]
    reply = cases.submit_reply(identity, "owner", operation_id="reply-1", expected_revision=view["revision"], text=text)
    return cases.propose(identity, "owner", reply_id=reply["reply_id"],
        proposal={"kind": "budget_authorization_proposal", "order_id": "30", "amount": amount, "currency": "USD"})


def confirm(cases, view, **overrides):
    params = {"operation_id": "confirm-1", "expected_revision": view["revision"],
              "draft_sha256": view["draft"]["draft_sha256"],
              "parent_policy_sha256": view["ticket"]["parent_policy_sha256"], "confirmed": True}
    params.update(overrides)
    return cases.confirm(view["case_id"], "owner", **params)


def test_actual_parent_terminal_to_ticket_and_new_policy(system):
    cases = system["cases"]
    view = proposed(system)
    parent_id = view["current_episode"]
    before = cases.episode_config(view["case_id"], "owner", parent_id)
    parent_bytes = (before["store"] / "investigation/state.json").read_bytes()
    receipt = confirm(cases, view)
    child = cases.episode_config(view["case_id"], "owner", receipt["child_episode"])
    assert child["store"] != before["store"]
    assert child["socket"] != before["socket"]
    assert child["policy"].budgets == {"30": 800.0, "31": 1000.0}
    assert child["policy"].product_code == system["contract"].product_code
    assert child["policy"].original_commitment_policy == system["contract"].original_commitment_policy
    assert system["factory"](child["policy"]).query("policy", {"order_id": 30})["records"][0]["fields"]["budget_cap"] == 800
    assert (before["store"] / "investigation/state.json").read_bytes() == parent_bytes
    assert not (child["store"] / "investigation/state.json").exists()
    assert confirm(cases, view) == receipt
    assert len(cases.get(view["case_id"], "owner")["episodes"]) == 2


def test_reply_and_unconfirmed_draft_have_no_authorization(system):
    view = proposed(system)
    state = read_json(system["cases"]._directory(view["case_id"]) / "case.json")
    assert state["confirmation"] is None and state["continuation"] is None
    assert len(state["episodes"]) == 1
    assert system["policy"].payload()["orders"]["30"]["budget_cap"] is None
    with pytest.raises(CaseError, match="explicit_confirmation_required"):
        confirm(system["cases"], view, confirmed=False)


def test_wrong_owner_cannot_read_reply_or_confirm(system):
    view = proposed(system)
    cases = system["cases"]
    with pytest.raises(CaseError, match="case_not_found"):
        cases.get(view["case_id"], "different")
    with pytest.raises(CaseError, match="case_not_found"):
        cases.submit_reply(view["case_id"], "different", operation_id="r", expected_revision=view["revision"], text="900")
    with pytest.raises(CaseError, match="case_not_found"):
        cases.confirm(view["case_id"], "different", operation_id="confirm", expected_revision=view["revision"],
            draft_sha256=view["draft"]["draft_sha256"], parent_policy_sha256=view["ticket"]["parent_policy_sha256"], confirmed=True)


def test_new_reply_invalidates_previous_confirmation(system):
    old = proposed(system)
    cases = system["cases"]
    receipt = cases.submit_reply(old["case_id"], "owner", operation_id="correction",
        expected_revision=old["revision"], text="Make that 900 USD for SO-30")
    with pytest.raises(CaseError, match="case_revision_conflict"):
        confirm(cases, old)
    new = cases.propose(old["case_id"], "owner", reply_id=receipt["reply_id"],
        proposal={"kind": "budget_authorization_proposal", "order_id": "30", "amount": 900, "currency": "USD"})
    assert new["draft"]["draft_sha256"] != old["draft"]["draft_sha256"]
    confirmation = confirm(cases, new)
    child = cases.episode_config(old["case_id"], "owner", confirmation["child_episode"])
    assert child["policy"].budgets["30"] == 900


@pytest.mark.parametrize("kind", ["ambiguous", "price_fact", "out_of_scope", "cancel", "pause"])
def test_non_authorization_interpretations_never_confirm(system, kind):
    view = waiting(system)
    cases = system["cases"]
    text = {"cancel": "Cancel this case.", "pause": "Pause for now."}.get(kind, "The quote is 800")
    reply = cases.submit_reply(view["case_id"], "owner", operation_id="reply", expected_revision=view["revision"], text=text)
    view = cases.propose(view["case_id"], "owner", reply_id=reply["reply_id"], proposal={"kind": kind, "question": "Please specify your decision"})
    with pytest.raises(CaseError, match="case_not_waiting" if kind == "cancel" else "draft_not_confirmable"):
        confirm(cases, view)
    assert len(cases.get(view["case_id"], "owner")["episodes"]) == 1


@pytest.mark.parametrize("field,value,error", [("order_id", "31", "wrong_budget_order"),
    ("currency", "EUR", "wrong_budget_currency"), ("amount", True, "invalid_budget_amount"),
    ("amount", 0, "invalid_budget_amount"), ("amount", "0.009", "invalid_budget_amount"),
    ("amount", "NaN", "invalid_budget_amount"), ("margin", 0.1, "invalid_reply_proposal")])
def test_typed_draft_boundary(system, field, value, error):
    view = waiting(system)
    cases = system["cases"]
    reply = cases.submit_reply(view["case_id"], "owner", operation_id="r", expected_revision=view["revision"], text="800 USD")
    proposal = {"kind": "budget_authorization_proposal", "order_id": "30", "amount": "800", "currency": "USD", field: value}
    with pytest.raises(CaseError, match=error):
        cases.propose(view["case_id"], "owner", reply_id=reply["reply_id"], proposal=proposal)


def test_confirmation_operation_payload_is_immutable(system):
    view = proposed(system)
    confirm(system["cases"], view)
    with pytest.raises(CaseError, match="operation_input_conflict"):
        confirm(system["cases"], view, draft_sha256="f" * 64)
    with pytest.raises(CaseError, match="case_revision_conflict"):
        confirm(system["cases"], view, operation_id="racing-confirmation")


@pytest.mark.parametrize("change", ["generation", "currency", "price", "scope", "origin"])
def test_changes_between_question_and_confirmation_block(system, change):
    view = proposed(system)
    if change == "generation":
        system["environment"]["generation"] = "new-generation"
    elif change == "currency":
        system["environment"]["currency"] = "EUR"
    elif change == "price":
        source_record(system["source"], "purchase_order_line", 200)["fields"]["unit_price"] += 1
    elif change == "scope":
        source_record(system["source"], "sales_order", 31)["fields"]["state"] = "cancel"
    else:
        source_record(system["source"], "purchase_order", 20)["fields"]["origin"] = "different"
    with pytest.raises((CaseError, IntakeError)):
        confirm(system["cases"], view)
    assert len(system["cases"].get(view["case_id"], "owner")["episodes"]) == 1


def test_recovery_materializes_same_child_after_interruption(system, monkeypatch):
    view = proposed(system)
    cases = system["cases"]
    original = cases._materialize
    monkeypatch.setattr(cases, "_materialize", lambda state: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        confirm(cases, view)
    saved = cases.get(view["case_id"], "owner")
    assert len(saved["episodes"]) == 2 and saved["confirmation"]["after"] == "800.00"
    monkeypatch.setattr(cases, "_materialize", original)
    receipt = confirm(cases, view)
    child = cases.episode_config(view["case_id"], "owner", receipt["child_episode"])
    assert read_json(child["policy_file"])["budgets"]["30"] == 800
    assert cases.get(view["case_id"], "owner")["revision"] == saved["revision"]


def test_parent_store_tampering_rejects_ticket(system):
    cases, view = system["cases"], system["view"]
    config = cases.episode_config(view["case_id"], "owner", view["current_episode"])
    path = config["store"] / "investigation/state.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"run_id": config["run_id"], "policy_sha256": "other", "event_sha256": EVENT["sha256"]}))
    with pytest.raises(CaseError, match="episode_binding_changed"):
        cases.record_terminal(view["case_id"], "owner", config["run_id"])


def test_template_expiry_cannot_be_dropped_or_extended(system):
    template = task_template(system["contract"])
    template["fields"]["expires_at_utc"] = "1970-01-01T00:01:00+00:00"
    with pytest.raises(IntakeError, match="policy_expired"):
        inspect_intake(system["factory"](system["policy"]), template=template,
            policy=system["policy"], event=EVENT, now=100)
    del template["fields"]["expires_at_utc"]
    with pytest.raises(IntakeError, match="invalid_task_template"):
        inspect_intake(system["factory"](system["policy"]), template=template,
            policy=system["policy"], event=EVENT, now=100)


def test_one_environment_one_active_chain(system):
    with pytest.raises(CaseError, match="environment_chain_active"):
        system["cases"].create("owner", operation_id="other", template=task_template(system["contract"]),
            policy=system["policy"], event=EVENT)


def test_case_budget_reservation_idempotence_unknown_and_episode_once(system):
    cases, view = system["cases"], system["view"]
    allowance = {k: 1 for k in ("tokens", "seconds", "queries", "validations", "prepares")}
    first = cases.reserve_work(view["case_id"], "owner", operation_id="worker", kind="parent", allowance=allowance)
    assert cases.get(view["case_id"], "owner")["usage"]["tokens"] is None
    assert cases.reserve_work(view["case_id"], "owner", operation_id="worker", kind="parent", allowance=allowance) == first
    with pytest.raises(CaseError, match="worker_still_active"):
        cases.reserve_work(view["case_id"], "owner", operation_id="other", kind="parent", allowance=allowance)
    cases.finish_work(view["case_id"], "owner", operation_id="worker", usage={**allowance, "tokens": None})
    with pytest.raises(CaseError, match="case_usage_unknown"):
        cases.reserve_work(view["case_id"], "owner", operation_id="other", kind="parent", allowance=allowance)
    assert cases.get(view["case_id"], "owner")["usage_complete"] is False
    assert cases.get(view["case_id"], "owner")["usage"]["tokens"] is None
    assert cases.get(view["case_id"], "owner")["known_usage"]["tokens"] == 0


def test_budget_overshoot_recorded_and_cannot_redispatch_episode(system):
    cases, view = system["cases"], system["view"]
    allowance = {k: 1 for k in ("tokens", "seconds", "queries", "validations", "prepares")}
    cases.reserve_work(view["case_id"], "owner", operation_id="w", kind="parent", allowance=allowance)
    cases.finish_work(view["case_id"], "owner", operation_id="w", usage={**allowance, "tokens": 6_000_001})
    assert cases.get(view["case_id"], "owner")["usage"]["tokens"] == 6_000_001
    with pytest.raises(CaseError, match="episode_already_dispatched"):
        cases.reserve_work(view["case_id"], "owner", operation_id="w2", kind="parent", allowance=allowance)


def test_budget_capacity_is_explicit_and_persisted(system):
    assert system["view"]["limits"] == asdict(CaseLimits())
    view = proposed(system)
    data = read_json(system["cases"]._directory(view["case_id"]) / "case.json")
    assert data["template"] == task_template(system["contract"])
    assert "budgets" not in data["template"]["fields"]
    copy_of_template = copy.deepcopy(data["template"])
    del copy_of_template["fields"]["original_commitment_policy"]
    with pytest.raises(IntakeError, match="invalid_task_template"):
        inspect_intake(system["factory"](system["policy"]), template=copy_of_template,
            policy=system["policy"], event=EVENT, now=100)
