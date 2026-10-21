"""Native recovery tool contract; fake exec only, no ERP or provider calls."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/forgeflow/agents/native_recovery.js"

PREAMBLE = """
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {default: register,createRecoveryTools,EXECUTE_SCHEMA}
  =await import(pathToFileURL(process.argv[1]));
const identity='0123456789abcdef'.repeat(4);
const request={target_product_code:'TOY-FIN',sales_order_ids:[1,2],
  anchor_date:'2026-01-01',margin:0.2,sales_order_budget_caps:{'1':50,'2':40},
  disruption:{kind:'supplier_cancellation',vendor_ref:'TOY-OLD'}};
const prepared={ok:true,odoo_writes:false,plan_id:identity,
  operation_counts:{keep:2,update:3,cancel:1,create:1},step_count:12,expected_new_spend:12.5};
const applied={ok:true,odoo_writes:true,plan_id:identity,verified_steps:12,
  execution_validated:true,benchmark_success:null};
function fixture(){
  const events=[],calls=[];
  const io={
    async mkdtemp(prefix){events.push(['mkdir',prefix]);return prefix+'a1B2c3'},
    async lstat(path){events.push(['stat',path]);return{
      isDirectory:()=>true,isSymbolicLink:()=>false}},
    async chmod(path,mode){events.push(['chmod',path,mode])},
    async writeFile(path,value,options){events.push(['write',path,value,options])},
  };
  const pi={async exec(command,args,options){
    calls.push({command,args,options});
    return {code:0,killed:false,stderr:'private-fixture-stderr',
      stdout:JSON.stringify(args[2]==='prepare'?prepared:applied)};
  }};
  return {io,pi,events,calls};
}
function stopped(error,phase,dispatched=false,expectedId){
  assert.ok(error instanceof Error);
  assert.ok(!error.message.includes('private-fixture'));
  const safe=JSON.parse(error.message);
  assert.equal(safe.ok,false);
  assert.equal(safe.error_code,phase==='prepare'?'recovery_prepare_failed':'recovery_execute_stopped');
  assert.equal(safe.odoo_writes,phase==='apply'&&dispatched);
  if(phase==='apply'){
    assert.equal(safe.plan_id,expectedId);
    if(dispatched){
      assert.ok(safe.message.includes('ERP state may have changed'));
      assert.ok(safe.message.includes('same plan_id'));
      assert.ok(safe.message.includes('Do not blindly retry'));
    }
  }
  return true;
}
"""


def run_node(script, *arguments):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to validate the Pi extension.")
    result = subprocess.run(
        [node, "--input-type=module", "-e", PREAMBLE + script, str(SOURCE), *arguments],
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_registers_sequential_prepare_and_execute_with_exact_schemas():
    run_node("""
const tools=[];register({registerTool:tool=>tools.push(tool)});
assert.deepEqual(tools.map(t=>t.name),['prepare_recovery','execute_recovery']);
assert.ok(tools.every(t=>t.executionMode==='sequential'));
assert.deepEqual([...tools[0].parameters.required].sort(),Object.keys(request).sort());
assert.equal(tools[0].parameters.additionalProperties,false);
assert.ok(tools[0].parameters.properties.disruption.anyOf.every(s=>s.additionalProperties===false));
assert.deepEqual(tools[1].parameters,EXECUTE_SCHEMA);
assert.deepEqual(EXECUTE_SCHEMA.required,['plan_id']);
assert.equal(EXECUTE_SCHEMA.additionalProperties,false);
assert.deepEqual(tools[0].prepareArguments(request),request);
assert.deepEqual(tools[1].prepareArguments({plan_id:identity}),{plan_id:identity});
assert.ok(tools[0].description.includes('total target state'));
assert.ok(tools[0].promptGuidelines.some(s=>s.includes('not additive order creation')));
assert.ok(tools[0].promptGuidelines.some(s=>s.includes('Do not fabricate receipts')));
assert.ok(tools[1].description.includes('does not assert external benchmark success'));
""")


@pytest.mark.parametrize("case", ["extra", "nested_extra", "budget_extra", "string_budget",
                                   "nan_budget", "duplicate_ids", "bad_date", "missing"])
def test_prepare_invalid_request_never_persists_or_executes(case):
    run_node("""
