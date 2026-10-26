/** Case answer provenance and fallback contracts; no browser, ERP or model. */
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import ts from 'typescript';

const scratch = await mkdtemp(join(tmpdir(), 'forgeflow-case-evidence-'));
after(() => rm(scratch, {recursive: true, force: true}));
const app = new URL('../', import.meta.url);
const modules = ['caseUi', 'caseAnswerEvidence', 'api', 'decisionSummary', 'costAmounts', 'impactFacts', 'orderReadinessDates', 'verificationExplanation', 'environmentAccess'];
for (const name of modules) {
  const source = await readFile(new URL(`src/${name}.ts`, app), 'utf8');
  const {outputText} = ts.transpileModule(source, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022}});
  await writeFile(join(scratch, `${name}.mjs`), outputText.replace(/from "(\.\/[^"]+)"/g, 'from "$1.mjs"'));
}
const {readCaseSourceMode, readCaseCitations, readCaseReferenceLabels, readCaseEvidenceUsage, caseCitationValue} = await import(pathToFileURL(join(scratch, 'caseAnswerEvidence.mjs')).href);
const {localCaseAnswer} = await import(pathToFileURL(join(scratch, 'caseUi.mjs')).href);
const {askProductCase, streamProductCase} = await import(pathToFileURL(join(scratch, 'api.mjs')).href);
const original = JSON.parse(await readFile(new URL('../web/e2e/fixtures/approved-case.json', app), 'utf8'));

