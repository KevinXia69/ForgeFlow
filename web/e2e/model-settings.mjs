/** Real browser UI with synthetic API transport; never calls a model provider. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const presets = JSON.parse(await readFile(path.join(root, 'src/modelProfiles.json'), 'utf8'));
const historical = JSON.parse(await readFile(path.join(root, 'src/historicalDemo.json'), 'utf8'));
const liveCase = { ...historical.case, case_id: 'synthetic-live-case' };
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
await server.listen();
const base = `http://127.0.0.1:${server.httpServer.address().port}`;
const browser = await chromium.launch({ headless: true,
  ...(process.env.CHROME_EXECUTABLE_PATH ? { executablePath: process.env.CHROME_EXECUTABLE_PATH } : { channel: 'chrome' }) });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
page.setDefaultTimeout(15_000);
const pageErrors = [], unexpected = [], requests = [], testRequests = [];
let platform = { configured: true, profile: 'deepseek_flash', model: 'deepseek-flash',
  base_url: 'https://api.deepseek.com/v1', can_override: true };
let loggedIn = true, fallback = false;
page.on('pageerror', error => pageErrors.push(error.message));
await context.route('**/*', async route => {
  const req = route.request(), url = new URL(req.url());
  if (url.origin !== base) { unexpected.push('external'); return route.abort(); }
  if (!url.pathname.startsWith('/api/')) return route.continue();
  const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  if (url.pathname === '/api/auth/me') return json(loggedIn
    ? { scope_id: 'synthetic-provider-scope', deployment: 'hosted', display_name: 'Synthetic reviewer' }
    : { detail: 'authentication_required' }, loggedIn ? 200 : 401);
  if (url.pathname === '/api/config') return json({ deployment: 'hosted', runner_enabled: false, accounts_enabled: false });
  if (url.pathname === '/api/settings/model') return json(platform);
  if (url.pathname === '/api/settings/model/test') { testRequests.push(req.postDataJSON()); return json({ ok: true, latency_ms: 7 }); }
  if (url.pathname === '/api/auth/session' && req.method() === 'DELETE') {
    loggedIn = false; return route.fulfill({ status: 204 });
  }
  if (url.pathname === '/api/browser-reports') return route.fulfill({ status: 204 });
  if (url.pathname.startsWith('/api/assistant/') || /^\/api\/product\/cases\/[^/]+\/ask(?:\/stream)?$/.test(url.pathname)) {
    const body = req.postDataJSON(); requests.push({ path: url.pathname, body });
    if (fallback && url.pathname.endsWith('/stream')) return json({ detail: 'stream_unavailable' }, 503);
    const answer = { type: 'final', answer: `Synthetic reply ${requests.length}`, citations: [], passages: [], mode: 'harness' };
    return url.pathname.endsWith('/stream')
      ? route.fulfill({ status: 200, contentType: 'text/event-stream', body: `data: ${JSON.stringify(answer)}\n\n` })
      : json(answer);
  }
  if (req.method() !== 'GET') { unexpected.push(url.pathname); return route.abort(); }
  if (url.pathname === '/api/environment') return json({ available: true });
  if (url.pathname === '/api/capabilities') return json({});
  if (url.pathname === '/api/product/cases/synthetic-live-case') return json(liveCase);
  if (url.pathname === '/api/product/cases') return json([liveCase]);
  return json([]);
});

const checks = [];
const check = (condition, label) => { assert.ok(condition, label); checks.push(label); };
const open = async () => {
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.locator('.settings-dialog').waitFor();
  await page.getByRole('switch', { name: 'Use my own key' }).waitFor();
};
const select = async (selector, label) => {
  await page.locator(selector).click();
  await page.getByRole('option', { name: label, exact: true }).click();
};
const send = async text => {
  await page.locator('.composer [contenteditable="true"]').fill(text);
  const count = requests.length;
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.composer [aria-label="Stop"]'));
  await page.getByText(`Synthetic reply ${count + (fallback ? 2 : 1)}`, { exact: true }).waitFor();
};

