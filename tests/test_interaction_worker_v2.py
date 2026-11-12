"""Actual case/worker version seams with only a synthetic provider boundary."""
import copy
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_continuation_default_strategy import configured as _configured
from test_continuation_default_strategy import waiting_reply
from test_investigation_worker import identity, worker

from forgeflow.investigation import clarification_v2 as v2
from forgeflow.investigation import runtime
from forgeflow.investigation.cases import CaseError
from forgeflow.investigation.worker import reply_system_help
from forgeflow.recovery.storage import atomic_json, digest, read_json


@pytest.fixture
def configured(tmp_path, monkeypatch):
    return {**_configured.__wrapped__(tmp_path, monkeypatch), "decision_input_mode": "legacy_reply"}


def explicit_proposal(text, identity):
    return {"schema_version": 2, "kind": "budget_authorization_proposal", "order_id": "30",
            "amount": "800.00", "currency": "USD", "question": "Model-only suggestion",
            "source_spans": [{"reply_id": identity, "start": 0, "end": len(text)}]}


def test_legacy_reply_v2_default_and_explicit_v1_binding_is_unchanged(configured):
    _, host, _, _ = runtime.build_runtime(**configured, reply_contract_version=1)
    path = configured["root"] / "runtime-binding.json"
    original = path.read_bytes()
    assert "reply_contract_version" not in read_json(path)
    runtime.build_runtime(**configured, reply_contract_version=1)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="continuation_runtime_binding_changed"):
        runtime.build_runtime(**configured)
    assert path.read_bytes() == original
    # A new root binds v2 and cannot be reopened as legacy.
    options = {**configured, "root": configured["root"].parent / "v2"}
    _, newer, _, _ = runtime.build_runtime(**options)
    assert newer.create_inputs("owner")["reply_contract_version"] == 2
    with pytest.raises(ValueError, match="continuation_runtime_binding_changed"):
        runtime.build_runtime(**options, reply_contract_version=1)


def test_real_v2_case_proposal_confirmation_and_original_manifest(configured, monkeypatch):
    _, host, model, environment = runtime.build_runtime(**configured)
    case_id = waiting_reply(host, environment)
    calls = []

    def provider(**args):
        calls.append(args)
        visible = json.loads(args["prompt"])["visible_replies"]
        current = visible["replies"][-1]
        result = args["dispatch"]("proposal", explicit_proposal(current["text"], current["reply_id"]))
        assert result["accepted"] and result["host_question"] is None
        return {"seconds": 1, "runtime_valid": True}, {"tokens": 37}

    monkeypatch.setattr(model, "_model", provider)
    view = host._perform(host._job("reply", case_id, "owner", "answer"))
    assert len(calls) == 1 and view["draft"]["proposal"]["question"] == "Model-only suggestion"
    assert view["draft"]["host_question"] is None
    root = host.cases._directory(case_id)
    worker_path = root / "workers" / ("interpret-" + view["draft"]["reply_id"])
    claim = read_json(worker_path / "claim.json")
    visible = read_json(worker_path / "visible-replies.json")
    assert claim["reply_contract_version"] == 2
    assert claim["input"]["visible_manifest_sha256"] == digest(visible)
    assert digest(claim["input"]) == claim["input_sha256"]
    assert view["confirmation"] is None and len(view["episodes"]) == 1
    parent = (root / "episodes" / view["current_episode"] / "store/investigation/state.json").read_bytes()
    decision = host.cases.confirm(case_id, "owner", operation_id="confirm-v2", expected_revision=view["revision"],
        draft_sha256=view["draft"]["draft_sha256"], parent_policy_sha256=view["ticket"]["parent_policy_sha256"], confirmed=True)
    child = host.cases.episode_config(case_id, "owner", decision["child_episode"])
    assert child["policy"].budgets["30"] == 800
    assert (root / "episodes" / view["current_episode"] / "store/investigation/state.json").read_bytes() == parent
    assert host.cases.confirm(case_id, "owner", operation_id="confirm-v2", expected_revision=view["revision"],
        draft_sha256=view["draft"]["draft_sha256"], parent_policy_sha256=view["ticket"]["parent_policy_sha256"], confirmed=True) == decision


