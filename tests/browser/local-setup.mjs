/** Real setup browser round-trip against an isolated, temporary Python server. */
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const playwrightPath = process.env.FORGEFLOW_PLAYWRIGHT_ROOT || path.join(root, 'web/node_modules/playwright-core');
const { chromium } = await import(pathToFileURL(path.join(playwrightPath, 'index.mjs')));
if (!process.env.FORGEFLOW_SETUP_PROFILE) {
  for (const profile of ['deepseek_flash', 'nemotron', 'openai', 'anthropic', 'google', 'openrouter', 'dashscope', 'siliconflow', 'ollama', 'custom']) {
    const result = spawnSync(process.execPath, [fileURLToPath(import.meta.url)], {
      env: { ...process.env, FORGEFLOW_SETUP_PROFILE: profile }, stdio: 'inherit', timeout: 60_000,
    });
    assert.equal(result.status, 0, `Setup browser failed for ${profile}`);
  }
  process.exit(0);
}
const profile = process.env.FORGEFLOW_SETUP_PROFILE;
assert.ok(['deepseek_flash', 'nemotron', 'openai', 'anthropic', 'google', 'openrouter', 'dashscope', 'siliconflow', 'ollama', 'custom'].includes(profile));
const directory = await mkdtemp(path.join(tmpdir(), 'forgeflow-setup-browser-'));
const fixture = `
import json,sys
from pathlib import Path
from http.server import ThreadingHTTPServer
from forgeflow.local_workbench.setup import SetupState,make_handler
async def test_connection(connection,key): return {'ok':bool(key),'model':connection['model'],'protocol':connection['api']}
root=Path(sys.argv[1]);state=SetupState(root/'config/runtime.json',mounts={name:root/name for name in ('control','odoo','odoo_config','qdrant')},connection_tester=test_connection)
state.issue()
server=ThreadingHTTPServer(('127.0.0.1',17801),make_handler(state))
print(json.dumps({'url':state.url()}),flush=True)
server.serve_forever()
`;
const child = spawn(process.env.FORGEFLOW_TEST_PYTHON || 'python3', ['-c', fixture, directory], {
  cwd: root, env: { ...process.env, PYTHONPATH: path.join(root, 'src') }, stdio: ['ignore', 'pipe', 'pipe'],
});
let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let output = '';
    const timeout = setTimeout(() => reject(new Error('Setup fixture did not start')), 20_000);
    child.once('exit', () => { clearTimeout(timeout); reject(new Error('Setup fixture exited')); });
    child.stdout.on('data', chunk => {
      output += chunk;
      if (output.includes('\n')) { clearTimeout(timeout); resolve(JSON.parse(output.split('\n')[0]).url); }
    });
    child.stderr.on('data', () => {}); // Do not forward arbitrary server diagnostics or form values.
  });
  browser = await chromium.launch({ headless: true, channel: process.env.CHROME_CHANNEL || 'chrome',
    ...(process.env.CHROME_EXECUTABLE_PATH ? { executablePath: process.env.CHROME_EXECUTABLE_PATH } : {}) });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:17801/') ? route.continue() : route.abort());
  await page.goto(url);
  await page.waitForFunction(() => !document.querySelector('#fields').disabled);
  assert.equal(new URL(page.url()).hash, '');
  assert.equal(await page.locator('#search-key-group').isVisible(), false);
  await page.locator('#profile').selectOption('nemotron');
  assert.equal(await page.locator('#search-key-group').isVisible(), true);
  assert.equal(await page.locator('#deepseek_search_api_key').evaluate(input => input.required), true);
  await page.locator('#profile').selectOption(profile);
  if (profile === 'custom') {
    await page.locator('#base_url').fill('https://gateway.example/v1');
    await page.locator('#custom_endpoint').check();
    await page.locator('#model').fill('synthetic-model');
  }
  if (profile !== 'deepseek_flash') await page.locator('#deepseek_search_api_key').fill('search-synthetic-12345');
  else assert.equal(await page.locator('#deepseek_search_api_key').isEnabled(), false);
  await page.locator('#model_api_key').fill('chat-synthetic-12345');
  await page.locator('#dashscope_api_key').fill('sk-docmind-synthetic-12345');
  await page.locator('#siliconflow_api_key').fill('semantic-synthetic-12345');
  assert.equal(await page.locator('#save').isEnabled(), false);
  const tested = page.waitForResponse(response => response.url().endsWith('/test'));
  await page.locator('#test').click();
  assert.equal((await tested).status(), 200);
  await page.waitForFunction(() => !document.querySelector('#save').disabled);
  const saved = page.waitForResponse(response => response.url().endsWith('/configure'));
  await page.getByRole('button', { name: 'Save and start / 保存并启动' }).click();
  assert.equal((await saved).status(), 200);
  await page.locator('#done').waitFor({ state: 'visible' });
  const config = JSON.parse(await readFile(path.join(directory, 'config/runtime.json'), 'utf8'));
  assert.equal(config.env.DEEPSEEK_API_KEY, profile !== 'deepseek_flash' ? 'search-synthetic-12345' : 'chat-synthetic-12345');
  if (profile === 'nemotron') assert.equal(config.env.NEBIUS_API_KEY, 'chat-synthetic-12345');
  assert.equal(config.model_connection.preset, profile);
  assert.equal(config.env.FORGEFLOW_CHAT_API_KEY, 'chat-synthetic-12345');
  if (profile === 'google') assert.equal(config.model_connection.catalog_native, true);
  assert.equal(await page.locator('#invite').textContent(), config.env.FORGEFLOW_INVITE_CODES);
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  assert.equal(await page.locator('#model_api_key').inputValue(), '');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  assert.deepEqual(errors, []);
  const replay = await page.evaluate(async token => {
    const response = await fetch('/configure', { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-ForgeFlow-Setup': token,
    }, body: '{}' });
    return response.status;
  }, new URL(url).hash.slice(1));
  assert.equal(replay, 403);
  console.log(JSON.stringify({ ok: true, profile, checks: ['conditional-search-credential', 'tested-settings-required', 'real-form-submit', 'fragment-removed', 'credentials-written-privately',
    'invite-displayed', 'no-browser-storage', 'inputs-cleared', 'mobile-no-overflow', 'CSP-script-works', 'replay-rejected'],
    scope: 'isolated local Python HTTP server, real Chrome; synthetic connection-test result and keys; no provider or Docker calls' }));
} finally {
  await browser?.close();
  child.kill('SIGTERM');
  await new Promise(resolve => child.exitCode !== null ? resolve() : child.once('exit', resolve));
  await rm(directory, { recursive: true, force: true });
}
