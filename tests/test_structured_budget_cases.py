"""Structured decisions exercise the real aggregate, intake and policy compiler."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from investigation_support import EVENT, make_case, source_record
from test_investigation_continuation import confirm, partial, waiting

from forgeflow.investigation.cases import CaseError, InvestigationCases
from forgeflow.investigation.intake import IntakeError, task_template
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.sources import FixtureSource
from forgeflow.recovery.storage import StorageError, atomic_json, read_json


def make_structured_system(tmp_path, *, mode="structured_budget_v1", operation="create", expiry=None):
    source, contract, request = make_case((30, 31))
    policy = partial(contract)
    environment = {"generation": "erp-structured-1", "currency": "USD", "time": 100}

    def factory(p):
        return FixtureSource(source.records, p.source_policy() if isinstance(p, PartialPolicy) else p)

    cases = InvestigationCases(tmp_path / "cases", source_factory=factory,
        generation=lambda: environment["generation"], clock=lambda: environment["time"],
        currency_probe=lambda _: {"currency": "USD", "company_id": 1, "provenance": "structured-fixture",
            "record_currencies": {"orders-and-offers": environment["currency"]}})
    template = task_template(contract)
    if expiry is not None:
        template["fields"]["expires_at_utc"] = expiry
    kwargs = {} if mode is None else {"decision_input_mode": mode}
    view = cases.create("owner", operation_id=operation, template=template, policy=policy, event=EVENT, **kwargs)
    return {"cases": cases, "view": view, "source": source, "contract": contract,
            "request": request, "policy": policy, "factory": factory, "environment": environment}


@pytest.fixture
def system(tmp_path):
    system = make_structured_system(tmp_path)
    system["view"] = waiting(system)
    return system


def submit(system, *, amount="800", operation="budget", view=None, **overrides):
    view = view or system["cases"].get(system["view"]["case_id"], "owner")
    args = {"operation_id": operation, "expected_revision": view["revision"],
            "ticket_id": view["ticket"]["ticket_id"], "amount": amount}
    args.update(overrides)
    return system["cases"].submit_budget(view["case_id"], "owner", **args)


def control(system, action, *, operation=None, view=None):
    view = view or system["cases"].get(system["view"]["case_id"], "owner")
    return system["cases"].budget_control(view["case_id"], "owner", action=action,
        operation_id=operation or action, expected_revision=view["revision"])


def state_path(system):
    return system["cases"]._directory(system["view"]["case_id"]) / "case.json"


def current(system):
    return system["cases"].get(system["view"]["case_id"], "owner")


def test_candidate_is_a_typed_authenticated_input_not_a_reply_or_authorization(system, monkeypatch):
    from forgeflow.investigation import clarification, clarification_v2

    def forbidden(*args, **kwargs):
        pytest.fail("structured budget called a language interpreter")

    for parser in (clarification, clarification_v2):
        monkeypatch.setattr(parser, "interpret_reply", forbidden)
        monkeypatch.setattr(parser, "validate_proposal", forbidden)
    business = copy.deepcopy(system["source"].records)
    before = read_json(state_path(system))
    receipt = submit(system, amount="800.1")
    view = current(system)
    assert view["decision_input_mode"] == "structured_budget_v1" and view["decision_contract_version"] == 1
    assert view["draft"]["proposal"] == {"kind": "budget_authorization_proposal", "order_id": "30",
                                         "amount": "800.10", "currency": "USD"}
    assert view["draft"]["source"] == "structured_budget" and "reply_id" not in view["draft"]
    assert view["budget_inputs"][0]["raw_input"] == {"ticket_id": view["ticket"]["ticket_id"], "amount": "800.1"}
    assert view["budget_inputs"][0]["actor"] == "owner"
    assert view["budget_inputs"][0]["source"] == "authenticated_user"
    assert receipt["input_id"] == view["draft"]["input_id"]
    assert not view["replies"] and view["confirmation"] is None and len(view["episodes"]) == 1
    after = read_json(state_path(system))
    assert after["parent_policy"] == before["parent_policy"] and after["work"] == before["work"] == {}
    assert after["continuation"] is None and system["source"].records == business


@pytest.mark.parametrize("amount,canonical", [("1", "1.00"), ("1.2", "1.20"), ("1.23", "1.23"),
    ("0.01", "0.01"), ("1000000000000", "1000000000000.00"), ("999999999999.99", "999999999999.99")])
def test_exact_decimal_boundary_accepts_only_the_stated_amount(system, amount, canonical):
    submit(system, amount=amount)
    assert current(system)["draft"]["proposal"]["amount"] == canonical


@pytest.mark.parametrize("amount", [800, 800.0, True, None, "", " 800", "800 ", "+800", "-800", "0", "0.00",
    "0.001", "1.230", "1e3", "1E3", "NaN", "Infinity", "1,000", "1_000", "１００", "١٠", "00.10", "01",
    "1.", ".1", "1000000000000.01", "9" * 500])
def test_invalid_decimal_never_mutates_case(system, amount):
    before = state_path(system).read_bytes()
    with pytest.raises(CaseError, match="invalid_budget_amount"):
        submit(system, amount=amount)
    assert state_path(system).read_bytes() == before


def test_structured_confirmation_changes_only_missing_cap_and_records_source(system):
    parent = current(system)
    cases = system["cases"]
    config = cases.episode_config(parent["case_id"], "owner", parent["current_episode"])
    terminal_path = config["store"] / "investigation/state.json"
    original = terminal_path.read_bytes()
    business = copy.deepcopy(system["source"].records)
    submit(system)
    view = current(system)
    receipt = confirm(cases, view)
    saved = current(system)
    child = cases.episode_config(view["case_id"], "owner", receipt["child_episode"])
    assert child["policy"].budgets == {"30": 800.0, "31": 1000.0}
    assert saved["confirmation"]["input_id"] == view["draft"]["input_id"]
    assert saved["confirmation"]["source"] == "structured_budget"
    assert saved["confirmation"]["decision_input_mode"] == "structured_budget_v1"
    assert saved["confirmation"]["decision_contract_version"] == 1
    assert saved["budget_inputs"][0]["status"] == "confirmed"
    assert "reply_id" not in saved["confirmation"]
    assert child["store"] != config["store"] and not (child["store"] / "investigation/state.json").exists()
    assert terminal_path.read_bytes() == original and system["source"].records == business
    assert confirm(cases, view) == receipt and len(current(system)["episodes"]) == 2


def test_correction_and_withdrawal_retain_original_input_and_invalidate_candidate(system):
    submit(system)
    old = current(system)
    second = submit(system, amount="900", operation="correction")
    changed = current(system)
    assert changed["budget_inputs"][0]["raw_input"]["amount"] == "800"
    assert changed["budget_inputs"][0]["status"] == "invalidated"
    assert changed["budget_inputs"][0]["invalidation_reason"] == "replaced"
    assert changed["budget_inputs"][0]["superseded_by_input_id"] == second["input_id"]
    assert changed["draft_history"][0]["draft_sha256"] == old["draft"]["draft_sha256"]
    with pytest.raises(CaseError, match="draft_not_confirmable"):
        confirm(system["cases"], changed, draft_sha256=old["draft"]["draft_sha256"])
    control(system, "withdraw")
    withdrawn = current(system)
    assert withdrawn["status"] == "awaiting_human" and withdrawn["draft"] is None
    assert withdrawn["budget_inputs"][-1]["invalidation_reason"] == "withdraw"
    assert withdrawn["confirmation"] is None and len(withdrawn["episodes"]) == 1
    submit(system, amount="850", operation="replacement")
    confirm(system["cases"], current(system))
    assert current(system)["confirmation"]["after"] == "850.00"


def test_pause_and_resume_keep_expiry_usage_and_require_fresh_input(system):
    submit(system)
    old = current(system)
    before = read_json(state_path(system))
    receipt = control(system, "pause")
    assert receipt["decision_paused"] is True
    paused = current(system)
    assert paused["status"] == "awaiting_human" and paused["draft"] is None
    with pytest.raises(CaseError, match="case_decision_paused"):
        submit(system, operation="during-pause")
    with pytest.raises(CaseError, match="case_decision_paused"):
        confirm(system["cases"], old, expected_revision=paused["revision"])
    control(system, "resume")
    resumed = current(system)
    assert not resumed["decision_paused"] and resumed["draft"] is None
    after = read_json(state_path(system))
    assert before["template"] == after["template"] and before["work"] == after["work"]
    assert before["limits"] == after["limits"] and len(resumed["episodes"]) == 1
    with pytest.raises(CaseError, match="draft_not_confirmable"):
        confirm(system["cases"], old, expected_revision=resumed["revision"])
    submit(system, operation="fresh", amount="850")
    confirm(system["cases"], current(system))


@pytest.mark.parametrize("action", ["submit", "resume", "confirm"])
@pytest.mark.parametrize("change", ["currency", "facts", "generation", "version"])
def test_context_changes_block_new_structured_decisions(system, action, change):
    if action in {"resume", "confirm"}:
        submit(system)
    if action == "resume":
        control(system, "pause")
    view = current(system)
    if change == "currency":
        system["environment"]["currency"] = "EUR"
    elif change == "facts":
        source_record(system["source"], "purchase_order_line", 200)["fields"]["unit_price"] += 1
    elif change == "generation":
        system["environment"]["generation"] = "new-generation"
    else:
        state = read_json(state_path(system))
        state["decision_contract_version"] = 2
        atomic_json(state_path(system), state, replace=True)
    before = state_path(system).read_bytes()
    with pytest.raises((CaseError, IntakeError)):
        if action == "submit":
            submit(system, operation="changed")
        elif action == "resume":
            control(system, "resume")
        else:
            confirm(system["cases"], view)
    assert state_path(system).read_bytes() == before


def test_old_completed_receipts_survive_expiry_and_version_change(system):
    original = current(system)
    candidate_receipt = submit(system, view=original)
    candidate = current(system)
    decision_receipt = confirm(system["cases"], candidate)
    state = read_json(state_path(system))
    state["decision_contract_version"] = 99
    atomic_json(state_path(system), state, replace=True)
    system["environment"]["time"] = 99999999999
    assert submit(system, view=original) == candidate_receipt
    assert confirm(system["cases"], candidate) == decision_receipt
    with pytest.raises(CaseError, match="operation_input_conflict"):
        submit(system, view=original, amount="800.00")


def test_confirmation_is_durable_before_materialization_and_recovery_is_unique(system, monkeypatch):
    submit(system)
    view = current(system)
    cases = system["cases"]
    materialize = cases._materialize
    monkeypatch.setattr(cases, "_materialize", lambda _: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        confirm(cases, view)
    assert len(current(system)["episodes"]) == 2 and current(system)["confirmation"]["after"] == "800.00"
    monkeypatch.setattr(cases, "_materialize", materialize)
    first = confirm(cases, view)
    assert confirm(cases, view) == first and len(current(system)["episodes"]) == 2


def test_concurrent_confirmation_keeps_one_policy_and_child(system):
    submit(system)
    view = current(system)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, system["cases"], view) for _ in range(2)]
        receipts = []
        for future in futures:
            try:
                receipts.append(future.result())
            except StorageError as error:
                assert str(error) == "another_recovery_operation_running"
    # A nonblocking aggregate lock may reject the simultaneous request. After
    # the first commit its unchanged operation retrieves the original receipt.
    receipts.append(confirm(system["cases"], view))
    assert receipts[0] == receipts[1] and len(current(system)["episodes"]) == 2


@pytest.mark.parametrize("action", ["pause", "withdraw", "cancel"])
def test_confirm_racing_control_has_one_legal_winner(system, action):
    submit(system)
    view = current(system)

    def confirm_attempt():
        return confirm(system["cases"], view)

    def control_attempt():
        if action == "cancel":
            return system["cases"].request_closure(view["case_id"], "owner", operation_id="cancel",
                                                  expected_revision=view["revision"])
        return control(system, action, view=view)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fn) for fn in (confirm_attempt, control_attempt)]
        successes, errors = [], []
        for future in futures:
            try:
                successes.append(future.result())
            except (CaseError, StorageError) as error:
                errors.append(str(error))
    assert len(successes) == 1 and errors[0] in {"case_revision_conflict", "another_recovery_operation_running"}
    result = current(system)
    assert len(result["episodes"]) == (2 if result["confirmation"] else 1)
    if not result["confirmation"]:
        assert result["status"] == "closing" if action == "cancel" else result["draft"] is None
        loser = confirm_attempt
    else:
        loser = control_attempt
    with pytest.raises(CaseError, match="case_revision_conflict"):
        loser()


def test_resume_cannot_reopen_cancelled_case(system):
    control(system, "pause")
    view = current(system)
    system["cases"].request_closure(view["case_id"], "owner", operation_id="cancel", expected_revision=view["revision"])
    with pytest.raises(CaseError, match="case_closing"):
        control(system, "resume")
    assert current(system)["status"] == "closing"


def test_structured_mode_rejects_all_language_callbacks_and_reply_work(system):
    view, cases = current(system), system["cases"]
    before = state_path(system).read_bytes()
    callbacks = [
        lambda: cases.submit_reply(view["case_id"], "owner", operation_id="reply", expected_revision=view["revision"], text="800"),
        lambda: cases.propose(view["case_id"], "owner", reply_id="fake", proposal={}),
        lambda: cases.record_reply_fallback(view["case_id"], "owner", reply_id="fake"),
        lambda: cases.reserve_work(view["case_id"], "owner", operation_id="parser", kind="reply",
                                   allowance={key: 1 for key in ("tokens", "seconds", "queries", "validations", "prepares")}),
    ]
    for callback in callbacks:
        with pytest.raises(CaseError, match="structured_budget_required"):
            callback()
    assert state_path(system).read_bytes() == before


def test_legacy_create_bytes_and_hashes_are_unchanged_and_do_not_migrate(tmp_path):
    implicit = make_structured_system(tmp_path / "implicit", mode=None)
    explicit = make_structured_system(tmp_path / "explicit", mode="legacy_reply")
    assert state_path(implicit).read_bytes() == state_path(explicit).read_bytes()
    before = state_path(implicit).read_bytes()
    view = current(implicit)
    assert view["decision_input_mode"] == "legacy_reply" and view["decision_contract_version"] is None
    assert view["budget_inputs"] == [] and view["decision_paused"] is False
    assert state_path(implicit).read_bytes() == before
    with pytest.raises(CaseError, match="operation_input_conflict"):
        implicit["cases"].create("owner", operation_id="create", template=task_template(implicit["contract"]),
            policy=implicit["policy"], event=EVENT, decision_input_mode="structured_budget_v1")
    implicit["view"] = waiting(implicit)
    with pytest.raises(CaseError, match="structured_budget_mode_required"):
        submit(implicit)


def test_ticket_owner_extra_fields_and_stale_revision_do_not_mutate(system):
    view, cases = current(system), system["cases"]
    before = state_path(system).read_bytes()
    with pytest.raises(CaseError, match="ticket_binding_changed"):
        submit(system, ticket_id="ticket-other")
    with pytest.raises(CaseError, match="case_not_found"):
        cases.submit_budget(view["case_id"], "other-owner", operation_id="wrong-owner",
            expected_revision=view["revision"], ticket_id=view["ticket"]["ticket_id"], amount="800")
    with pytest.raises(CaseError, match="case_revision_conflict"):
        submit(system, expected_revision=view["revision"] - 1)
    with pytest.raises(TypeError):
        submit(system, currency="EUR")
    assert state_path(system).read_bytes() == before


@pytest.mark.parametrize("action", ["withdraw", "pause", "resume"])
def test_candidate_controls_cannot_revoke_confirmed_policy(system, action):
    submit(system)
    confirm(system["cases"], current(system))
    before = state_path(system).read_bytes()
    with pytest.raises(CaseError, match="case_not_waiting"):
        control(system, action)
    assert state_path(system).read_bytes() == before


def test_resume_racing_cancel_cannot_reopen_after_closure(system):
    control(system, "pause")
    view = current(system)
    cases = system["cases"]

    def resume():
        return control(system, "resume", view=view)

    def cancel():
        return cases.request_closure(view["case_id"], "owner", operation_id="cancel",
                                     expected_revision=view["revision"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fn) for fn in (resume, cancel)]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except (CaseError, StorageError) as error:
                assert str(error) in {"case_revision_conflict", "another_recovery_operation_running"}
    assert len(results) == 1 and len(current(system)["episodes"]) == 1
    if current(system)["closure_intent"]:
        with pytest.raises(CaseError, match="case_closing"):
            control(system, "resume", operation="later-resume")
    else:
        assert current(system)["decision_paused"] is False and current(system)["draft"] is None
        with pytest.raises(CaseError, match="case_revision_conflict"):
            cancel()


@pytest.mark.parametrize("phase,error", [("reserved", "worker_still_active"), ("unknown", "case_usage_unknown")])
def test_pausing_does_not_hide_persisted_active_or_unknown_work(system, phase, error):
    state = read_json(state_path(system))
    state["work"]["original-worker"] = {"kind": "parent", "phase": phase, "usage": {},
        "allowance": {key: 1 for key in ("tokens", "seconds", "queries", "validations", "prepares")}}
    atomic_json(state_path(system), state, replace=True)
    before = state_path(system).read_bytes()
    with pytest.raises(CaseError, match=error):
        control(system, "pause")
    assert state_path(system).read_bytes() == before


def test_original_expiry_blocks_resume_and_confirmation_without_get_mutation(tmp_path):
    system = make_structured_system(tmp_path, expiry="1970-01-01T00:03:20+00:00")
    system["view"] = waiting(system)
    submit(system)
    candidate = current(system)
    control(system, "pause")
    system["environment"]["time"] = 201
    before = state_path(system).read_bytes()
    assert current(system)["policy_expired"] is True and state_path(system).read_bytes() == before
    with pytest.raises(IntakeError, match="policy_expired"):
        control(system, "resume")
    with pytest.raises(IntakeError, match="policy_expired"):
        confirm(system["cases"], candidate, expected_revision=current(system)["revision"])
    assert state_path(system).read_bytes() == before
    view = current(system)
    system["cases"].request_closure(view["case_id"], "owner", operation_id="expiry-close",
                                    expected_revision=view["revision"], reason="policy_expired")
    assert current(system)["status"] == "closing"


@pytest.mark.parametrize("changed", ["input", "proposal", "source"])
def test_confirmation_rechecks_structured_input_not_only_displayed_hash(system, changed):
    submit(system)
    view = current(system)
    state = read_json(state_path(system))
    if changed == "input":
        state["budget_inputs"][-1]["raw_input"]["amount"] = "900"
    elif changed == "proposal":
        state["draft"]["proposal"]["amount"] = "900.00"
    else:
        state["draft"]["source"] = "reply"
    atomic_json(state_path(system), state, replace=True)
    before = state_path(system).read_bytes()
    with pytest.raises(CaseError, match="draft_not_confirmable"):
        confirm(system["cases"], view)
    assert state_path(system).read_bytes() == before