const f=fixture(),tool=createRecoveryTools(f.pi,f.io)[0],data=structuredClone(request);
switch(process.argv[2]){
  case 'extra':data.api_key='private-fixture-secret';break;
  case 'nested_extra':data.disruption.api_key='private-fixture-secret';break;
  case 'budget_extra':data.sales_order_budget_caps.api_key='private-fixture-secret';break;
  case 'string_budget':data.sales_order_budget_caps['1']='private-fixture-secret';break;
  case 'nan_budget':data.sales_order_budget_caps['1']=NaN;break;
  case 'duplicate_ids':data.sales_order_ids=[1,1];break;
  case 'bad_date':data.anchor_date='2026-02-30';break;
  case 'missing':delete data.margin;break;
}
assert.throws(()=>tool.prepareArguments(data),e=>stopped(e,'prepare'));
await assert.rejects(tool.execute('id',data),e=>stopped(e,'prepare'));
assert.equal(f.events.length,0);assert.equal(f.calls.length,0);
""", case)


@pytest.mark.parametrize("case", ["extra", "missing", "null", "path", "uppercase",
                                   "short", "long", "newline", "object", "shell"])
def test_execute_rejects_anything_except_exact_lowercase_hex_id(case):
    run_node("""
const f=fixture(),tool=createRecoveryTools(f.pi,f.io)[1];
let data={plan_id:identity};
switch(process.argv[2]){
  case 'extra':data.api_key='private-fixture-secret';break;
  case 'missing':data={};break;
  case 'null':data=null;break;
  case 'path':data.plan_id='/tmp/private-fixture';break;
  case 'uppercase':data.plan_id=identity.toUpperCase();break;
  case 'short':data.plan_id=identity.slice(1);break;
  case 'long':data.plan_id=identity+'a';break;
  case 'newline':data.plan_id=identity+'\\n';break;
  case 'object':data.plan_id={secret:'private-fixture'};break;
  case 'shell':data.plan_id='$(private-fixture)';break;
}
assert.throws(()=>tool.prepareArguments(data),e=>stopped(e,'apply'));
await assert.rejects(tool.execute('id',data),e=>stopped(e,'apply'));
assert.equal(f.events.length,0);assert.equal(f.calls.length,0);
""", case)


def test_success_uses_fixed_argv_private_request_and_only_exact_summary():
    run_node("""
const f=fixture(),tools=createRecoveryTools(f.pi,f.io),controller=new AbortController();
const data=structuredClone(request);data.target_product_code='literal;$(not-a-shell-command)';
let response=await tools[0].execute('prepare',data,controller.signal);
assert.deepEqual(response.details,prepared);
assert.deepEqual(JSON.parse(response.content[0].text),prepared);
const directory='/workspace/forgeflow-recovery-a1B2c3';
assert.deepEqual(f.calls[0],{command:'/opt/forgeflow/planner-env/bin/python',
  args:['-B','/opt/forgeflow/planner/recover.py','prepare','--request',directory+'/request.json'],
  options:{cwd:'/workspace',signal:controller.signal,timeout:120000}});
assert.deepEqual(f.events.find(e=>e[0]==='chmod'),['chmod',directory,0o700]);
const write=f.events.find(e=>e[0]==='write');
assert.equal(write[1],directory+'/request.json');
assert.deepEqual(JSON.parse(write[2]),data);
assert.deepEqual(write[3],{encoding:'utf8',mode:0o600,flag:'wx'});
const eventCount=f.events.length;
response=await tools[1].execute('apply',{plan_id:identity},controller.signal);
assert.deepEqual(response.details,applied);
assert.deepEqual(JSON.parse(response.content[0].text),applied);
assert.deepEqual(f.calls[1],{command:'/opt/forgeflow/planner-env/bin/python',
  args:['-B','/opt/forgeflow/planner/recover.py','apply','--autonomous','--plan-id',identity],
  options:{cwd:'/workspace',signal:controller.signal,timeout:300000}});
