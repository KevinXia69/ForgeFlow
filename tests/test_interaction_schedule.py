"""Finite frozen layouts and host-only input/usage boundaries, without a model."""
import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

from forgeflow.recovery.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
evaluator = importlib.import_module("interaction_evaluate")
schedule = importlib.import_module("interaction_schedule")


def test_formal_layout_is_exactly_sixty_components_eighteen_chains():
    rows = schedule.layout("m4-formal", [f"c-{i}" for i in range(12)], [f"k-{i}" for i in range(6)])
    assert len(rows) == 78
    assert len({r["attempt_id"] for r in rows}) == 78
    assert {(layer, arm): sum(r["layer"] == layer and r["arm"] == arm for r in rows)
            for layer in ("component", "chain") for arm in evaluator.ARMS} == {
                ("component", "rules"): 12, ("component", "fixed"): 24, ("component", "interactive"): 24,
                ("chain", "rules"): 6, ("chain", "fixed"): 6, ("chain", "interactive"): 6}


def test_development_layout_and_high_budgets():
    rows = schedule.layout("m4-dev-r1", list(map(str, range(12))), ["one", "two"], development=True)
    assert len(rows) == 42
    assert sum(row["layer"] == "component" and row["arm"] != "rules" for row in rows) == 24
    assert sum(row["layer"] == "chain" and row["arm"] != "rules" for row in rows) == 4
    config = evaluator.configuration()
    assert config["component_allowance"]["tokens"] == 2_000_000
    assert config["component_allowance"]["seconds"] == 1800
    assert config["case_limits"]["tokens"] == 6_000_000
    assert config["case_limits"]["seconds"] == 10800
    assert config["provider_response_caps"] == {"fixed": 1, "interactive": 8}


def test_development_definitions_are_independent_and_visible_input_is_whitelisted():
    for layer in ("component", "chain"):
        suite = json.loads((ROOT / f"data/interaction-semantics/{layer}-development-v1.json").read_text())
        schedule.validate_suite(suite, layer=layer, split="development")
        assert suite["authoring"]["gold_from_production_parser"] is False
    case = json.loads((ROOT / "data/interaction-semantics/component-development-v1.json").read_text())["cases"][0]
    sentinel = copy.deepcopy(case)
    sentinel["gold"]["hidden_future_answer"] = "DO_NOT_LEAK_THIS_GOLD"
    assert "DO_NOT_LEAK_THIS_GOLD" not in json.dumps(evaluator.visible_input(sentinel))
    assert set(evaluator.visible_input(sentinel)) == {"ticket", "currency", "history", "reply", "reply_contract_version"}


