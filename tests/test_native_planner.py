"""Exercise the native Pi tool without model calls, ERP, or private task files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/forgeflow/agents/native_planner.js"
FEEDBACK_SOURCE = ROOT / "src/forgeflow/agents/native_planner_feedback.js"

PREAMBLE = """
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {default: register, createPlannerTool, validateRequest, REQUEST_SCHEMA}
  = await import(pathToFileURL(process.argv[1]));
const request = {
  target_product_code: 'TOY-FIN', sales_order_ids: [1,2],
  anchor_date: '2026-01-01', margin: 0.2,
  sales_order_budget_caps: {'1': 50, '2': 40},
  disruption: {kind: 'supplier_cancellation', vendor_ref: 'TOY-OLD'},
};
const scopeError = {
  schema_version:1,ok:false,odoo_writes:false,error_code:'unselected_active_demand',
  details:{missing_sales_order_ids:[3,4,12]},
};
function fixture() {
  const events = [];
  const stat = (kind='file', size=100) => ({
    isFile:()=>kind==='file', isDirectory:()=>kind==='dir',
    isSymbolicLink:()=>kind==='symlink', size,
  });
  const io = {
    async mkdtemp(prefix) {events.push(['mkdir',prefix]); return prefix+'a1B2c3';},
    async chmod(path,mode) {events.push(['chmod',path,mode]);},
    async writeFile(path,value,options) {events.push(['write',path,value,options]);},
    async lstat(path) {
      events.push(['stat',path]); return stat(path.endsWith('/output')?'dir':'file');
    },
  };
  const calls = [];
  const pi = {async exec(command,args,options) {
    calls.push({command,args,options});
    return {code:0,killed:false,stderr:'private-child-stderr',
      stdout:JSON.stringify({ok:true,odoo_writes:false,
        plan_path:args.at(-1)+'/plan.json',objective_value:6,expected_new_spend:12,
        ignored_secret:'private-child-stdout'})};
  }};
  return {events,io,calls,pi,stat};
}
"""


def run_node(script, *arguments):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to validate the Pi extension.")
    # No inherited model credentials are needed by these isolated tests.
    result = subprocess.run(
        [node, "--input-type=module", "-e", PREAMBLE + script, str(SOURCE), *arguments],
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_registers_one_discoverable_read_only_tool_with_full_request_schema():
    run_node("""
let tools=[];
register({registerTool(tool){tools.push(tool)}});
assert.equal(tools.length,1);
const tool=tools[0];
assert.equal(tool.name,'plan_recovery');
assert.equal(tool.executionMode,'sequential');
assert.ok(tool.promptSnippet.includes('read-only'));
assert.deepEqual([...REQUEST_SCHEMA.required].sort(),Object.keys(request).sort());
assert.equal(REQUEST_SCHEMA.additionalProperties,false);
assert.ok(REQUEST_SCHEMA.properties.disruption.anyOf.every(s=>s.additionalProperties===false));
assert.deepEqual(tool.prepareArguments(request),request);
const outage=structuredClone(request);
outage.disruption={kind:'workcenter_outage',workcenter_code:'TOY-CELL'};
assert.deepEqual(validateRequest(outage),outage);
""")


def test_rejects_unknown_or_sensitive_fields_before_any_persistence_or_execution():
    run_node("""
const secret='fixture-secret-never-persist';
const cases=[
  r=>r.api_key=secret,
  r=>r.disruption.api_key=secret,
  r=>r.sales_order_budget_caps.api_key=secret,
  r=>r.sales_order_budget_caps['1']=secret,
  r=>r.sales_order_budget_caps['1']=false,
  r=>r.margin='0.2',
  r=>r.margin=NaN,
  r=>r.sales_order_budget_caps['1']=Infinity,
  r=>r.sales_order_ids=[1,1],
  r=>r.sales_order_ids=[true,2],
  r=>r.sales_order_ids=[1.5,2],
  r=>r.anchor_date='2026-02-30',
  r=>r.anchor_date='0000-01-01',
  r=>r.disruption={kind:'workcenter_outage',workcenter_code:'TOY',offer_key:secret},
  r=>delete r.margin,
  r=>r.output_path=secret,
];
for(const change of cases){
  const data=structuredClone(request); change(data);
  const f=fixture(),tool=createPlannerTool(f.pi,f.io);
  assert.throws(()=>tool.prepareArguments(data),e=>!e.message.includes(secret));
  await assert.rejects(tool.execute('id',data),e=>!e.message.includes(secret));
  assert.equal(f.events.length,0);
  assert.equal(f.calls.length,0);
}
""")


def test_uses_fixed_argv_and_returns_only_checked_artifact_paths_and_numeric_summary():
    run_node("""