try {
  await page.goto(base + '/app/');
  await page.locator('[data-home-view]').waitFor();
  await page.getByText('DeepSeek V4.1 Flash 1017', { exact: true }).waitFor();
  await send('Synthetic default-model route check');
  check(requests.at(-1).path.endsWith('/stream') && !('model' in requests.at(-1).body), 'default uses stream without model override');
  await open();
  check(await page.getByRole('switch', { name: 'Use my own key' }).getAttribute('aria-checked') === 'false', 'default requires no user key');
  await page.getByRole('switch', { name: 'Use my own key' }).click();
  for (const preset of Object.values(presets)) {
    await select('#settings-model-choice', preset.label);
    check(await page.locator('#settings-base-url').inputValue() === preset.base_url, `${preset.label}: endpoint`);
    check(await page.locator('#settings-model').inputValue() === preset.model, `${preset.label}: model`);
    check(await page.locator('#settings-api-key').inputValue() === '', `${preset.label}: previous key cleared`);
  }
  await select('#settings-model-choice', 'DeepSeek');
  await select('#settings-api-protocol', 'OpenAI Responses');
  check(await page.locator('#settings-base-url').inputValue() === presets.deepseek_flash.endpoints['openai-responses'], 'DeepSeek Responses uses its explicit endpoint');
  await select('#settings-api-protocol', 'Anthropic Messages');
  check(await page.locator('#settings-base-url').inputValue() === presets.deepseek_flash.endpoints['anthropic-messages'], 'protocol changes to its actual endpoint');
  await page.locator('#settings-model').fill('a-different-model');
  check(!await page.locator('#settings-image-input').isChecked(), 'unknown model does not inherit image capability');
  await page.locator('#settings-base-url').fill('http://127.0.0.1:11434/v1');
  await page.locator('#settings-api-key').fill('synthetic-test-key');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByText(/Use a public HTTPS API URL/).waitFor();
  check(testRequests.length === 0, 'local HTTP rejected before outbound connection test');
  await select('#settings-model-choice', 'OpenAI');
  await page.locator('#settings-api-key').fill('synthetic-test-key');
  await page.getByRole('button', { name: 'Test connection', exact: true }).click();
  await page.getByText('Connection OK (7 ms).', { exact: true }).waitFor();
  check(testRequests.at(-1).model.api === 'openai-responses', 'connection test sends selected protocol');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByText('Enabled for this tab until refresh or logout.', { exact: true }).waitFor();
  check(await page.evaluate(() => !JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]).includes('synthetic-test-key')), 'BYOK absent from durable browser storage');
  await page.locator('.settings-close').click();
  await send('Synthetic provider stream check');
  check(requests.at(-1).path.endsWith('/stream') && requests.at(-1).body.model.provider === 'openai'
    && requests.at(-1).body.model.api === 'openai-responses', 'BYOK preserves stream and protocol');
  fallback = true;
  await send('Synthetic stream fallback check');
  check(requests.at(-1).path.endsWith('/answer') && requests.at(-1).body.model.api_key === 'synthetic-test-key', 'fallback retains the same BYOK configuration');
  fallback = false;
  await page.evaluate(() => { location.hash = '#case=synthetic-live-case&chat'; });
  await page.locator('.case-chat-page').waitFor();
  await send('Synthetic live case route check');
  check(requests.at(-1).path.endsWith('/ask/stream') && requests.at(-1).body.model.api === 'openai-responses', 'live case uses the same BYOK stream');
  await send('Synthetic live case follow-up');
  check(requests.at(-1).body.history?.length > 0, 'live case includes conversation history');
  await page.evaluate(id => { location.hash = `#case=${id}&chat`; }, historical.case.case_id);
  await page.locator('.case-chat-page').waitFor();
  const countBeforeHistory = requests.length;
  await page.locator('.composer [contenteditable="true"]').fill('What is the impact scope?');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByText(/13 units/, { exact: false }).first().waitFor();
  check(requests.length === countBeforeHistory, 'historical example remains local with BYOK configured');
  await page.evaluate(() => { location.hash = ''; });
  await page.locator('[data-home-view]').waitFor();
  platform = { ...platform, profile: 'new-server-profile', model: 'actual-server-model' };
  await page.reload();
  await page.getByText('actual-server-model', { exact: true }).waitFor();
  await send('Synthetic post-refresh default check');
  check(!('model' in requests.at(-1).body), 'refresh clears key and restores actual unknown platform model');
  await open();
  await page.getByRole('switch', { name: 'Use my own key' }).click();
  await select('#settings-model-choice', 'Anthropic');
  await page.locator('#settings-api-key').fill('synthetic-test-key');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await page.getByText('Enabled for this tab until refresh or logout.', { exact: true }).waitFor();
  const held = await page.evaluate(async () => {
    const module = await import('/app/src/modelConfig.ts');
    return Boolean(module.loadModelConfig('synthetic-provider-scope', 'hosted'));
  });
  check(held, 'hosted override exists only in this document memory');
  const guarded = await page.evaluate(async () => {
    const module = await import('/app/src/modelConfig.ts');
    const unsafe = ['http://127.0.0.1/v1', 'https://127.0.0.1/v1', 'https://localhost/v1',
      'https://10.0.0.1/v1', 'https://user:pass@example.com/v1', 'https://example.com/v1?key=x'];
    return unsafe.every(value => !module.isPublicModelEndpoint(value))
      && module.isPublicModelEndpoint('https://api.openai.com/v1')
      && module.supportsModelImages({ profile: 'custom', base_url: '', model: '', api_key: '' })
      && !module.supportsModelImages({ image_input: false });
  });
  check(guarded, 'endpoint and capability guards preserve legacy default compatibility');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('[data-home-view]'));
  const retired = await page.evaluate(async () => {
    const module = await import('/app/src/modelConfig.ts');
    return module.loadModelConfig('synthetic-provider-scope', 'hosted');
  });
  check(retired === null, 'logout clears BYOK');
  check(pageErrors.length === 0 && unexpected.length === 0, 'no page errors or external/business writes');
  console.log(JSON.stringify({ ok: true, checks: checks.length, scope: 'real Chrome UI; synthetic API transport; no providers or ERP' }));
} finally {
  await browser.close();
  await server.close();
}
