"""Reported main-loop token usage gates later dispatch, never invents usage."""
import json
import shutil
import subprocess
import time

import pytest

from forgeflow.budget import PI_BUDGET_EXTENSION, resolve_total_token_budget


def node_run(tmp_path, source, *args):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the actual extension boundary")
    (tmp_path / "guard.mjs").write_text(PI_BUDGET_EXTENSION.replace(
        "/opt/forgeflow", str(tmp_path)).replace("/proc/uptime", str(tmp_path / "uptime")).replace(
        "/home/forgeflow/logs", str(tmp_path / "logs")))
    (tmp_path / "driver.mjs").write_text(source)
    return subprocess.run([node, str(tmp_path / "driver.mjs"), *args], capture_output=True, text=True, timeout=5)


def test_total_token_default_is_only_for_preparation():
    assert resolve_total_token_budget(None, agent="pi", recovery_mode="autonomous") is None
    assert resolve_total_token_budget(None, agent="pi", recovery_mode="prepare-only") == 2000000
    assert resolve_total_token_budget(None, agent="pi", recovery_mode="investigation") == 2000000
    assert resolve_total_token_budget(4096, agent="pi", recovery_mode="autonomous") == 4096


@pytest.mark.parametrize("value", [0, -1, True, "60000", 2**53, 2.5])
def test_invalid_total_token_budget_rejected(value):
    with pytest.raises(ValueError, match="max_total_tokens"):
        resolve_total_token_budget(value, agent="pi", recovery_mode="investigation")


def test_total_tokens_is_pi_only():
    with pytest.raises(ValueError, match="only for the Pi agent"):
        resolve_total_token_budget(1000, agent="oracle", recovery_mode="autonomous")


