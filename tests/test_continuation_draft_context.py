"""Identical amounts and policies do not make different cases interchangeable."""
import copy
import hashlib

import pytest
from investigation_support import EVENT, make_case
from test_investigation_continuation import confirm, partial

from forgeflow.investigation.cases import CaseError, InvestigationCases
from forgeflow.investigation.event import trusted_event
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import FixtureSource
from forgeflow.recovery.storage import atomic_json, digest, read_json


def candidate(root, *, operation="create", generation="erp-a", suffix=""):
    source, contract, _ = make_case((30, 31))
    initial = copy.deepcopy(source.records)
    policy = partial(contract)
    text = EVENT["text"] + suffix
    event = trusted_event({"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()})

    def factory(p):
        return FixtureSource(source.records, p.source_policy() if isinstance(p, PartialPolicy) else p)

    cases = InvestigationCases(root, source_factory=factory, generation=lambda: generation,
        currency_probe=lambda _: {"currency": "USD", "company_id": 1, "provenance": "explicit-fixture",
            "record_currencies": {"orders-and-offers": "USD"}}, clock=lambda: 100)
    view = cases.create("owner", operation_id=operation, template=task_template(contract), policy=policy, event=event)
    cfg = cases.episode_config(view["case_id"], "owner", view["current_episode"])
    service = InvestigationService(source=factory(policy), contract=policy, store=cfg["store"],
        bundle=cfg["store"] / "unused", connection_factory=lambda: None, event=event)
    service.dispatch(cfg["run_id"], "finish", {"status": "needs_clarification", "reason": "Missing budget",
                                              "question": "What budget do you authorize for SO-30?"})
    view = cases.record_terminal(view["case_id"], "owner", view["current_episode"])
    receipt = cases.submit_reply(view["case_id"], "owner", operation_id="answer",
        expected_revision=view["revision"], text="Budget 800 USD for SO-30.")
    view = cases.propose(view["case_id"], "owner", reply_id=receipt["reply_id"],
        proposal={"kind": "budget_authorization_proposal", "order_id": "30", "amount": "800.00", "currency": "USD"})
    assert source.records == initial
    return cases, view


@pytest.mark.parametrize("changes", [
    {"operation": "another-case"}, {"generation": "erp-b"},
    {"suffix": " Investigate affected demand."},
    {"operation": "another-case", "generation": "erp-b", "suffix": " Investigate affected demand."},
])
def test_foreign_case_confirmation_cannot_create_policy_or_child(tmp_path, changes):
    _, old = candidate(tmp_path / "a")
    cases, current = candidate(tmp_path / "b", **changes)
    assert old["draft"]["proposal"] == current["draft"]["proposal"]
    assert old["draft"]["reply_id"] == current["draft"]["reply_id"]
    assert old["ticket"]["parent_policy_sha256"] == current["ticket"]["parent_policy_sha256"]
    path = cases._directory(current["case_id"]) / "case.json"
    before = path.read_bytes()
    with pytest.raises(CaseError, match="draft_not_confirmable"):
        confirm(cases, current, draft_sha256=old["draft"]["draft_sha256"])
    assert path.read_bytes() == before
    assert len(current["episodes"]) == 1 and current["confirmation"] is None
    receipt = confirm(cases, current)
    assert confirm(cases, current) == receipt
    after = cases.get(current["case_id"], "owner")
    assert len(after["episodes"]) == 2 and after["confirmation"]["after"] == "800.00"


def test_legacy_unbound_candidate_requires_fresh_display_and_confirmation(tmp_path):
    cases, view = candidate(tmp_path)
    path = cases._directory(view["case_id"]) / "case.json"
    state = read_json(path)
    draft = state["draft"]
    draft.pop("context_sha256", None)
    draft.pop("draft_sha256")
    draft["draft_sha256"] = digest(draft)
    atomic_json(path, state, replace=True)
    legacy = cases.get(view["case_id"], "owner")
    before = path.read_bytes()
    with pytest.raises(CaseError, match="draft_not_confirmable"):
        confirm(cases, legacy)
    assert path.read_bytes() == before
    refreshed = cases.propose(view["case_id"], "owner", reply_id=view["draft"]["reply_id"],
                              proposal=view["draft"]["proposal"])
    assert refreshed["draft"]["draft_sha256"] != legacy["draft"]["draft_sha256"]
    assert confirm(cases, refreshed)["child_episode"] == view["case_id"] + "-e2"


@pytest.mark.parametrize("field", ["ticket_id", "facts_sha256", "parent_episode"])
def test_confirmation_rechecks_current_ticket_context(tmp_path, field):
    cases, view = candidate(tmp_path)
    path = cases._directory(view["case_id"]) / "case.json"
    state = read_json(path)
    state["ticket"][field] += "-changed"
    atomic_json(path, state, replace=True)
    before = path.read_bytes()
    with pytest.raises(CaseError, match="draft_not_confirmable"):
        confirm(cases, view)
    assert path.read_bytes() == before