assert.equal(f.events.length,eventCount);
assert.equal(f.calls.length,2);
assert.ok(!JSON.stringify(response).includes('private-fixture'));
""")


@pytest.mark.parametrize("phase", ["prepare", "apply"])
@pytest.mark.parametrize("case", ["nonzero", "killed", "throw", "abort", "string_exit"])
def test_failures_redact_all_child_output_and_never_retry(phase, case):
    run_node("""
const [phase,which]=process.argv.slice(2),f=fixture(),controller=new AbortController();
let calls=0;
f.pi.exec=async(command,args,options)=>{
  calls++;assert.equal(options.signal,controller.signal);
  assert.equal(options.timeout,phase==='prepare'?120000:300000);
  if(which==='throw')throw Error('private-fixture-os-exception');
  if(which==='abort')controller.abort();
  return {code:which==='nonzero'?2:which==='string_exit'?'0':0,killed:which==='killed',
    stdout:JSON.stringify({ok:false,error_code:'private-fixture-secret',odoo_writes:false}),
    stderr:'private-fixture-stderr'};
};
const tool=createRecoveryTools(f.pi,f.io)[phase==='prepare'?0:1];
await assert.rejects(tool.execute('id',phase==='prepare'?request:{plan_id:identity},controller.signal),
  e=>stopped(e,phase,phase==='apply',phase==='apply'?identity:undefined));
assert.equal(calls,1);
""", phase, case)


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_preaborted_call_does_not_execute_or_persist(phase):
    run_node("""
const phase=process.argv[2],f=fixture(),controller=new AbortController();controller.abort();
const tool=createRecoveryTools(f.pi,f.io)[phase==='prepare'?0:1];
await assert.rejects(tool.execute('id',phase==='prepare'?request:{plan_id:identity},controller.signal),
  e=>stopped(e,phase,false,phase==='apply'?identity:undefined));
assert.equal(f.calls.length,0);assert.equal(f.events.length,0);
""", phase)


def test_abort_after_request_write_still_never_dispatches():
    run_node("""
const f=fixture(),controller=new AbortController(),original=f.io.writeFile;
f.io.writeFile=async(...args)=>{await original(...args);controller.abort()};
await assert.rejects(createRecoveryTools(f.pi,f.io)[0].execute('id',request,controller.signal),
  e=>stopped(e,'prepare'));
assert.equal(f.calls.length,0);
assert.equal(f.events.filter(e=>e[0]==='write').length,1);
""")


@pytest.mark.parametrize("case", ["escape", "newline", "symlink", "file", "fs_exception"])
def test_invalid_private_workspace_cannot_dispatch(case):
    run_node("""
const f=fixture(),which=process.argv[2];
if(which==='escape')f.io.mkdtemp=async()=>'/workspace/forgeflow-recovery-../private-fixture';
if(which==='newline')f.io.mkdtemp=async()=>'/workspace/forgeflow-recovery-a1B2c3\\n';
if(which==='symlink')f.io.lstat=async()=>({isDirectory:()=>true,isSymbolicLink:()=>true});
if(which==='file')f.io.lstat=async()=>({isDirectory:()=>false,isSymbolicLink:()=>false});
if(which==='fs_exception')f.io.chmod=async()=>{throw Error('private-fixture-fs-error')};
await assert.rejects(createRecoveryTools(f.pi,f.io)[0].execute('id',request),e=>stopped(e,'prepare'));
assert.equal(f.calls.length,0);
assert.equal(f.events.filter(e=>e[0]==='write').length,0);
""", case)


@pytest.mark.parametrize("phase", ["prepare", "apply"])
@pytest.mark.parametrize("case", ["extra", "missing", "bool_count", "negative_count", "unsafe_count",
                                   "leading_log", "trailing_log", "two_objects", "oversize",
                                   "unicode", "wrong_writes", "wrong_id", "wrong_success"])
def test_success_protocol_is_bounded_exact_and_typed(phase, case):
    run_node("""
