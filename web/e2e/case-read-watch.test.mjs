/** Read scheduler contract, without a browser, ERP or model. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const modulePath = process.env.FORGEFLOW_READ_WATCH_MODULE;
const { startCaseReadWatch } = await import(modulePath ? pathToFileURL(modulePath).href : '../src/caseReadWatch.ts');
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
function setup(t, options = {}) {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  let version = 0;
  const calls = [], values = [], errors = [], expired = [];
  const stop = startCaseReadWatch({
    read: signal => new Promise((resolve, reject) => calls.push({ signal, resolve, reject })),
    version: () => version, accept: value => values.push(value), onError: error => errors.push(error),
    onExpired: () => expired.push(true), intervalMs: 20, requestTimeoutMs: 100, lifetimeMs: 500,
    ...options,
  });
  t.after(stop);
  return { calls, values, errors, expired, stop, update: () => version++, tick: async ms => { t.mock.timers.tick(ms); await settle(); } };
}
test('serial reads; navigation aborts and discards a late result', async t => {
  const f = setup(t);
  await f.tick(0); assert.equal(f.calls.length, 1);
  await f.tick(50); assert.equal(f.calls.length, 1);
  f.calls[0].resolve('executing'); await settle();
  assert.deepEqual(f.values, ['executing']);
  await f.tick(20); assert.equal(f.calls.length, 2);
  f.stop(); assert.equal(f.calls[1].signal.aborted, true);
  f.calls[1].resolve('old-case'); await settle(); await f.tick(1000);
  assert.deepEqual(f.values, ['executing']); assert.deepEqual(f.expired, []);
});
test('newer mutation or refresh invalidates old data and old errors', async t => {
  const f = setup(t);
  await f.tick(0); f.update(); f.calls[0].resolve('stale-awaiting-approval'); await settle();
  assert.deepEqual(f.values, []);
  await f.tick(20); f.update(); f.calls[1].reject(new Error('obsolete')); await settle();
  assert.deepEqual(f.errors, []);
  await f.tick(20); f.calls[2].reject(new Error('current-read-failed')); await settle();
  assert.equal(f.errors[0].message, 'current-read-failed');
  await f.tick(20); f.calls[3].resolve('confirmed-receipt'); await settle();
  assert.deepEqual(f.values, ['confirmed-receipt']);
});
test('request timeout aborts; a transport honoring abort can recover by reading only', async t => {
  const f = setup(t);
  await f.tick(0); await f.tick(100);
  assert.equal(f.calls[0].signal.aborted, true);
  f.calls[0].reject(new DOMException('Timed out', 'AbortError')); await settle();
  assert.equal(f.errors.length, 1);
  await f.tick(20); assert.equal(f.calls.length, 2);
  f.calls[1].resolve('server-current'); await settle();
  assert.deepEqual(f.values, ['server-current']);
});
test('total deadline aborts a hung read; late receipts cannot restart polling', async t => {
  const f = setup(t);
  await f.tick(0); await f.tick(500);
  assert.equal(f.calls[0].signal.aborted, true); assert.deepEqual(f.expired, [true]);
  f.calls[0].resolve('too-late'); await settle(); await f.tick(1000);
  assert.deepEqual(f.values, []); assert.equal(f.calls.length, 1); assert.deepEqual(f.expired, [true]);
});
