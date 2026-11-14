/** Offline wire contract against the installed, pinned DSH adapter. No network. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const runtime = path.resolve(process.argv[2] || '/app/.runtime/dsh');
const require = createRequire(path.join(runtime, 'package.json'));
assert.ok(process.execArgv.includes('--experimental-import-meta-resolve'),
  'Run with node --experimental-import-meta-resolve to resolve the pinned runtime ESM exports.');
for (const key of Object.keys(process.env)) delete process.env[key];
process.env.NARB_DISABLE_NATIVE_CACHE = '1';
const captures = [];
globalThis.fetch = async (input, init) => {
  const body = JSON.parse(init?.body ?? await input.clone().text());
  const url = new URL(typeof input === 'string' ? input : input.url ?? String(input));
  const headers = new Headers(init?.headers ?? input.headers);
  assert.equal(url.origin, 'https://synthetic.invalid');
  captures.push({ path: url.pathname, thinking: body.thinking ?? null,
    reasoning_effort: body.reasoning_effort ?? null, reasoning: body.reasoning ?? null,
    output_config: body.output_config ?? null, max_tokens: body.max_tokens ?? null,
    google_key_header_matches: headers.get('x-goog-api-key') === 'synthetic-test-key',
    key_not_in_query: !url.searchParams.has('key'),
    google_tools: body.tools?.[0]?.functionDeclarations ?? null });
  // Only serialization is under test. An empty successful stream is enough to
  // collect the outgoing request; this is not a provider-answer acceptance.
  return new Response('data: [DONE]\n\n', {
    status: 200, headers: { 'content-type': 'text/event-stream' },
  });
};
const moduleUrl = pathToFileURL(require.resolve('@deepseek-ai/dsh-llm-pi-ai'));
let source = fs.readFileSync(moduleUrl, 'utf8');
source = source.replace(/from "([^"\n]+)"/g, (_, name) =>
  'from ' + JSON.stringify(import.meta.resolve(name, moduleUrl.href)));
source += '\nexport { resolveProfiles };\n';
const { PiAiAdapter, resolveProfiles } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const rows = [];
for (const api of ['openai-completions', 'openai-responses', 'anthropic-messages']) {
  const anthropic = api === 'anthropic-messages';
  const baseURL = 'https://synthetic.invalid' + (anthropic ? '/anthropic' : '/v1');
  const profiles = resolveProfiles({ deepseek: { apiKeyEnv: 'SYNTHETIC_KEY', api, baseURL,
    models: [{ id: 'deepseek-flash', input: ['text', 'image'], maxTokens: 8192 }],
    reasoning: 'max', ...(anthropic ? { compat: { forceAdaptiveThinking: true } } : {}) } });
  const adapter = new PiAiAdapter({ profiles: () => profiles,
    resolveApiKey: async () => 'synthetic-test-key' });
  const begin = captures.length;
  for await (const unused of adapter.stream({ provider: 'deepseek', model: 'deepseek-flash',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'Synthetic wire capture.' }] }],
    maxTokens: 8192 })) { void unused; }
  assert.equal(captures.length, begin + 1);
  const wire = captures[begin];
  if (api === 'openai-completions') {
    assert.equal(wire.path, '/v1/chat/completions');
    assert.equal(wire.thinking?.type, 'enabled');
    assert.equal(wire.reasoning_effort, 'max');
  } else if (api === 'openai-responses') {
    assert.equal(wire.path, '/v1/responses');
    assert.equal(wire.reasoning?.effort, 'max');
  } else {
    assert.equal(wire.path, '/anthropic/v1/messages');
    assert.equal(wire.thinking?.type, 'adaptive');
    assert.equal(wire.output_config?.effort, 'max');
  }
  rows.push({ api, ...wire });
}
// Native Google is supplied by the installed catalog. An explicit api field
// would incorrectly enter the adapter's three-protocol custom factory path.
const googleProfiles = resolveProfiles({ google: {
  apiKeyEnv: 'FORGEFLOW_CHAT_API_KEY', baseURL: 'https://synthetic.invalid/v1beta',
} });
const google = new PiAiAdapter({ profiles: () => googleProfiles,
  resolveApiKey: async (provider, profile) => {
    assert.equal(provider, 'google');
    assert.equal(profile.apiKeyEnv, 'FORGEFLOW_CHAT_API_KEY');
    return 'synthetic-test-key';
  } });
const googleStart = captures.length;
for await (const unused of google.stream({ provider: 'google', model: 'gemini-2.5-flash',
  messages: [{ role: 'user', content: [{ type: 'text', text: 'Synthetic native tool capture.' }] }],
  tools: [{ name: 'forgeflow_connection_check', description: 'Synthetic connection check.',
    parameters: { type: 'object', properties: { token: { type: 'string' } }, required: ['token'] } }],
  maxTokens: 8192 })) { void unused; }
assert.equal(captures.length, googleStart + 1);
const googleWire = captures[googleStart];
assert.equal(googleWire.path, '/v1beta/models/gemini-2.5-flash:streamGenerateContent');
assert.equal(googleWire.google_key_header_matches, true);
assert.equal(googleWire.key_not_in_query, true);
assert.equal(googleWire.google_tools.length, 1);
assert.equal(googleWire.google_tools[0].name, 'forgeflow_connection_check');
const googleParameters = googleWire.google_tools[0].parametersJsonSchema ?? googleWire.google_tools[0].parameters;
assert.deepEqual(googleParameters.required, ['token']);
assert.equal(googleParameters.properties.token.type.toLowerCase(), 'string');
rows.push({ api: 'google-generative-ai', ...googleWire });
console.log(JSON.stringify({ ok: true, offline: true, provider_requests: 0, checks: rows }));