def test_unaccounted_worker_never_becomes_zero_total(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    atomic_json(first / "provider-usage.json", {"tokens": 125, "messages": [{"usage": {"input": 100, "output": 25, "cacheRead": 0, "cacheWrite": 0}}]})
    result = evaluator.usage_summary([("first", first, True, None), ("second", second, True, None)])
    assert result["tokens"] is None and result["known_token_prefix"] == 125
    assert result["unknown_workers"] == ["second"]
    assert result["known_usage_by_field"]["input"] == 100


def test_unstarted_snapshot_retains_both_plan_denominators(tmp_path):
    jobs = schedule.layout("test", ["c"], ["k"])
    report = schedule.snapshot({"jobs": jobs}, tmp_path)
    assert report["component"]["planned_denominator"] == 5
    assert report["chain"]["planned_denominator"] == 3
    assert report["component"]["outcomes"]["not_started"] == 5


def test_wrong_scope_transition_is_not_silently_accepted():
    suite = json.loads((ROOT / "data/interaction-semantics/chain-development-v1.json").read_text())
    suite["cases"][0]["nodes"][0]["then"] = {"confirm": True}
    with pytest.raises(ValueError, match="chain_transition_position_invalid"):
        schedule.validate_suite(suite, layer="chain", split="development")


def test_missing_child_is_not_a_false_policy_change():
    policy = {"kind": "partial", "orders": {"1": {"budget_cap": None}}}
    case = {"missing_order_id": 1, "expected": {"terminal": "prepared", "budget": "100.00"}, "contract": {}}
    final = {"episodes": {"parent": {"parent_episode": None, "policy": policy}}, "work": {}, "confirmation": None}
    class Connection:
        write_attempts = []
        def fingerprint(self):
            return "before"
    result = evaluator.chain_grade(case, policy, final, Connection(), "before", {}, False, {"code": "parent_failed"})
    assert result["checks"]["parent_policy_unchanged"] is True
    assert result["checks"]["unchanged_contract_fields"] is None
    assert result["checks"]["authorized_budget"] is None


def test_development_round_two_cap_is_not_an_unlimited_retry():
    jobs = schedule.layout("r1", [f"c{i}" for i in range(12)], ["k1", "k2"], development=True)
    first = {"split": "development", "development_round": 1, "jobs": jobs}
    second = {"split": "development", "jobs": schedule.layout("r2", [f"c{i}" for i in range(12)], ["k1", "k2"], development=True)}
    selection = [{k: job[k] for k in ("layer", "case_id", "arm")} for job in jobs if job["layer"] == "component" and job["arm"] == "fixed"]
    assert len(schedule.round_two_jobs(second, first, selection)) == 12
    selection.append({"layer": "component", "case_id": "c0", "arm": "interactive"})
    with pytest.raises(ValueError, match="round_two_component_model_limit"):
        schedule.round_two_jobs(second, first, selection)
    with pytest.raises(ValueError, match="first_development_round"):
        schedule.round_two_jobs(second, {**first, "development_round": 2}, [])


def test_rule_exception_is_observed_failure_not_unknown_provider_cost(tmp_path):
    case = json.loads((ROOT / "data/interaction-semantics/component-development-v1.json").read_text())["cases"][0]
    class BrokenRule:
        def reply(self, **kwargs):
            assert "gold" not in kwargs and kwargs["reply_contract_version"] == 2
            raise ValueError("known_local_rule_failure")
    result = evaluator.run_component(case, arm="rules", output=tmp_path / "attempt", worker_factory=BrokenRule)
    assert result["status"] == "observed_failure"
    assert result["usage"]["tokens"] == 0 and result["usage"]["complete"] is True


@pytest.mark.parametrize("phase,service,count,outcome,expected", [
    ("reserved", "running", 0, None, "unknown"),
    ("unknown", "technical_failure", 0, None, "unknown"),
    ("complete", "technical_failure", 0, None, "not_reached"),
    ("complete", "technical_failure", 1, "infeasible", "observed_failure"),
    ("complete", "prepared", 1, "prepared", "success"),
    ("unknown", "prepared", 1, "prepared", "success"),
])
def test_actual_chain_classifier_uses_host_attempt_and_completion_evidence(phase, service, count, outcome, expected):
    state = {"status": service, "usage": {"prepares": count}, "attempts": [] if not count else [{"result": outcome}],
             "prepared": {"plan_id": "bound-plan"} if service == "prepared" else None}
    stages = evaluator.classify_child_stages(state=state, work={"phase": phase},
        receipt={"runtime_valid": True}, episode={"status": service})
    assert stages["prepare"]["status"] == expected
    if phase != "complete":
        assert stages["child"]["status"] == "unknown"


def test_fixture_preflight_rejects_unsupported_intake_before_solver(tmp_path, monkeypatch):
    preflight = importlib.import_module("interaction_preflight")
    case = json.loads((ROOT / "data/interaction-semantics/chain-development-v2.json").read_text())["cases"][0]
    monkeypatch.setattr(preflight.service, "prepare", lambda *a, **kw: pytest.fail("Intake must reject before solver"))
    with pytest.raises(ValueError, match="intake_event_unproven"):
        preflight.run(case, tmp_path / "preflight")


def test_native_schema_rejected_proposal_remains_in_raw_evidence(tmp_path):
    ticket = {"order_id": "317", "order_name": "SO-317"}
    text = "I authorize 800 USD for SO-317."
    valid = {"schema_version": 2, "kind": "budget_authorization_proposal", "order_id": "317", "amount": "800.00", "currency": "USD",
             "source_spans": [{"reply_id": "current", "start": 0, "end": len(text)}]}
    malformed = {**valid, "kind": "budget_authorization_proposal\n</parameter>"}
    rows = []
    for identity, raw, bad in (("bad", malformed, True), ("good", valid, False)):
        rows.append({"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Private diagnostic text must not become a proposal."},
            {"type": "toolCall", "id": identity, "name": "submit_reply_proposal", "arguments": raw}]}})
        rows.append({"type": "tool_execution_end", "toolCallId": identity, "toolName": "submit_reply_proposal", "isError": bad,
                     "result": {"content": [{"type": "text", "text": "Validation failed for tool" if bad else "accepted"}]}})
    raw_bytes = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    (tmp_path / "pi.jsonl").write_bytes(raw_bytes)
    atomic_json(tmp_path / "tool-trace.json", [{"request": {"action": "proposal", "payload": valid}, "result": {"accepted": True}}])
    assert evaluator.raw_proposals(tmp_path) == [malformed, valid]
    gold = {"allowed_actions": [{"kind": "budget_authorization_proposal", "order_id": "317", "amount": "800.00", "currency": "USD"}]}
    audits = evaluator.admission_audits(tmp_path, gold, ticket, "USD", [{"reply_id": "current", "text": text}], "current")
    assert len(audits) == 2
    assert audits[0]["native_schema_rejected"] is True and audits[0]["host_reached"] is False
    assert audits[0]["model_error_blocked"] is True and audits[0]["host_false_rejection"] is False
    assert audits[1]["host_accepted"] is True and audits[1]["native_schema_rejected"] is False
    assert (tmp_path / "pi.jsonl").read_bytes() == raw_bytes


def test_incomplete_native_log_does_not_erase_durable_host_proposal(tmp_path):
    raw = {"kind": "pause"}
    (tmp_path / "pi.jsonl").write_text('{"partial":')
    atomic_json(tmp_path / "tool-trace.json", [{"request": {"action": "proposal", "payload": raw}, "result": {"accepted": True}}])
    assert evaluator.raw_proposals(tmp_path) == [raw]
    assert evaluator.proposal_observations(tmp_path)[0]["origin"] == "host_bridge_without_native_match"
