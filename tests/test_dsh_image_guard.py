"""Execute image authorization through the real DSH tool runtime, without a model."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forgeflow.api.dsh_harness import DshHarness
from forgeflow.api.dsh_permissions import scoped_image_root
from forgeflow.vps_runtime import dsh_patch

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "src/forgeflow/api/dsh_image_guard.mjs"


def installed_runtime():
    node = shutil.which("node")
    cli = Path(os.environ.get("FORGEFLOW_DSH_CLI") or
               REPO / ".runtime/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js")
    modules = cli.parent.parent.parent.parent
    if node is None or not (modules / "@deepseek-ai/dsh-tools/lib/index.js").is_file():
        pytest.skip("pinned DSH Node runtime not installed")
    return node, cli, modules


def test_owner_image_scope_survives_evidence_revision_and_disables_unguarded_tools(tmp_path):
    patch = tmp_path / "base.json"
    patch.write_text(json.dumps([{"insert": [{"id": "mcp-forgeflow",
        "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "forgeflow", "transport": "stdio"}}]},
        {"id": "tool-fs", "disabled": False, "config": {"read": True, "readImage": True}}]))
    cli = tmp_path / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    harness = DshHarness(cli=cli, patch=patch, home=tmp_path / "home", workspace=tmp_path)
    first = harness.with_evidence(tmp_path / f"{'a' * 64}-1111111111111111.json")
    revised = harness.with_evidence(tmp_path / f"{'a' * 64}-2222222222222222.json")
    other = harness.with_evidence(tmp_path / f"{'b' * 64}-1111111111111111.json")
    assert first.image_root == revised.image_root
    assert first.image_root != other.image_root
    rows = json.loads(first.patch.read_text())
    assert [row for row in rows if row.get("id") == "tool-fs"][-1]["disabled"] is True
    guard = next(row["insert"][0] for row in rows
                 if row.get("insert", [{}])[0].get("id") == "forgeflow-image-guard")
    assert guard["name"] == GUARD.as_uri()
    assert guard["config"]["root"] == str(first.image_root)
    assert guard["config"]["toolModule"] == (
        tmp_path / "node_modules/@deepseek-ai/dsh-tool-fs/lib/index.js").as_uri()
    assert scoped_image_root(tmp_path, Path("../../scope.json")).is_relative_to(tmp_path)


def test_actual_dsh_image_tool_enforces_owner_scope(tmp_path):
    node, _cli, modules = installed_runtime()
    script = r"""