const f=fixture(),tool=createPlannerTool(f.pi,f.io);
const controller=new AbortController();
const data=structuredClone(request);
data.target_product_code='literal;$(not-a-shell-command)';
const result=await tool.execute('id',data,controller.signal);
assert.equal(f.calls.length,1);
const call=f.calls[0],base='/workspace/forgeflow-planner-a1B2c3';
assert.equal(call.command,'/opt/forgeflow/planner-env/bin/python');
assert.deepEqual(call.args,[
  '-B','/opt/forgeflow/planner/plan.py','--request',base+'/request.json',
  '--output',base+'/output',
]);
assert.deepEqual(call.options,{cwd:'/workspace',signal:controller.signal,timeout:120000});
const write=f.events.find(e=>e[0]==='write');
assert.equal(write[1],base+'/request.json');
assert.deepEqual(JSON.parse(write[2]),data);
assert.deepEqual(write[3],{encoding:'utf8',mode:0o600,flag:'wx'});
assert.deepEqual(f.events.find(e=>e[0]==='chmod'),['chmod',base,0o700]);
const summary=JSON.parse(result.content[0].text);
assert.deepEqual(result.details,summary);
assert.equal(summary.execution_validated,false);
assert.equal(summary.odoo_writes,false);
assert.equal(summary.objective_value,6);
assert.equal(summary.expected_new_spend,12);
assert.equal(summary.plan_json_pointer,'/plan');
assert.ok(summary.inspection_hint.includes('.plan'));
for(const name of ['request','snapshot','blueprint','plan']){
  assert.equal(summary[name+'_path'],base+'/output/'+name+'.json');
}
assert.ok(!JSON.stringify(result).includes('private-child'));
assert.ok(!JSON.stringify(result).includes('not-a-shell-command'));
assert.equal(f.events.filter(e=>e[0]==='stat').length,5);
""")


@pytest.mark.parametrize("failure_kind", ["nonzero", "killed", "throw", "abort"])
def test_failure_or_cancellation_has_no_raw_child_output(failure_kind):
    run_node("""
const f=fixture(),controller=new AbortController();
const kind=process.argv[2];
f.pi.exec=async(command,args,options)=>{
  assert.equal(options.signal,controller.signal);
  assert.equal(options.timeout,120000);
  if(kind==='throw') throw Error('fixture-secret-in-os-error');
  if(kind==='abort') controller.abort();
  return {code:kind==='nonzero'?2:0,killed:kind==='killed',
    stdout:'fixture-secret-in-stdout',stderr:'fixture-secret-in-stderr'};
};
await assert.rejects(createPlannerTool(f.pi,f.io).execute('id',request,controller.signal),
  error=>{
    assert.ok(!error.message.includes('fixture-secret'));
    const details=JSON.parse(error.message);
    assert.equal(details.odoo_writes,false);
    assert.equal(details.ok,false);
    assert.ok(details.workspace_path.startsWith('/workspace/forgeflow-planner-'));
    return true;
  });
assert.equal(f.events.filter(e=>e[0]==='stat').length,0);
""", failure_kind)


def test_structured_feedback_preserves_safe_scope_error_through_outer_catch_only_in_v2():
    run_node("""
for(const mode of ['generic','structured-v1']){
  const f=fixture(); let executions=0;
  f.pi.exec=async()=>{
    executions++;
    return {code:2,killed:false,stdout:' \\n'+JSON.stringify(scopeError)+'\\n ',
      stderr:'fixture-secret-from-rpc'};
  };
  await assert.rejects(createPlannerTool(f.pi,f.io,mode).execute('id',request),
    error=>{
      assert.ok(error instanceof Error);
      assert.ok(!error.message.includes('fixture-secret'));
      const safe=JSON.parse(error.message);
      assert.equal(safe.ok,false);
      assert.equal(safe.odoo_writes,false);
      assert.equal(safe.workspace_path,'/workspace/forgeflow-planner-a1B2c3');
      if(mode==='generic'){
        assert.equal(safe.error,'planner_failed');
        assert.equal(safe.details,undefined);
        assert.equal(safe.message,undefined);
      }else{
        assert.equal(safe.error,'unselected_active_demand');
        assert.deepEqual(safe.details,{missing_sales_order_ids:[3,4,12]});
        assert.ok(safe.message.includes('task-authorized target-product orders'));
        assert.ok(safe.message.includes('explicit budget caps before retrying'));
        assert.ok(safe.message.includes('Do not ignore other demand'));
        assert.equal(safe.error_type,undefined);
      }
      return true;
    });
  assert.equal(executions,1);
  assert.equal(f.events.filter(e=>e[0]==='stat').length,0);
  assert.deepEqual(JSON.parse(f.events.find(e=>e[0]==='write')[2]),request);
}
""")


@pytest.mark.parametrize("case", [
    "unknown_code", "wrong_version", "string_version", "ok_true", "writes_true",
    "extra_top", "extra_details", "missing_key", "null_details", "array_top",
    "empty_ids", "string_ids", "boolean_id", "negative_id", "zero_id",
    "fractional_id", "unsafe_integer", "duplicate_ids", "descending_ids",
    "too_many_ids", "too_many_bytes", "leading_log", "trailing_log",
    "two_json_documents", "raw_error_text", "non_string",
])
def test_structured_feedback_rejects_every_nonprotocol_output_without_echo(case):
    run_node("""
