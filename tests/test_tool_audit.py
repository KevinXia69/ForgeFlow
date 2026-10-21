"""Controlled Pi API fixtures only; no pinned runtime, provider, ERP or Docker."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/forgeflow/agents/tool_audit.js"
RECOVERY = ROOT / "src/forgeflow/agents/native_recovery.js"
PREAMBLE = """
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const audit=await import(pathToFileURL(process.argv[1]));
const {default:recovery}=await import(pathToFileURL(process.argv[2]));
function fixture(){
 const tools=[],hooks=new Map(),records=[];
 let active=[];
 const pi={
  registerTool(tool){tools.push({...tool,sourceInfo:{path:'/opt/forgeflow/native_recovery.js'}})},
  on(name,fn){hooks.set(name,fn)},getAllTools:()=>tools,getActiveTools:()=>active,
 };
 recovery(pi);active=tools.map(tool=>tool.name);
 return {pi,tools,hooks,records,setActive:value=>{active=value}};
}
"""


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the controlled audit extension test.")
    return subprocess.run(
        [node, "--input-type=module", "-e", PREAMBLE + script, str(SOURCE), str(RECOVERY)],
        env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=10,
    )


def test_audit_reads_registered_objects_and_active_set_at_each_hook():
    result = run_node("""
const f=fixture();
audit.installToolAudit(f.pi,row=>f.records.push(row),()=> '2026-10-23T00:00:00.000Z');
assert.deepEqual([...f.hooks.keys()],['session_start','before_agent_start','before_provider_request']);
assert.equal(f.hooks.get('session_start')({}),undefined);
assert.equal(f.records[0].status,'complete');
assert.deepEqual(f.records[0].active_tools.map(tool=>tool.name),['prepare_recovery','execute_recovery']);
const originalHash=f.records[0].registered_tools[0].schema_sha256;
assert.equal(originalHash,audit.schemaHash(f.tools[0].parameters));
assert.ok(f.records[0].registered_tools.every(tool=>tool.source==='native_recovery'));
f.setActive(['prepare_recovery']);
f.tools[0].parameters=structuredClone(f.tools[0].parameters);
f.tools[0].parameters.required=f.tools[0].parameters.required.slice(1);
const input={prompt:'private-canary raw prompt',systemPrompt:'private-canary system prompt',
 systemPromptOptions:{contextFiles:[{path:'/private-canary'}],skills:[]}};
assert.equal(f.hooks.get('before_agent_start')(input),undefined);
assert.notEqual(f.records[1].registered_tools[0].schema_sha256,originalHash);
assert.equal(f.records[1].active_count,1);
assert.equal(f.records[1].registered_tools[1].active,false);
assert.equal(f.records[1].input.context_files_count,1);
assert.match(f.records[1].input.prompt_sha256,/^[a-f0-9]{64}$/);
// The provider payload is deliberately inaccessible: the audit never reads it.
const event={get payload(){throw Error('private-canary payload access')}};
assert.equal(f.hooks.get('before_provider_request')(event),undefined);
assert.equal(f.records[2].input_snapshot_index,2);
assert.equal(f.records[2].provider_receipt_confirmed,false);
assert.ok(!JSON.stringify(f.records).includes('private-canary'));
assert.equal(input.prompt,'private-canary raw prompt');
""")
    assert result.returncode == 0, result.stderr


def test_unknown_names_sources_and_unavailable_registry_do_not_leak_text():
    result = run_node("""
const f=fixture();
f.tools.push({name:'private-canary-tool',parameters:{type:'object',description:'private-canary'},
 description:'private-canary description',sourceInfo:{source:'private-canary',path:'/private-canary'}});
f.setActive(['private-canary-tool']);
const row=audit.registrySnapshot(f.pi);
assert.equal(row.status,'complete');assert.equal(row.registered_count,3);
assert.equal(row.active_tools[0].name,null);
assert.equal(row.registered_tools[2].source,'unrecognized');
assert.match(row.active_tools[0].name_sha256,/^[a-f0-9]{64}$/);
assert.ok(!JSON.stringify(row).includes('private-canary'));
f.pi.getAllTools=()=>{throw Error('private-canary stack')};
assert.deepEqual(audit.registrySnapshot(f.pi),{status:'unavailable',reason:'registry_unavailable',
 registered_tools:null,active_tools:null,registered_count:null,active_count:null});
""")
    assert result.returncode == 0, result.stderr


def test_missing_schema_duplicate_names_and_inconsistent_active_set_remain_unknown():
    result = run_node("""
const f=fixture();f.setActive(['read']);
assert.equal(audit.registrySnapshot(f.pi).status,'incomplete');
f.setActive(['prepare_recovery']);delete f.tools[0].parameters;
const row=audit.registrySnapshot(f.pi);
assert.equal(row.status,'incomplete');assert.equal(row.registered_tools[0].schema_sha256,null);
f.tools.push(f.tools[0]);
assert.equal(audit.registrySnapshot(f.pi).status,'unavailable');
assert.equal(audit.schemaHash({a:1,b:{d:2,c:3}}),audit.schemaHash({b:{c:3,d:2},a:1}));
assert.notEqual(audit.schemaHash({a:1}),audit.schemaHash({a:2}));
const cyclic={};cyclic.self=cyclic;
assert.equal(audit.schemaHash(cyclic),null);
assert.equal(audit.schemaHash({bad:Infinity}),null);
""")
    assert result.returncode == 0, result.stderr


def test_audit_sink_failure_stops_with_fixed_message_not_underlying_exception():
    result = run_node("""
const f=fixture();
audit.installToolAudit(f.pi,()=>{throw Error('private-canary path credentials')});
f.hooks.get('before_provider_request')({});
throw Error('must not continue');
""")
    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr == "ForgeFlow: unable to record runtime tool audit.\n"