const [phase,which]=process.argv.slice(2),f=fixture();
const data=structuredClone(phase==='prepare'?prepared:applied);
const counter=phase==='prepare'?'step_count':'verified_steps';let stdout;
switch(which){
  case 'extra':data.api_key='private-fixture-secret';break;
  case 'missing':delete data.plan_id;break;
  case 'bool_count':data[counter]=true;break;
  case 'negative_count':data[counter]=-1;break;
  case 'unsafe_count':data[counter]=Number.MAX_SAFE_INTEGER+1;break;
  case 'wrong_writes':data.odoo_writes=!data.odoo_writes;break;
  case 'wrong_id':data.plan_id=phase==='prepare'?identity+'\\n':'f'.repeat(64);break;
  case 'wrong_success':data.ok='true';break;
}
stdout=JSON.stringify(data);
if(which==='leading_log')stdout='private-fixture-log\\n'+stdout;
if(which==='trailing_log')stdout+='\\nprivate-fixture-log';
if(which==='two_objects')stdout+='\\n'+stdout;
if(which==='oversize')stdout=' '.repeat(16*1024)+stdout;
if(which==='unicode')stdout+='\u200b';
f.pi.exec=async()=>({code:0,killed:false,stdout,stderr:'private-fixture-stderr'});
const tool=createRecoveryTools(f.pi,f.io)[phase==='prepare'?0:1];
await assert.rejects(tool.execute('id',phase==='prepare'?request:{plan_id:identity}),
  e=>stopped(e,phase,phase==='apply',phase==='apply'?identity:undefined));
""", phase, case)


@pytest.mark.parametrize("case", ["extra_count", "missing_count", "fractional_count", "negative_spend",
                                   "infinite_spend", "string_spend", "false_validation", "benchmark_true"])
def test_nested_counts_spend_and_external_verifier_claims_are_rejected(case):
    run_node("""
const which=process.argv[2],f=fixture();
const phase=['false_validation','benchmark_true'].includes(which)?'apply':'prepare';
const data=structuredClone(phase==='prepare'?prepared:applied);
switch(which){
  case 'extra_count':data.operation_counts.secret='private-fixture';break;
  case 'missing_count':delete data.operation_counts.keep;break;
  case 'fractional_count':data.operation_counts.update=0.5;break;
  case 'negative_spend':data.expected_new_spend=-1;break;
  case 'infinite_spend':data.expected_new_spend=Infinity;break;
  case 'string_spend':data.expected_new_spend='private-fixture';break;
  case 'false_validation':data.execution_validated=false;break;
  case 'benchmark_true':data.benchmark_success=true;break;
}
f.pi.exec=async()=>({code:0,killed:false,stdout:JSON.stringify(data),stderr:'private-fixture-stderr'});
const tool=createRecoveryTools(f.pi,f.io)[phase==='prepare'?0:1];
await assert.rejects(tool.execute('id',phase==='prepare'?request:{plan_id:identity}),
  e=>stopped(e,phase,phase==='apply',phase==='apply'?identity:undefined));
""", case)


def test_pinned_pi_loader_registers_both_tools_and_validates_their_arguments():
    runtime = ROOT / ".runtime/pi-smoke/packages"
    paths = [runtime / "ai/dist/utils/validation.js",
             runtime / "coding-agent/dist/core/extensions/loader.js",
             runtime / "coding-agent/dist/core/event-bus.js"]
    if not all(path.is_file() for path in paths):
        pytest.skip("Build the pinned Pi smoke copy for loader integration.")
    run_node("""
