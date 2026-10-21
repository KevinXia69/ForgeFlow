/** History provenance contract; no browser, ERP or provider. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const path = process.env.FORGEFLOW_ANSWER_CONTEXT_MODULE;
const { caseAnswerContext, readAnswerContext, answerContextKind } = await import(path ? pathToFileURL(path).href : '../src/caseAnswerContext.ts');
const record = { case_id: 'case-a', state: 'awaiting_approval', revision: { version: 1 }, plan: { plan_id: 'a'.repeat(64) },
  comparison: { status: 'ready', comparison_id: 'c'.repeat(32), selection_revision: 3 }, decision: { selection_pending: false },
  updated_at: '2026-10-23T12:00:00Z', result: { outcome: 'awaiting_approval' } };
test('older stored messages are unknown and never upgraded by reading the current case', () => {
  const current = caseAnswerContext(record);
  for (const missing of [undefined, null, {}, { schema_version: 1 }, { ...current, case_id: 'another-owner-case' }, { ...current, version: NaN }]) {
    assert.equal(answerContextKind(missing, current), 'unknown');
  }
  assert.equal(readAnswerContext({ ...current, selection_pending: 'false' }), null);
  assert.equal(answerContextKind(current, current), 'recorded');
});
test('version, choice, pending intent and result changes make the old context earlier', () => {
  const current = caseAnswerContext(record), before = structuredClone(current);
  for (const [key, value] of Object.entries({version: 2, plan_id: 'b'.repeat(64), comparison_id: 'd'.repeat(32),
    selection_revision: 4, selection_pending: true, state: 'succeeded', outcome: 'verified', updated_at: '2026-10-23T12:01:00Z'})) {
    assert.equal(answerContextKind(before, {...current, [key]: value}), 'earlier', key);
  }
  assert.deepEqual(before, current);
});
test('unavailable comparison and absent legacy fields remain explicit nulls', () => {
  const current = caseAnswerContext({...record, plan: null, comparison: { status: 'unavailable' }, revision: undefined, updated_at: undefined});
  assert.equal(current.plan_id, null); assert.equal(current.comparison_id, null); assert.equal(current.selection_revision, null);
  assert.equal(current.version, null); assert.equal(current.updated_at, null);
  assert.equal(answerContextKind(current, current), 'recorded');
});