def test_rejected_model_is_separate_fallback_not_a_rules_repair(configured, monkeypatch):
    _, host, model, environment = runtime.build_runtime(**configured)
    case_id = waiting_reply(host, environment)

    def provider(**args):
        visible = json.loads(args["prompt"])["visible_replies"]
        current = visible["replies"][-1]
        proposal = explicit_proposal(current["text"], current["reply_id"])
        proposal["amount"] = "999.00"
        assert args["dispatch"]("proposal", proposal)["accepted"] is False
        return {"seconds": 1, "runtime_valid": True}, {"tokens": 18}

    monkeypatch.setattr(model, "_model", provider)
    view = host._perform(host._job("reply", case_id, "owner", "answer"))
    assert view["draft"] is None and view["confirmation"] is None
    assert view["reply_outcome"]["accepted"] is False
    assert "SO-30" in view["reply_outcome"]["host_question"]
    assert len(view["episodes"]) == 1 and view["usage"]["tokens"] == 18


def test_quote_action_host_rendering_and_new_reply_invalidates_draft(configured):
    _, host, model, environment = runtime.build_runtime(**configured, strategy="rules")
    case_id = waiting_reply(host, environment)
    view = host._perform(host._job("reply", case_id, "owner", "answer"))
    old = copy.deepcopy(view["draft"])
    receipt = host.cases.submit_reply(case_id, "owner", operation_id="withdraw", expected_revision=view["revision"],
                                      text="Withdraw that budget.")
    current = host.cases.get(case_id, "owner")
    assert current["draft"] is None
    with pytest.raises(CaseError):
        host.cases.confirm(case_id, "owner", operation_id="stale-confirm", expected_revision=current["revision"],
            draft_sha256=old["draft_sha256"], parent_policy_sha256=old["parent_policy_sha256"], confirmed=True)
    view = host.coordinator.interpret_reply(case_id, "owner", receipt["reply_id"], strategy="rules")
    assert view["draft"]["proposal"]["kind"] == "withdraw_budget"
    assert view["status"] == "awaiting_human" and view["confirmation"] is None
    assert "withdrawn" in view["draft"]["host_question"]


def recovered_reply(tmp_path, version):
    case = tmp_path / "case"
    directory = case / "workers/operation-1"
    directory.mkdir(parents=True)
    text = "Budget 800 USD"
    ticket = {"order_id": "30", "order_name": "SO-30"}
    row = {"reply_id": "reply-old", "sha256": digest(text), "text": text}
    later = {"reply_id": "reply-new", "sha256": digest("900 USD"), "text": "900 USD"}
    atomic_json(case / "case.json", {"replies": [row, later], "reply_contract_version": version})
    proposal = {"kind": "budget_authorization_proposal", "order_id": "30", "amount": "800.00", "currency": "USD"}
    binding = {"reply_id": row["reply_id"], "currency": "USD", "claim_input": {
        "ticket": ticket, "strategy": "fixed", "reply_sha256": row["sha256"]}}
    claim = {"kind": "reply", "episode_id": "parent"}
    if version == 2:
        manifest = v2.visible_manifest([row], row["reply_id"])
        atomic_json(directory / "visible-replies.json", manifest)
        binding["claim_input"].update(reply_contract_version=2, visible_manifest_sha256=digest(manifest))
        claim.update(reply_contract_version=2, input=binding["claim_input"])
        proposal.update(schema_version=2, source_spans=[{"reply_id": row["reply_id"], "start": 0, "end": len(text)}])
    claim["input_sha256"] = digest(binding["claim_input"])
    state = identity(mode="reply", phase="finished", started_at=time.time() - 2, dispatch_started=True,
        claim_sha256=digest(claim), business_binding=binding, runtime_valid=True, container_removed=True,
        quiescence="confirmed", container_quiescence="confirmed")
    atomic_json(directory / "claim.json", claim)
    atomic_json(directory / "runtime.json", state)
    atomic_json(directory / "proposal.json", proposal)
    (directory / "agent-logs").mkdir()
    atomic_json(directory / "agent-logs/token-budget.json", {"provider_requests": 1, "completed_responses": 1,
        "reported_total_tokens": 100, "in_flight": False, "partial_usage_unknown": False, "stop_reason": None})
    (directory / "pi.jsonl").write_text(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "toolUse", "usage": {"totalTokens": 100}}}) + "\n")
    atomic_json(directory / "expected-registry.json", [{"name": "only-tool"}])
    (directory / "agent-logs/case-registry.jsonl").write_text(json.dumps({"active": ["only-tool"],
        "expected": ["only-tool"], "registered": [{"name": "only-tool"}]}) + "\n")
    return directory, claim, proposal