def test_usage_counter_has_one_source_and_blocks_without_guessing(tmp_path):
    result = node_run(tmp_path, r'''
import assert from 'node:assert/strict';
import {createTokenBudget} from './guard.mjs';
const policy={mechanism:'reported-usage-gate-v1',max_total_tokens:3000,min_next_call_tokens:1024};
const logs=[]; const b=createTokenBudget(policy,v=>logs.push(v));
b.messageEnd({message:{role:'user'}});
b.beforeProvider();
b.messageEnd({message:{role:'toolResult',usage:{totalTokens:999999}}});
assert.equal(b.snapshot().in_flight,true);
b.messageEnd({message:{role:'assistant',stopReason:'toolUse',usage:{totalTokens:1200,output:1000,reasoning:900,cacheRead:200}}});
assert.equal(b.snapshot().reported_total_tokens,1200);
b.beforeTool(); b.beforeProvider();
b.messageEnd({message:{role:'assistant',stopReason:'stop',usage:{totalTokens:1000}}});
assert.equal(b.snapshot().reported_total_tokens,2200);
b.beforeTool();
assert.throws(()=>b.beforeProvider(),/token_budget_exhausted/);
assert.equal(b.snapshot().provider_requests,2);
for (const total of [undefined,0,-1,1.5,'100',NaN]) {
  const c=createTokenBudget(policy);c.beforeProvider();
  c.messageEnd({message:{role:'assistant',stopReason:'toolUse',usage:{totalTokens:total}}});
  assert.equal(c.snapshot().partial_usage_unknown,true);
  assert.throws(()=>c.beforeTool(),/token_usage_unknown/);
  assert.throws(()=>c.beforeProvider(),/token_usage_unknown/);
}
for (const stopReason of ['error','aborted','pending','deferred']) {
  const c=createTokenBudget(policy);c.beforeProvider();
  c.messageEnd({message:{role:'assistant',stopReason,usage:{totalTokens:10}}});
  assert.equal(c.snapshot().reported_total_tokens,10);
  assert.equal(c.snapshot().partial_usage_unknown,true);
  assert.throws(()=>c.beforeTool(),/token_usage_unknown/);
}
const overflow=createTokenBudget(policy);overflow.beforeProvider();
overflow.messageEnd({message:{role:'assistant',stopReason:'toolUse',usage:{totalTokens:3100}}});
assert.equal(overflow.snapshot().reported_total_tokens,3100);
assert.throws(()=>overflow.beforeTool(),/token_budget_exhausted/);
const interrupted=createTokenBudget(policy);interrupted.beforeProvider();interrupted.interrupted();
assert.equal(interrupted.snapshot().partial_usage_unknown,true);
assert.throws(()=>interrupted.beforeProvider(),/token_usage_unknown/);
assert.equal(createTokenBudget(null),null);
''')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("action", "exit_code", "reason"), [
    ("reserve", 125, "token_budget_exhausted"),
    ("retry", 125, "token_usage_unknown"),
    ("tool", 0, "token_budget_exhausted"),
    ("missing", 0, "token_usage_unknown"),
    ("partial", 0, "token_usage_unknown"),
])
def test_real_hooks_stop_next_dispatch_and_persist_partial_usage(tmp_path, action, exit_code, reason):
    (tmp_path / "logs").mkdir()
    (tmp_path / "uptime").write_text("101 1")
    (tmp_path / "budget.json").write_text(json.dumps({
        "mechanism": "host-continuous-v1", "started_boottime_sec": 100,
        "deadline_boottime_sec": 700, "deadline_utc_sec": time.time() + 600,
        "token_budget": {"mechanism": "reported-usage-gate-v1", "max_total_tokens": 20000,
                         "min_next_call_tokens": 1024},
    }))
    result = node_run(tmp_path, r'''
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import guard from './guard.mjs';
const handlers={};guard({on:(name,fn)=>{handlers[name]=fn}});
assert.equal(handlers.turn_end,undefined);
handlers.before_provider_request({payload:{max_tokens:1040000,messages:[{content:"private-input"}]}});
const action=process.argv[2];
if(action==='partial') process.exit(0);
if(action==='retry') handlers.before_provider_request({payload:{max_tokens:1040000,messages:[{content:"private-input"}]}});
handlers.message_end({message:{role:'assistant',stopReason:'toolUse',
  usage:{totalTokens:action==='missing'?0:action==='reserve'?19000:21000}}});
if(action==='reserve') handlers.before_provider_request({payload:{max_tokens:1040000,messages:[{content:"private-input"}]}});
const block=await handlers.tool_call({toolName:'prepare_investigated_recovery'});
assert.equal(block.block,true);assert.equal(block.terminate,true);
''', action)
    assert result.returncode == exit_code, result.stderr
    telemetry = json.loads((tmp_path / "logs/token-budget.json").read_text())
    assert telemetry["stop_reason"] == reason
    assert telemetry["provider_requests"] == 1
    assert telemetry["partial_usage_unknown"] == (reason == "token_usage_unknown")
    audit = json.loads((tmp_path / "logs/provider-request-policy.jsonl").read_text().splitlines()[0])
    assert 0 < audit["max_tokens"] < 20000
    assert audit["message_count"] == 1
    assert "private-input" not in json.dumps(audit)


def test_root_budget_configuration_contains_token_policy(tmp_path, monkeypatch):
    import asyncio
    import shlex
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from forgeflow.agents.pi_nebius import PiNebius
    from forgeflow.budget import Deadline
    agent = PiNebius(logs_dir=tmp_path / "logs", recovery_mode="prepare-only",
                     planner_profile="cp-sat-recovery-v1", security_profile="permission-split-v1",
                     max_total_tokens=4096)
    agent.exec_as_root=AsyncMock(return_value=SimpleNamespace(stdout=json.dumps({
        "utc_sec":1,"boottime_sec":1,"monotonic_sec":1,"probe_cpu_sec":1})))
    asyncio.run(agent._configure_budget(SimpleNamespace(), Deadline(agent._budget().clock,600)))
    program=shlex.split(agent.exec_as_root.call_args.kwargs["command"])[-1]
    assert "'max_total_tokens': 4096" in program
    assert "'min_next_call_tokens': 1024" in program
    assert "os.chmod(temporary, 0o444)" in program