const {validateToolArguments}=await import(pathToFileURL(process.argv[2]));
const {loadExtensions}=await import(pathToFileURL(process.argv[3]));
const {createEventBus}=await import(pathToFileURL(process.argv[4]));
const loaded=await loadExtensions([process.argv[1]],process.cwd(),createEventBus());
assert.equal(loaded.errors.length,0);assert.equal(loaded.extensions.length,1);
const tools=loaded.extensions[0].tools;
assert.deepEqual([...tools.keys()].sort(),['execute_recovery','prepare_recovery']);
for(const [name,args] of [['prepare_recovery',request],['execute_recovery',{plan_id:identity}]]){
  const tool=tools.get(name).definition;
  assert.equal(tool.executionMode,'sequential');
  assert.deepEqual(validateToolArguments(tool,{name,arguments:args}),args);
}
assert.throws(()=>validateToolArguments(tools.get('execute_recovery').definition,{
  name:'execute_recovery',arguments:{plan_id:identity+'\\n'},
}));
""", *(str(path) for path in paths))


def test_pinned_agent_loop_marks_stopped_execution_as_error_without_raw_output():
    runtime = ROOT / ".runtime/pi-smoke/packages"
    loop = runtime / "agent/dist/agent-loop.js"
    ai = runtime / "ai/dist/index.js"
    if not loop.is_file() or not ai.is_file():
        pytest.skip("Build the pinned Pi smoke copy for agent-loop integration.")
    run_node("""