import assert from 'node:assert/strict';
import { mkdir, writeFile, symlink } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const [modules, guardPath, root] = process.argv.slice(2);
const pkg = async name => import(pathToFileURL(join(modules, '@deepseek-ai', name, 'lib/index.js')));
const { Context } = await pkg('cordis');
const { default: SystemPrompt } = await pkg('dsh-system-prompt');
const { default: ToolRuntime } = await pkg('dsh-tools');
const { default: LocalFileSystem } = await pkg('dsh-fs-local');
const { default: LocalAttachmentStore } = await pkg('dsh-attachment-local');
const fileTools = await pkg('dsh-tool-fs');
const guard = await import(pathToFileURL(guardPath));
const owner = join(root, 'owner-a'), other = join(root, 'owner-b');
await mkdir(owner); await mkdir(other);
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAACXBIWXMAAAPoAAAD6AG1e1JrAAAAFElEQVQImWM4Y8xwxpgBhM8YMwAAHyID/fO6hNcAAAAASUVORK5CYII=', 'base64');
const ownFile = join(owner, 'allowed.png'), otherFile = join(other, 'private.png');
await writeFile(ownFile, png); await writeFile(otherFile, png);
await symlink(otherFile, join(owner, 'escape.png'));
const toolModule = pathToFileURL(join(modules, '@deepseek-ai/dsh-tool-fs/lib/index.js')).href;
let sequence = 0;
async function runtime(guarded, imageRoot=owner) {
  const ctx = new Context();
  await ctx.plugin(SystemPrompt, {}); await ctx.plugin(ToolRuntime, {});
  await ctx.plugin(LocalFileSystem, {cwd: owner});
  await ctx.plugin(LocalAttachmentStore, {dshHome:join(root, 'home-' + sequence++)});
  ctx.provide('workingDirectory', {ensure:async()=>owner});
  ctx.provide('llm', {resolveModelInfo:async()=>({inputModalities:['image']})});
  if (guarded) await ctx.plugin(guard, {root: imageRoot, toolModule});
  else await ctx.plugin(fileTools, {read:false,readImage:true,write:false,edit:false});
  return ctx;
}
const agent = {options:{provider:'synthetic',model:'synthetic'},session:{requestHeader(){return undefined;}}};
async function read(ctx, args) {
  return ctx.tools.execute({name:'read_image',callId:'call-' + sequence++,arguments:args,
    signal:new AbortController().signal,agent});
}
const unguarded = await runtime(false);
const baseline = await read(unguarded, {file_path:otherFile});
assert.equal(baseline.isError ?? false, false);
assert(baseline.content.some(block=>block.type==='image'));
const guarded = await runtime(true);
const allowed = await read(guarded, {file_path:ownFile});
assert.equal(allowed.isError ?? false, false, JSON.stringify(allowed));
assert(allowed.content.some(block=>block.type==='image'));
const deniedArgs = [
  {file_path:otherFile}, {file_path:'../owner-b/private.png'}, {file_path:'allowed.png'},
  {file_path:pathToFileURL(otherFile).href}, {file_path:'attachment://private'},
  {file_path:join(owner,'escape.png')}, {file_path:owner + '/../owner-b/private.png'},
  {file_path:join(root,'owner-a-extra','private.png')}, {path:otherFile}, {},
];
for (const args of deniedArgs) {
  const denied = await read(guarded, args);
  assert.equal(denied.isError, true, JSON.stringify({args,denied}));
  assert.equal(denied.error.message, 'image_access_denied');
  assert(!denied.content.some(block=>block.type==='image'));
}
// An absent root or incomplete configuration cannot accidentally enable reads.
const absent = await runtime(true, join(root,'missing-root'));
const missingRoot = await read(absent,{file_path:ownFile});
assert.equal(missingRoot.isError, true);
assert.equal(missingRoot.error.message, 'image_access_denied');
for (const config of [{}, {root:''}, {root:'relative',toolModule}, {root:owner}]) {
  await assert.rejects(guard.apply({},config), /image_scope_required/);
}
assert.equal(guarded.tools.get('read'), undefined);
assert.equal(guarded.tools.get('write'), undefined);
assert.equal(guarded.tools.get('edit'), undefined);
console.log(JSON.stringify({baseline_cross_owner_read:true,owner_read:true,denied:deniedArgs.length}));
"""
    completed = subprocess.run([node, "--input-type=module", "-", str(modules), str(GUARD), str(tmp_path)],
                               input=script, text=True, capture_output=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "baseline_cross_owner_read": True, "owner_read": True, "denied": 10,
    }


def test_scoped_patch_loads_image_guard_through_official_composition(tmp_path):
    node, cli, modules = installed_runtime()
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    base = tmp_path / "base.json"
    base.write_text(json.dumps(dsh_patch(
        {"python": sys.executable, "repo_root": str(REPO)},
        "guard-synthetic", "https://example.invalid/v1", provider="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
    )))
    evidence = tmp_path / f"{'a' * 64}-1111111111111111.json"
    evidence.write_text(json.dumps({"schema_version": 1, "scope_id": "a" * 64,
                                   "cases": [], "runs": [], "run_details": []}))
    scoped = DshHarness(cli=cli, patch=base, home=home, workspace=workspace).with_evidence(evidence)
    # Exercise the shipped SDK profile and generated owner patch through its
    # official loader, with the interactive stdin server disabled for this probe.
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps([
        {"id": "sdk-jsonrpc-server", "disabled": True},
        {"id": "sdk-app-startup", "disabled": True},
    ]))
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "DSH_HOME": str(home)}
    dumped = subprocess.run(
        [node, str(cli), "--profile", "sdk", "--patch", str(scoped.patch),
         "--patch", str(probe), "--dump-config"],
        cwd=workspace, env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert dumped.returncode == 0, dumped.stderr
    composed = tmp_path / "composed.yml"
    composed.write_text(dumped.stdout)
    script = r"""
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const [bootPath, config, base] = process.argv.slice(2);
const {boot} = await import(pathToFileURL(bootPath));
const ctx = await boot('forgeflow-image-test', config, [], undefined, pathToFileURL(base).href);
try {
  assert(ctx.tools.get('read_image'));
  for (const name of ['read', 'write', 'edit']) assert.equal(ctx.tools.get(name), undefined);
  const result = await ctx.tools.execute({name:'read_image',callId:'foreign-path',
    arguments:{file_path:'/unauthorized-owner/private.png'},signal:new AbortController().signal});
  assert.equal(result.isError, true);
  assert.equal(result.error.message, 'image_access_denied');
  console.log(JSON.stringify({guard_loaded:true,unguarded_file_tools:false,foreign_read_denied:true}));
} finally { await ctx.fiber.dispose(); }
"""
    loaded = subprocess.run(
        [node, "--input-type=module", "-", str(modules / "@deepseek-ai/dsh-app-boot/lib/index.js"),
         str(composed), str(cli)], input=script, cwd=workspace, env=env,
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    assert json.loads(loaded.stdout.strip().splitlines()[-1]) == {
        "guard_loaded": True, "unguarded_file_tools": False, "foreign_read_denied": True,
    }
