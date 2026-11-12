"""Trusted stop/control boundaries, with local fake provider responses only."""
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_investigation_clarification_revision import restart
from test_investigation_service import RUN_ID, setup_service, state, validate

from forgeflow.budget import PI_BUDGET_EXTENSION

ROOT = Path(__file__).resolve().parents[1]


def run_node(tmp_path, source):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for actual runtime hooks")
    for name in ("native_investigation.js", "native_planner.js"):
        shutil.copyfile(ROOT / "src/forgeflow/agents" / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    (tmp_path / "guard.mjs").write_text(PI_BUDGET_EXTENSION.replace(
        "/opt/forgeflow", str(tmp_path)).replace("/proc/uptime", str(tmp_path / "uptime")).replace(
        "/home/forgeflow/logs", str(tmp_path / "logs")))
    (tmp_path / "check.mjs").write_text(source)
    result = subprocess.run([node, str(tmp_path / "check.mjs")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("terminal", ["prepared", "needs_clarification", "infeasible",
                                    "unsupported", "budget_exhausted", "technical_failure"])
def test_all_ordinary_terminals_stop_without_another_provider_request(tmp_path, terminal):
    run_node(tmp_path, f'''
import assert from 'node:assert/strict';
import {{installInvestigationTerminal}} from './native_investigation.js';
const hooks={{}}, saved=[]; let exits=0, providers=1;
const c=installInvestigationTerminal({{on:(n,f)=>hooks[n]=f,setActiveTools:()=>{{}}}},
 {{save:x=>saved.push(x),stop:()=>{{exits++}}}});
c.observe({{status:{json.dumps(terminal)},run_id:'episode-1',terminal:{{reason:'trusted_stop'}}}});
assert.equal(c.beforeProvider(),false);
if(c.beforeProvider()) providers++;
assert.equal(providers,1);
assert.equal(hooks.tool_call({{toolName:'investigation_query',input:{{}}}}).block,true);
await hooks.turn_end();
assert.equal(exits,1); assert.equal(saved.at(-1).phase,'stopped');
assert.equal(saved.at(-1).closing_provider_requests,0);
''')


def test_closing_has_one_billed_response_and_one_finish_only(tmp_path):
    run_node(tmp_path, '''
import assert from 'node:assert/strict';
import {installInvestigationTerminal} from './native_investigation.js';
import {createTokenBudget} from './guard.mjs';
const hooks={}, saved=[]; let exits=0, active=null;
const budget=createTokenBudget({mechanism:'reported-usage-gate-v1',max_total_tokens:2000000,min_next_call_tokens:1024});
globalThis.__forgeflowTokenSnapshot=()=>budget.snapshot();
const c=installInvestigationTerminal({on:(n,f)=>hooks[n]=f,setActiveTools:x=>active=x},
 {save:x=>saved.push(x),stop:()=>{exits++}});
budget.beforeProvider({max_tokens:1048576,messages:[]});
budget.messageEnd({message:{role:'assistant',stopReason:'toolUse',usage:{totalTokens:10000}}});
c.observe({status:'technical_failure',closing_allowed:true,run_id:'episode-1',
 terminal:{stop_origin:'invalid_draft_guard',reason:'repeated_invalid_draft_without_new_evidence'}});
await hooks.turn_end(); assert.equal(exits,0); assert.deepEqual(active,['finish_investigation']);
for(const name of ['investigation_query','validate_investigation_draft','prepare_investigated_recovery','investigation_status'])
 assert.equal(hooks.tool_call({toolName:name,input:{}}).block,true);
assert.equal(c.beforeProvider(),true); budget.beforeProvider({max_tokens:1048576,messages:[]});
budget.messageEnd({message:{role:'assistant',stopReason:'toolUse',usage:{totalTokens:7000}}});
assert.equal(hooks.tool_call({toolName:'finish_investigation',input:{status:'needs_clarification'}}),undefined);
assert.equal(hooks.tool_call({toolName:'finish_investigation',input:{status:'needs_clarification'}}).block,true);
// Even invalid tool arguments / a text-only answer cannot buy another response.
assert.equal(c.beforeProvider(),false); await hooks.turn_end();
assert.equal(exits,1); assert.equal(saved.at(-1).closing_provider_requests,1);
assert.equal(saved.at(-1).usage_at_terminal.reported_total_tokens,10000);
assert.equal(saved.at(-1).usage_at_stop.reported_total_tokens,17000);
assert.equal(budget.snapshot().provider_requests,2);
''')


def test_closing_offer_is_consumed_before_response_and_never_reissued(tmp_path):
    service, source, _ = setup_service(tmp_path)
    draft = {"request": {}, "evidence": []}
    validate(service, draft)
    first = validate(service, draft)
    assert first["closing_allowed"] is True
    before = state(service)
    assert before["closing_opportunity"]["consumed"] is True
    assert before["closing_opportunity"]["max_provider_requests"] == 1
    # Treat the first response as lost: both duplicate dispatch and a new process
    # see only the persisted stop. No second offer can be minted.
    assert validate(service, draft)["closing_allowed"] is False
    fresh = restart(service, source)
    assert fresh.dispatch(RUN_ID, "status", {})["closing_allowed"] is False
    assert fresh.dispatch(RUN_ID, "validate", {"draft": draft})["closing_allowed"] is False
    assert state(service) == before


def test_concurrent_repeated_draft_cannot_duplicate_closing_offer(tmp_path):
    from forgeflow.recovery.storage import StorageError

    service, _, _ = setup_service(tmp_path)
    draft = {"request": {}, "evidence": []}
    validate(service, draft)

    def attempt():
        try:
            return validate(service, draft)
        except StorageError:
            return {"closing_allowed": False, "status": "lock_rejected"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result["closing_allowed"] for result in results) == 1
    assert state(service)["closing_opportunity"]["consumed"] is True


def test_query_transport_retry_reuses_id_bytes_and_deadline(tmp_path):
    run_node(tmp_path, '''
import assert from 'node:assert/strict';
import {invokeInvestigation} from './native_investigation.js';
const calls=[];let clock=100;
const pi={exec:async (...args)=>{calls.push(args);clock+=500;
 if(calls.length===1)throw Error('response lost');
 return {code:0,killed:false,stdout:JSON.stringify({status:'investigating',records:[]})};}};
await invokeInvestigation(pi,'query',{kind:'policy',arguments:{},operation_id:'same-operation'},null,()=>clock);
assert.equal(calls.length,2);assert.deepEqual(calls[0][1],calls[1][1]);
assert.ok(calls[1][2].timeout<calls[0][2].timeout);
''')


def test_query_cli_transport_error_retries_but_business_errors_do_not(tmp_path):
    run_node(tmp_path, '''
import assert from 'node:assert/strict';
import {invokeInvestigation} from './native_investigation.js';
for(const code of ['unavailable','bad_response','invalid_field']){
 const calls=[];const pi={exec:async(...args)=>{calls.push(args);return calls.length===1?
  {code:1,killed:false,stdout:JSON.stringify({status:'tool_error',code})}:
  {code:0,killed:false,stdout:JSON.stringify({status:'investigating',records:[]})};}};
 if(code==='invalid_field'){
  await assert.rejects(()=>invokeInvestigation(pi,'query',{operation_id:'fixed'},null));
  assert.equal(calls.length,1);
 }else{
  await invokeInvestigation(pi,'query',{operation_id:'fixed'},null);
  assert.equal(calls.length,2);assert.deepEqual(calls[0][1],calls[1][1]);
 }
}
''')


def test_actual_budget_hooks_keep_complete_usage_on_terminal_exit(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "uptime").write_text("101 1")
    (tmp_path / "budget.json").write_text(json.dumps({
        "mechanism": "host-continuous-v1", "started_boottime_sec": 100,
        "deadline_boottime_sec": 3700, "deadline_utc_sec": time.time() + 3600,
        "token_budget": {"mechanism": "reported-usage-gate-v1", "max_total_tokens": 2000000,
                         "min_next_call_tokens": 1024}}))
    run_node(tmp_path, '''
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import budget from './guard.mjs';
import {installInvestigationTerminal} from './native_investigation.js';
const handlers={};const pi={on:(n,f)=>(handlers[n]??=[]).push(f),setActiveTools:()=>{}};
budget(pi);const control=installInvestigationTerminal(pi,{save:()=>{},stop:()=>{}});
let payload={max_tokens:1048576,messages:[{role:'user',content:'event'}]};
for(const f of handlers.before_provider_request) payload=f({payload})??payload;
payload=globalThis.__forgeflowProviderRequestBudget(payload);
assert.equal(payload.max_tokens,1048576); // Default stays maximum while allowance permits.
for(const f of handlers.message_end)f({message:{role:'assistant',stopReason:'toolUse',usage:{totalTokens:12345}}});
control.observe({status:'prepared',terminal:{reason:'validated_preparation'}});
await handlers.turn_end[0]();
writeFileSync(new URL('./requests.json',import.meta.url),JSON.stringify({dispatched:1}));
// Attempting an ordinary next call exits before in_flight or request count changes.
for(const f of handlers.before_provider_request)f({payload});
throw Error('a post-terminal provider call escaped');
''')
    budget = json.loads((tmp_path / "logs/token-budget.json").read_text())
    assert budget["provider_requests"] == 1
    assert budget["completed_responses"] == 1
    assert budget["reported_total_tokens"] == 12345
    assert budget["in_flight"] is False
    assert budget["partial_usage_unknown"] is False


def test_remaining_cap_preserves_default_and_covers_final_payload_policy(tmp_path):
    run_node(tmp_path, '''
import assert from 'node:assert/strict';
import {createTokenBudget,inputReservation,capPayloadToRemaining} from './guard.mjs';
const payload={max_tokens:1048576,messages:[{role:'user',content:'event'}]};
assert.equal(capPayloadToRemaining(payload,2000000).max_tokens,1048576);
const capped=capPayloadToRemaining(payload,10000);
assert.equal(capped.max_tokens,10000-inputReservation(payload));
assert.equal(payload.max_tokens,1048576); // Do not mutate the model configuration.
const completion=capPayloadToRemaining({max_completion_tokens:1048576,messages:[]},10000);
assert.ok(completion.max_completion_tokens<10000);
const budget=createTokenBudget({mechanism:'reported-usage-gate-v1',max_total_tokens:5000,min_next_call_tokens:1024});
assert.throws(()=>budget.beforeProvider({max_tokens:1048576,messages:[{content:'x'.repeat(9000)}]}),/token_budget_exhausted/);
assert.equal(budget.snapshot().provider_requests,0);assert.equal(budget.snapshot().in_flight,false);
''')