@pytest.mark.parametrize("version", [1, 2])
def test_recovery_uses_original_version_and_manifest_not_later_reply(tmp_path, monkeypatch, version):
    directory, claim, proposal = recovered_reply(tmp_path, version)
    original = {name: (directory / name).read_bytes() for name in ("claim.json", "runtime.json", "proposal.json")}
    w = worker(tmp_path)
    calls = []

    def docker(*args, **_):
        calls.append(args)
        assert args[0] == "ps"  # never create/exec/provider
        return ""

    monkeypatch.setattr(w, "docker", docker)
    outcome = w.recover(claim, directory)
    assert outcome["proposal"] == proposal and outcome["usage"]["tokens"] == 100
    assert outcome["runtime_valid"] is True
    assert w.recover(claim, directory) == outcome and len(calls) == 1
    assert original == {name: (directory / name).read_bytes() for name in original}


def test_manifest_tampering_rejected_before_container_recovery(tmp_path, monkeypatch):
    directory, claim, _ = recovered_reply(tmp_path, 2)
    manifest = read_json(directory / "visible-replies.json")
    manifest["replies"][0]["text"] = "Budget 900 USD"
    atomic_json(directory / "visible-replies.json", manifest, replace=True)
    w = worker(tmp_path)
    monkeypatch.setattr(w, "docker", lambda *_a, **_k: pytest.fail("Unbound input must reject before Docker"))
    with pytest.raises(v2.ClarificationError, match="reply_hash_mismatch"):
        w.recover(claim, directory)


@pytest.mark.parametrize("version,strategy,limit", [(1, "fixed", 1), (2, "fixed", 1), (2, "interactive", 8)])
def test_actual_bridge_version_description_and_provider_count_gate(tmp_path, version, strategy, limit):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the real JS extension boundary")
    source = Path(__file__).resolve().parents[1] / "src/forgeflow/agents"
    bridge = (source / "continuation_bridge.js").read_text()
    bridge = bridge.replace("'./native_investigation.js'", json.dumps((source / "native_investigation.js").as_uri()))
    schema = v2.PROPOSAL_SCHEMA if version == 2 else {"type": "object", "properties": {"kind": {"type": "string"}}}
    config = {"mode": "reply", "strategy": strategy, "reply_contract_version": version, "proposal_schema": schema}
    bridge = bridge.replace("JSON.parse(readFileSync('/opt/forgeflow/bridge.json', 'utf8'))", json.dumps(config))
    path = tmp_path / "bridge.mjs"
    path.write_text(bridge)
    script = """
const {default: install,specs}=await import(process.argv[1]);
const handlers={},registered=[];
install({registerTool:t=>registered.push(t),on:(event,f)=>handlers[event]=f});
const gate=globalThis.__forgeflowInvestigationTerminal;
const approvals=Array.from({length:10},()=>gate.beforeProvider());
console.log(JSON.stringify({spec:specs()[0],approvals,registered:registered.length}));
"""
    result = subprocess.run([node, "--input-type=module", "-e", script, path.as_uri()],
                            capture_output=True, text=True, timeout=15, check=True)
    data = json.loads(result.stdout)
    assert data["registered"] == 1 and sum(data["approvals"]) == limit
    assert data["spec"]["parameters"] == schema
    assert ("schema_version 2" in data["spec"]["description"]) is (version == 2)


def test_bound_version_system_help_is_consistent_with_tool_contract():
    old = reply_system_help(1)
    assert "only kind and optional question" in old and "schema_version 2" not in old
    new = reply_system_help(2)
    assert "only kind and optional question" not in new
    assert "schema_version, kind and source_spans" in new and "ask_followup also requires need" in new
    assert "Unicode code-point" in new and "withdrawal" in new
    with pytest.raises(ValueError, match="unsupported_reply_contract_version"):
        reply_system_help(3)