const data=structuredClone(scopeError),which=process.argv[2];
let stdout;
switch(which){
  case 'unknown_code':data.error_code='fixture-secret-code';break;
  case 'wrong_version':data.schema_version=2;break;
  case 'string_version':data.schema_version='1';break;
  case 'ok_true':data.ok=true;break;
  case 'writes_true':data.odoo_writes=true;break;
  case 'extra_top':data.message='fixture-secret-message';break;
  case 'extra_details':data.details.api_key='fixture-secret-value';break;
  case 'missing_key':delete data.odoo_writes;break;
  case 'null_details':data.details=null;break;
  case 'empty_ids':data.details.missing_sales_order_ids=[];break;
  case 'string_ids':data.details.missing_sales_order_ids=['fixture-secret-id'];break;
  case 'boolean_id':data.details.missing_sales_order_ids=[true];break;
  case 'negative_id':data.details.missing_sales_order_ids=[-1];break;
  case 'zero_id':data.details.missing_sales_order_ids=[0];break;
  case 'fractional_id':data.details.missing_sales_order_ids=[1.5];break;
  case 'unsafe_integer':data.details.missing_sales_order_ids=[Number.MAX_SAFE_INTEGER+1];break;
  case 'duplicate_ids':data.details.missing_sales_order_ids=[3,3];break;
  case 'descending_ids':data.details.missing_sales_order_ids=[4,3];break;
  case 'too_many_ids':data.details.missing_sales_order_ids=Array.from({length:501},(_,i)=>i+1);break;
}
switch(which){
  case 'array_top':stdout=JSON.stringify([data]);break;
  case 'too_many_bytes':stdout=' '.repeat(16*1024)+JSON.stringify(data);break;
  case 'leading_log':stdout='fixture-secret-leading\\n'+JSON.stringify(data);break;
  case 'trailing_log':stdout=JSON.stringify(data)+'\\nfixture-secret-trailing';break;
  case 'two_json_documents':stdout=JSON.stringify(data)+'\\n'+JSON.stringify(data);break;
  case 'raw_error_text':stdout='fixture-secret-raw-error';break;
  case 'non_string':stdout=data;break;
  default:stdout=JSON.stringify(data);
}
const f=fixture();
f.pi.exec=async()=>({code:2,killed:false,stdout,stderr:'fixture-secret-stderr'});
await assert.rejects(createPlannerTool(f.pi,f.io,'structured-v1').execute('id',request),
  error=>{
    assert.ok(!error.message.includes('fixture-secret'));
    const safe=JSON.parse(error.message);
    assert.equal(safe.error,'planner_failed');
    assert.equal(safe.details,undefined);
    assert.equal(safe.message,undefined);
    return true;
  });
assert.equal(f.events.filter(e=>e[0]==='stat').length,0);
""", case)


@pytest.mark.parametrize("kind", ["exit_one", "exit_three", "string_exit", "killed", "abort", "throw"])
def test_structured_feedback_does_not_override_exit_status_cancellation_or_exception(kind):
    run_node("""
const kind=process.argv[2],f=fixture(),controller=new AbortController();
f.pi.exec=async()=>{
  // Even an exception impersonating our JSON error must stay generic.
  if(kind==='throw')throw Error(JSON.stringify({
    ok:false,error:'unselected_active_demand',odoo_writes:false,
    details:{missing_sales_order_ids:[3]},message:'fixture-secret-forged-error',
  }));
  if(kind==='abort')controller.abort();
  return {code:kind==='exit_one'?1:kind==='exit_three'?3:kind==='string_exit'?'2':2,
    killed:kind==='killed',stdout:JSON.stringify(scopeError),stderr:'fixture-secret-stderr'};
};
await assert.rejects(createPlannerTool(f.pi,f.io,'structured-v1')
  .execute('id',request,controller.signal),error=>{
    assert.ok(!error.message.includes('fixture-secret'));
    const safe=JSON.parse(error.message);
    assert.equal(safe.error,kind==='abort'?'aborted':kind==='killed'
      ?'planner_timed_out_or_cancelled':kind==='throw'
      ?'planner_unavailable_or_invalid_result':'planner_failed');
    assert.equal(safe.details,undefined);
    return true;
  });