const {agentLoop}=await import(pathToFileURL(process.argv[2]));
const {EventStream}=await import(pathToFileURL(process.argv[3]));
const f=fixture();let streamCalls=0,executions=0;
f.pi.exec=async()=>{
  executions++;
  return {code:2,killed:false,stdout:'private-fixture-stdout',stderr:'private-fixture-stderr'};
};
const usage={input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,
  cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const model={id:'synthetic',name:'synthetic',api:'openai-completions',provider:'nebius',
  baseUrl:'https://example.invalid',reasoning:false,input:['text'],cost:usage.cost,
  contextWindow:8192,maxTokens:2048};
// This in-memory stream replaces all provider/network operations.
const streamFn=()=>{
  streamCalls++;assert.ok(streamCalls<=2);
  const stopReason=streamCalls===1?'toolUse':'stop';
  const content=streamCalls===1?[{type:'toolCall',id:'synthetic-call',name:'execute_recovery',
    arguments:{plan_id:identity}}]:[{type:'text',text:'fixture done'}];
  const message={role:'assistant',content,api:model.api,provider:model.provider,
    model:model.id,usage,stopReason,timestamp:0};
  const stream=new EventStream(event=>event.type==='done',event=>event.message);
  queueMicrotask(()=>stream.push({type:'done',reason:stopReason,message}));
  return stream;
};
const stream=agentLoop([{role:'user',content:'Synthetic test',timestamp:0}],
  {systemPrompt:'',messages:[],tools:createRecoveryTools(f.pi,f.io)},
  {model,convertToLlm:messages=>messages},undefined,streamFn);
const messages=await stream.result();
assert.equal(streamCalls,2);assert.equal(executions,1);
const toolResult=messages.find(message=>message.role==='toolResult');
assert.equal(toolResult.isError,true);
const safe=JSON.parse(toolResult.content[0].text);
assert.equal(safe.error_code,'recovery_execute_stopped');
assert.equal(safe.plan_id,identity);assert.equal(safe.odoo_writes,true);
assert.ok(safe.message.includes('ERP state may have changed'));
assert.ok(!JSON.stringify(toolResult).includes('private-fixture'));
""", str(loop), str(ai))


@pytest.mark.parametrize("case", ["demand", "contract", "missing_offer", "change_binding",
                                  "change_quantity", "change_mapping", "change_generic", "unknown"])
def test_prepare_feedback_crosses_real_python_exit_and_js_boundary(tmp_path, monkeypatch, case):
    """The real CLI exits 2 after a real socket error; JS preserves only safe facts."""
    import json
    import sys
    import tempfile
    import threading

    from forgeflow.executor.daemon import ExecutorServer, Handlers
    from forgeflow.executor.handlers import RecoveryHandlers
    from forgeflow.executor.protocol import ProtocolError
    from forgeflow.planning.live import LiveInputError
    from forgeflow.recovery import service

    calls = []

    def reject(*args, **kwargs):
        calls.append("prepare")
        if case == "demand":
            raise LiveInputError(
                "private-fixture-secret", code="unselected_active_demand",
                details={"missing_sales_order_ids": [4, 5], "secret": "private-fixture-secret"},
            )
        if case == "contract":
            raise service.RecoveryError("plan_contract_violation")
        if case == "missing_offer":
            from recovery_synthetic import inputs

            from forgeflow.planning.live import build_blueprint

            request, snapshot, _, _ = inputs()
            request["disruption"] = {
                "kind": "supplier_cancellation", "vendor_ref": "private-fixture-secret",
                "offer_key": "private-fixture-secret",
            }
            # Exercise the real live error constructor, not a duplicated code.
            build_blueprint(snapshot, request)
            raise AssertionError("missing synthetic offer unexpectedly accepted")
        if case.startswith("change_"):
            from recovery_synthetic import inputs

            from forgeflow.recovery.change_set import ChangeSetError, build_change_set

            request, snapshot, blueprint, plan = inputs()
            if case == "change_binding":
                plan["blueprint_sha256"] = "0" * 64
            elif case == "change_quantity":
                plan["plan"]["purchase_orders"][0]["quantity"] += 1
            elif case == "change_mapping":
                plan["plan"]["purchase_orders"][0]["planned_arrival_days"] = 6
            else:
                raise ChangeSetError("private-fixture-secret: arbitrary offer id 123")
            # Exercise existing guards on synthetic records, without an ERP or provider.
            build_change_set(request, snapshot, blueprint, plan)
            raise AssertionError("invalid synthetic change set unexpectedly accepted")
        raise ProtocolError("private-fixture-secret", 422, reason="private-fixture-secret")

    monkeypatch.setattr(service, "prepare", reject)
    recovery = RecoveryHandlers(
        connection_factory=lambda: object(), store=tmp_path / "store",
        bundle=tmp_path / "bundle", require_grant=False,
    )
    with tempfile.TemporaryDirectory(prefix="ff-native-feedback-", dir="/tmp") as directory:
        server = ExecutorServer(
            Path(directory) / "s.sock", Handlers(recovery.prepare, recovery.apply, recovery.status),
            agent_uid=os.getuid(), peer_check=lambda *_: None,
        )
        server.bind()
        stop = threading.Event()
        worker = threading.Thread(target=server.serve_forever, args=(stop,), daemon=True)
        worker.start()
        request_path = tmp_path / "request.json"
        request_path.write_text(json.dumps({"synthetic": True}))
        try:
            run_node("""
const {spawnSync}=await import('node:child_process');
const f=fixture(),which=process.argv[7];let calls=0;
f.pi.exec=async()=>{
  calls++;
  const child=spawnSync(process.argv[2],['-c',
    'from forgeflow.recovery.service import main; raise SystemExit(main())',
    'prepare','--request',process.argv[5]],{
    env:{PATH:process.env.PATH,PYTHONPATH:process.argv[3],
      FORGEFLOW_EXECUTOR_SOCKET:process.argv[4],FORGEFLOW_RUN_ID:'synthetic-feedback',
      FORGEFLOW_ROUTE_LOG:process.argv[6]},encoding:'utf8',timeout:10000});
  assert.equal(child.status,2);
  return {code:child.status,killed:false,stdout:child.stdout,stderr:child.stderr};
};
await assert.rejects(createRecoveryTools(f.pi,f.io)[0].execute('id',request),error=>{
  stopped(error,'prepare');const safe=JSON.parse(error.message);
  const expected=which==='demand'
    ? {code:'plan_rejected',reason:'unselected_active_demand',missing_sales_order_ids:[4,5]}
    : which==='contract'
      ? {code:'plan_rejected',reason:'plan_contract_violation',missing_sales_order_ids:[]}
      : which==='missing_offer'
        ? {code:'plan_rejected',reason:'broken_offer_not_found',missing_sales_order_ids:[]}
        : which.startsWith('change_')
          ? {code:'plan_rejected',reason:({change_binding:'change_set_binding_mismatch',
            change_quantity:'change_set_supply_quantity_mismatch',
            change_mapping:'change_set_mapping_unproven',
            change_generic:'change_set_rejected'})[which],missing_sales_order_ids:[]}
        : {code:'prepare_rejected',reason:'details_unavailable',missing_sales_order_ids:[]};
  assert.deepEqual(safe.feedback,expected);
  assert.ok(!error.message.includes('private-fixture-secret'));
  return true;
});
assert.equal(calls,1);
""", sys.executable, str(ROOT / "src"), str(server.socket_path), str(request_path),
                     str(tmp_path / "route.jsonl"), case)
        finally:
            stop.set()
            worker.join(timeout=3)
            server.close()
        assert not worker.is_alive()
        assert calls == ["prepare"]
        assert not (tmp_path / "store").exists()


@pytest.mark.parametrize("case", ["extra", "code", "reason", "text_ids", "negative_ids",
                                   "oversized_ids", "wrong_reason", "duplicate_ids", "leading_log",
                                   "string_exit", "killed", "apply", "two_objects", "zero_exit",
                                   "change_wrong_code", "change_ids", "change_suffix", "change_newline"])
def test_prepare_feedback_rejects_untrusted_child_payload(case):
    run_node(r"""
const f=fixture(),which=process.argv[2];
const value={ok:false,error_code:'recovery_prepare_failed',odoo_writes:false,
 feedback:{code:'plan_rejected',reason:'unselected_active_demand',missing_sales_order_ids:[4,5]}};
if(which==='extra')value.feedback.secret='private-fixture-secret';
if(which==='code')value.feedback.code='private-fixture-secret';
if(which==='reason')value.feedback.reason='private-fixture-secret';
if(which==='text_ids')value.feedback.missing_sales_order_ids=['private-fixture-secret'];
if(which==='negative_ids')value.feedback.missing_sales_order_ids=[-1];
if(which==='oversized_ids')value.feedback.missing_sales_order_ids=Array.from({length:21},(_,i)=>i+1);
if(which==='wrong_reason')value.feedback.reason='plan_contract_violation';
if(which==='duplicate_ids')value.feedback.missing_sales_order_ids=[4,4];
if(which.startsWith('change_')){
 value.feedback.reason='change_set_mapping_unproven';
 value.feedback.missing_sales_order_ids=[];
}
if(which==='change_wrong_code')value.feedback.code='invalid_field';
if(which==='change_ids')value.feedback.missing_sales_order_ids=[123];
if(which==='change_suffix')value.feedback.reason+=':private-fixture-secret';
if(which==='change_newline')value.feedback.reason+='\nprivate-fixture-secret';
f.pi.exec=async()=>({code:which==='string_exit'?'2':which==='zero_exit'?0:2,killed:which==='killed',
 stdout:(which==='leading_log'?'private-fixture-secret\n':'')+JSON.stringify(value)
   +(which==='two_objects'?JSON.stringify(value):''),
 stderr:'private-fixture-secret'});
const apply=which==='apply';
await assert.rejects(createRecoveryTools(f.pi,f.io)[apply?1:0].execute('id',apply?{plan_id:identity}:request),error=>{
 stopped(error,apply?'apply':'prepare',apply,apply?identity:undefined);
 assert.equal(JSON.parse(error.message).feedback,undefined);
 return true;
});
""", case)


def test_python_and_javascript_feedback_allowlists_remain_identical():
    import json
    import re

    from forgeflow.recovery.change_set import CHANGE_SET_REASONS as SOURCE_CHANGE_SET_REASONS
    from forgeflow.recovery.client import CHANGE_SET_REASONS, PREPARE_ERROR_CODES, PREPARE_REASONS

    source = SOURCE.read_text()
    assert CHANGE_SET_REASONS == SOURCE_CHANGE_SET_REASONS
    for name, expected in (("PREPARE_ERROR_CODES", PREPARE_ERROR_CODES),
                           ("CHANGE_SET_REASONS", CHANGE_SET_REASONS),
                           ("PREPARE_REASONS", PREPARE_REASONS)):
        match = re.search(r"const " + name + r" = new Set\((\[[\s\S]*?\])\)", source)
        assert match is not None
        encoded = re.sub(r",\s*]$", "]", match.group(1))
        assert set(json.loads(encoded)) == expected