// Read the actual localized copy without mounting React or running a browser.
const localeSource = ts.createSourceFile('i18n.tsx', await readFile(new URL('src/i18n.tsx', app), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const messages = {};
function walk(node) {
  if (ts.isVariableDeclaration(node) && ['zh', 'en'].includes(node.name.getText()) && node.initializer) {
    const object = ts.isAsExpression(node.initializer) ? node.initializer.expression : node.initializer;
    if (ts.isObjectLiteralExpression(object)) messages[node.name.getText()] = Object.fromEntries(object.properties.filter(ts.isPropertyAssignment).map(p => [p.name.text, p.initializer.text]));
  }
  ts.forEachChild(node, walk);
}
walk(localeSource);
const translate = lang => (key, vars) => (messages[lang]?.[key] ?? messages.en?.[key] ?? key).replace(/\{(\w+)\}/g, (all, name) => vars?.[name] === undefined ? all : String(vars[name]));
const availableLanguages = Object.keys(messages);

test('legacy modes and unrelated links do not become model evidence', () => {
  for (const unknown of [undefined, null, 'case', 'harness', 'retrieval']) assert.equal(readCaseSourceMode(unknown), undefined);
  for (const explicit of ['model', 'deterministic', 'local']) assert.equal(readCaseSourceMode(explicit), explicit);
  assert.deepEqual(readCaseCitations(['Case abc', {label:'Plan xyz'}, {field:'../../private',value:1}]), []);
  assert.deepEqual(readCaseReferenceLabels(['Case abc', {field:'plan.actions.0.quantity',value:3}]), ['Case abc']);
  assert.deepEqual(readCaseCitations([]), []);
});

test('field citations preserve explicit zero, null and immutable snapshot without using current data', () => {
  const rows = readCaseCitations([{field:'plan.costs.known_delta',value:0,snapshot_id:'a'.repeat(64)}, {field:'execution',value:null}]);
  assert.equal(caseCitationValue(rows[0], 'missing'), '0');
  assert.equal(caseCitationValue(rows[1], 'missing'), 'null');
  assert.equal(caseCitationValue({field:'execution'}, 'missing'), 'missing');
  assert.equal(rows[0].snapshot_id, 'a'.repeat(64));
  assert.deepEqual(readCaseEvidenceUsage({snapshot_id:'a'.repeat(64),source:'case_snapshot',coverage:[{topic:'actions',status:'omitted',total:20,shown:12}]}).coverage[0], {topic:'actions',status:'omitted',total:20,shown:12});
});

for (const lang of availableLanguages) {
  const t = translate(lang);
  test(`${lang}: original quantity question includes recorded actions and retained documents`, () => {
    const question = lang === 'zh' ? '这次供应商取消造成多大缺口？原有采购与制造安排保留了什么？' : 'What quantity was cancelled and replaced? Which manufacturing and purchase documents were retained?';
    const reply = localCaseAnswer(original, question, t).text;
    assert.match(reply, /P00006[^\n]*3/);
    assert.match(reply, /new:purchase\.order:0006[^\n]*3/);
    for (const name of ['WH/MO/00001','WH/MO/00002','WH/MO/00003','WH/MO/00004','P00001','P00002','P00003','P00004','P00005']) assert.ok(reply.includes(name), name);
    assert.ok(reply.includes(original.plan.costs.lines[0].product_code));
    assert.ok(reply.includes('Units'));
    assert.ok(reply.includes(t('caseChat.documentScopeNote')));
  });
  test(`${lang}: changed facts change the answer and no default quantity is fabricated`, () => {
    const changed = structuredClone(original);
    changed.plan.actions[0].quantity = 17;
    changed.plan.actions[1].quantity = 19;
    changed.plan.actions[0].document = 'P-CHANGED';
    const reply = localCaseAnswer(changed, 'quantity cancelled and replaced / 数量与缺口', t).text;
    assert.match(reply, /P-CHANGED[^\n]*17/);
    assert.match(reply, /new:purchase\.order:0006[^\n]*19/);
    const missing = structuredClone(original);
    delete missing.plan.costs;
    const absent = localCaseAnswer(missing, 'retained documents / 保留单据', t).text;
    assert.ok(absent.includes(t('caseChat.preservedUnknown')));
    assert.ok(!absent.includes('WH/MO/00004'));
    const missingActions = structuredClone(original);
    delete missingActions.plan.actions;
    assert.ok(localCaseAnswer(missingActions, 'cancelled quantities / 取消数量', t).text.includes(t('caseChat.actionsUnknown')));
    const zero = structuredClone(original);
    zero.plan.actions[0].quantity = 0;
    assert.match(localCaseAnswer(zero, 'quantities / 数量', t).text, /P00006[^\n]*0 Units/);
  });
  test(`${lang}: cost and delivery questions preserve the financial and physical boundaries`, () => {
    const cost = localCaseAnswer(original, '计划成本包含什么，差额和新增采购是什么？ / planned cost and new purchase amount', t).text;
    for (const amount of ['4,634.89','4,794.94','160.05','1,432.32']) assert.ok(cost.includes(amount), amount);
    assert.ok(cost.includes(t('cost.deltaNote')));
    const delivery = localCaseAnswer(original, '实际改了哪些单据，51/51说明交付了吗？ / Which records changed and does verification prove delivery?', t).text;
    assert.ok(delivery.includes(t('caseChat.deliveryBoundary')));
    assert.ok(delivery.includes(t('caseChat.writeCountBoundary')));
    assert.ok(delivery.includes('P00006'));
  });
  test(`${lang}: large retained list is explicitly incomplete`, () => {
    const many = structuredClone(original);
    many.plan.costs.lines = Array.from({length:20}, (_,i) => ({...many.plan.costs.lines[0],document:`MO-${i}`}));
    const reply = localCaseAnswer(many, '保留哪些制造安排 / retained manufacturing documents', t).text;
    assert.ok(reply.includes('MO-11'));
    assert.ok(!reply.includes('MO-19'));
    assert.ok(reply.includes(t('caseChat.documentMore',{shown:12,total:20})));
  });
}

test('case transport carries explicit locale in both JSON and streaming requests', async () => {
  const before = globalThis.fetch;
  const sent = [];
  globalThis.fetch = async (_url, init) => {
    sent.push(JSON.parse(init.body));
    return sent.length === 1 ? Response.json({answer:'ok',source_mode:'deterministic'}) : new Response('data: {"type":"final","answer":"ok","source_mode":"model"}\n\n', {headers:{'Content-Type':'text/event-stream'}});
  };
  try {
    await askProductCase('case-a', 'P00006?', 'conversation-a', undefined, 'zh');
    await streamProductCase('case-a', 'P00006?', 'conversation-a', () => {}, undefined, {id:'turn-a',resume:false,lastEventId:'',onCursor:()=>{}}, 'en');
    assert.equal(sent[0].lang, 'zh');
    assert.equal(sent[1].lang, 'en');
    assert.equal(sent[1].turn_id, 'turn-a');
  } finally { globalThis.fetch = before; }
});