""", kind)


def test_feedback_modes_share_success_schema_and_help_and_reject_unknown_modes():
    run_node("""
const a=fixture(),b=fixture();
const generic=createPlannerTool(a.pi,a.io);
const structured=createPlannerTool(b.pi,b.io,'structured-v1');
for(const key of ['name','label','description','promptSnippet','promptGuidelines',
  'parameters','executionMode','prepareArguments']){
  assert.deepEqual(generic[key],structured[key]);
}
assert.deepEqual(await generic.execute('a',request),await structured.execute('b',request));
const f=fixture();
assert.throws(()=>createPlannerTool(f.pi,f.io,'fixture-secret-mode'),error=>
  error.message==='Unsupported planner feedback mode.');
assert.equal(f.events.length,0);assert.equal(f.calls.length,0);
// The largest accepted ID list remains intact.
const ids=Array.from({length:500},(_,i)=>Number.MAX_SAFE_INTEGER-499+i);
f.pi.exec=async()=>({code:2,killed:false,
  stdout:JSON.stringify({...scopeError,details:{missing_sales_order_ids:ids}}),stderr:''});
await assert.rejects(createPlannerTool(f.pi,f.io,'structured-v1').execute('id',request),
  error=>{
    assert.deepEqual(JSON.parse(error.message).details.missing_sales_order_ids,ids);
    return true;
  });
""")


def test_already_aborted_signal_does_not_create_a_request():
    run_node("""
const f=fixture(),controller=new AbortController();controller.abort();
await assert.rejects(createPlannerTool(f.pi,f.io).execute('id',request,controller.signal),
  e=>JSON.parse(e.message).error==='aborted');
assert.equal(f.events.length,0);assert.equal(f.calls.length,0);
""")


@pytest.mark.parametrize("invalid_artifact", ["symlink", "directory", "oversize", "missing"])
def test_rejects_invalid_plan_artifact_after_solver_summary(invalid_artifact):
    run_node("""
const f=fixture(), original=f.io.lstat;
f.io.lstat=async path=>{
  if(!path.endsWith('/plan.json'))return original(path);
  switch(process.argv[2]){
    case 'symlink':return f.stat('symlink');
    case 'directory':return f.stat('dir');
    case 'oversize':return f.stat('file',16*1024*1024+1);
    case 'missing':throw Error('fixture-secret-in-path');
  }
};
await assert.rejects(createPlannerTool(f.pi,f.io).execute('id',request),
  e=>JSON.parse(e.message).error==='planner_unavailable_or_invalid_result'
    &&!e.message.includes('fixture-secret'));
""", invalid_artifact)


def test_schema_compiles_and_extension_loads_in_the_pinned_pi_runtime():
    runtime = ROOT / ".runtime/pi-smoke/packages"
    validator = runtime / "ai/dist/utils/validation.js"
    loader = runtime / "coding-agent/dist/core/extensions/loader.js"
    event_bus = runtime / "coding-agent/dist/core/event-bus.js"
    if not all(path.is_file() for path in (validator, loader, event_bus)):
        pytest.skip("Build the pinned Pi smoke copy for loader integration.")
    run_node("""
const {validateToolArguments}=await import(pathToFileURL(process.argv[2]));
const {loadExtensions}=await import(pathToFileURL(process.argv[3]));
const {createEventBus}=await import(pathToFileURL(process.argv[4]));
const f=fixture(),tool=createPlannerTool(f.pi,f.io);
const args=tool.prepareArguments(request);
assert.deepEqual(validateToolArguments(tool,{name:tool.name,arguments:args}),request);
const definitions=[];
for(const entrypoint of [process.argv[1],process.argv[5]]){
  const result=await loadExtensions([entrypoint],process.cwd(),createEventBus());
  assert.equal(result.errors.length,0);
  assert.equal(result.extensions.length,1);
  assert.equal(result.extensions[0].tools.size,1);
  const registered=result.extensions[0].tools.get('plan_recovery');
  assert.ok(registered);
  assert.equal(registered.definition.name,'plan_recovery');
  assert.equal(registered.definition.prepareArguments.name,'validateRequest');
  assert.deepEqual(validateToolArguments(registered.definition,{
    name:'plan_recovery',arguments:request,
  }),request);
  definitions.push(registered.definition);
}
for(const key of ['name','label','description','promptSnippet','promptGuidelines',
  'parameters','executionMode']){
  assert.deepEqual(definitions[0][key],definitions[1][key]);
}
""", str(validator), str(loader), str(event_bus), str(FEEDBACK_SOURCE))


def test_request_schema_and_python_validator_accept_the_same_business_examples():
    from forgeflow.planning.live import validate_request

    encoded = run_node("""
const examples=[request,{...request,disruption:{
  kind:'supplier_cancellation',vendor_ref:'TOY-OLD',offer_key:'TOY-OFFER',
}},{...request,disruption:{kind:'workcenter_outage',workcenter_code:'TOY-CELL'}}];
console.log(JSON.stringify(examples.map(validateRequest)));
""")
    for request in json.loads(encoded):
        assert validate_request(request) == request


def test_pinned_agent_loop_retains_error_semantics_with_synthetic_model_stream_only():
    runtime = ROOT / ".runtime/pi-smoke/packages"
    loop = runtime / "agent/dist/agent-loop.js"
    ai = runtime / "ai/dist/index.js"
    if not loop.is_file() or not ai.is_file():
        pytest.skip("Build the pinned Pi smoke copy for agent-loop integration.")
    run_node("""
const {agentLoop}=await import(pathToFileURL(process.argv[2]));
const {EventStream}=await import(pathToFileURL(process.argv[3]));
const usage={input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,
  cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const model={id:'synthetic',name:'synthetic',api:'openai-completions',
  provider:'nebius',baseUrl:'https://example.invalid',reasoning:false,
  input:['text'],cost:usage.cost,contextWindow:8192,maxTokens:2048};
for(const mode of ['generic','structured-v1']){
  const f=fixture();let streamCalls=0;
  f.pi.exec=async()=>({code:2,killed:false,
    stdout:JSON.stringify(scopeError),stderr:'fixture-secret-stderr'});
  // Supplying this in-memory stream replaces all provider/network operations.
  const streamFn=()=>{
    streamCalls++;
    assert.ok(streamCalls<=2);
    const stopReason=streamCalls===1?'toolUse':'stop';
    const content=streamCalls===1?[{type:'toolCall',id:'synthetic-call',
      name:'plan_recovery',arguments:request}]:[{type:'text',text:'fixture done'}];
    const message={role:'assistant',content,api:model.api,provider:model.provider,
      model:model.id,usage,stopReason,timestamp:0};
    const stream=new EventStream(event=>event.type==='done',event=>event.message);
    queueMicrotask(()=>stream.push({type:'done',reason:stopReason,message}));
    return stream;
  };
  const stream=agentLoop([{role:'user',content:'Synthetic test',timestamp:0}],
    {systemPrompt:'',messages:[],tools:[createPlannerTool(f.pi,f.io,mode)]},
    {model,convertToLlm:messages=>messages},undefined,streamFn);
  const messages=await stream.result();
  assert.equal(streamCalls,2);
  const result=messages.find(message=>message.role==='toolResult');
  assert.equal(result.isError,true);
  const safe=JSON.parse(result.content[0].text);
  assert.equal(safe.error,mode==='generic'?'planner_failed':'unselected_active_demand');
  assert.ok(!JSON.stringify(result).includes('fixture-secret'));
  if(mode==='structured-v1')assert.deepEqual(safe.details.missing_sales_order_ids,[3,4,12]);
}
""", str(loop), str(ai))


def test_pinned_exec_cancels_real_process_on_timeout_and_signal(tmp_path):
    executable = ROOT / ".runtime/pi-smoke/packages/coding-agent/dist/core/exec.js"
    if not executable.is_file():
        pytest.skip("Build the pinned Pi smoke copy for child process checks.")
    run_node("""
const {execCommand}=await import(pathToFileURL(process.argv[2]));
const child=['-e','console.log(process.pid); setInterval(()=>{},1000)'];
for(const mode of ['timeout','signal']){
  const controller=new AbortController();
  const timer=mode==='signal'?setTimeout(()=>controller.abort(),200):undefined;
  const result=await execCommand(process.execPath,child,process.argv[3],{
    timeout:mode==='timeout'?200:2000,signal:controller.signal,
  });
  if(timer)clearTimeout(timer);
  assert.equal(result.killed,true);
  const pid=Number(result.stdout.trim());
  assert.ok(Number.isInteger(pid)&&pid>0);
  assert.throws(()=>process.kill(pid,0),error=>error.code==='ESRCH');
}
""", str(executable), str(tmp_path))
