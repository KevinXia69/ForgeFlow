/** Real responder and answer component logic rendered with React SSR.
 * The ChatSurface, markdown renderer and activity view are inert seams: this
 * validates source/field/link markup, not browser interaction or model behavior.
 */
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const app = new URL('../', import.meta.url), require = createRequire(import.meta.url);
const scratch = await mkdtemp(join(tmpdir(), 'forgeflow-render-evidence-'));
after(() => rm(scratch, {recursive:true, force:true}));
const reactUrl = pathToFileURL(require.resolve('react')).href;
const transpile = text => ts.transpileModule(text, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022,jsx:ts.JsxEmit.React}}).outputText;
for (const name of ['api','environmentAccess','caseUi','caseAnswerEvidence','caseAnswerContext','decisionSummary','costAmounts','impactFacts','orderReadinessDates','verificationExplanation']) {
  const source = await readFile(new URL(`src/${name}.ts`, app), 'utf8');
  await writeFile(join(scratch, `${name}.mjs`), transpile(source).replace(/from "(\.\/[^"]+)"/g, 'from "$1.mjs"'));
}
const dictionaries = {};
const i18n = ts.createSourceFile('i18n.tsx',await readFile(new URL('src/i18n.tsx',app),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
function walk(node) {
  if (ts.isVariableDeclaration(node) && ['zh','en'].includes(node.name.getText()) && node.initializer) {
    const object = ts.isAsExpression(node.initializer) ? node.initializer.expression : node.initializer;
    if (ts.isObjectLiteralExpression(object)) dictionaries[node.name.getText()] = Object.fromEntries(object.properties.filter(ts.isPropertyAssignment).map(p=>[p.name.text,p.initializer.text]));
  }
  ts.forEachChild(node,walk);
}
walk(i18n);
const translate = (lang,key,vars) => (dictionaries[lang]?.[key] ?? dictionaries.en[key] ?? key).replace(/\{(\w+)\}/g,(all,k)=>vars?.[k] === undefined ? all : String(vars[k]));
const state = {lang:'en', props:null, calls:[], response:{}, failure:null};
class ApiError extends Error {constructor(message,status){super(message);this.status=status;}}
const bridge = {
  useI18n:()=>({lang:state.lang,t:(key,vars)=>translate(state.lang,key,vars)}),
  useWorkspace:()=>({scope_id:'owner-a'}),
  ChatSurface:props=>{state.props=props;return null;},
  MarkdownText:({text})=>React.createElement('article',null,text),
  ActivityPanel:({state})=>React.createElement('aside',{'data-test-activity':true},JSON.stringify(state)),
  ApiError,
  askProductCase:async(...args)=>{state.calls.push(['ask',args]);if(state.failure)throw state.failure;return structuredClone(state.response);},
  streamProductCase:async(...args)=>{state.calls.push(['stream',args]);if(state.failure)throw state.failure;return structuredClone(state.response);},
  cancelCaseTurn:async()=>{},
};
globalThis.__forgeflowCaseRenderBridge = bridge;
const imports = `import React, {useMemo} from ${JSON.stringify(reactUrl)};\nimport {localCaseAnswer,caseOutcomeLabel,stateLabel,formatDateTime} from './caseUi.mjs';\nimport {answerContextKind,caseAnswerContext,readAnswerContext} from './caseAnswerContext.mjs';\nimport {caseCitationValue,readCaseCitations,readCaseEvidenceUsage,readCaseReferenceLabels,readCaseSourceMode} from './caseAnswerEvidence.mjs';\nconst {useI18n,useWorkspace,ChatSurface,MarkdownText,ActivityPanel,ApiError,askProductCase,streamProductCase,cancelCaseTurn}=globalThis.__forgeflowCaseRenderBridge;\n`;
const printer = ts.createPrinter();
async function actualFunctions(file,names) {
  const ast = ts.createSourceFile(file,await readFile(new URL(`src/${file}`,app),'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  return ast.statements.filter(n=>ts.isFunctionDeclaration(n)&&names.includes(n.name?.text)).map(n=>printer.printNode(ts.EmitHint.Unspecified,n,ast)).join('\n');
}
await writeFile(join(scratch,'components.mjs'),imports+transpile(await actualFunctions('CaseChatView.tsx',['CaseChatView'])+'\n'+await actualFunctions('Assistant.tsx',['AssistantMessage','answerCitations'])+'\nexport {AssistantMessage};'));
const {CaseChatView,AssistantMessage} = await import(pathToFileURL(join(scratch,'components.mjs')).href);
const {caseAnswerContext} = await import(pathToFileURL(join(scratch,'caseAnswerContext.mjs')).href);
const original = JSON.parse(await readFile(new URL('../web/e2e/fixtures/approved-case.json',app),'utf8'));
function mount(lang,mode='live',record=original) {
  Object.assign(state,{lang,props:null,calls:[],response:{},failure:null});
  renderToStaticMarkup(React.createElement(CaseChatView,{caseFile:record,mode,onBack:()=>{}}));
  return state.props;
}
function render(reply,props=state.props) {
  return renderToStaticMarkup(React.createElement(AssistantMessage,{message:{id:'test',role:'assistant',...reply},displayText:props.displayAnswerText,contextNotice:props.answerContextNotice(reply.meta)}));
}
const snapshot='a'.repeat(64);
const model = {answer:'Recorded answer',source_mode:'model',citations:[],case_context:caseAnswerContext(original),evidence_refs:[{kind:'case',id:original.case_id,label:'Related case',href:'javascript:alert(1)'}],evidence_usage:{source:'case_snapshot',snapshot_id:snapshot,coverage:[]}};

for(const lang of Object.keys(dictionaries)) {
  test(`${lang}: real responder never fills empty model citations and preserves request locale`,async()=>{
    const props=mount(lang);state.response=model;
    const reply=await props.responder('P00006',[]);
    assert.equal(state.calls[0][1].at(-1),lang);
    assert.equal(reply.meta.sourceMode,'model');assert.deepEqual(reply.meta.caseCitations,[]);
    assert.deepEqual(reply.meta.relatedCaseLabels,[]);assert.equal(reply.meta.activity,undefined);
    const html=render(reply);
    assert.match(html,/data-case-answer-source="model"/);assert.doesNotMatch(html,/data-case-fact-citations/);
    assert.match(html,/data-related-evidence/);assert.doesNotMatch(html,/javascript:|alert\(1\)/);
    assert.ok(html.includes(`#case=${original.case_id}&amp;evidence=1`));
    assert.ok(html.includes(translate(lang,'caseChat.relatedEvidenceNote')));
    state.calls=[];
    await props.responder('P00006',[],()=>{},undefined,'conversation',{id:'turn',resume:false});
    assert.equal(state.calls[0][0],'stream');assert.equal(state.calls[0][1].at(-1),lang);
  });
  test(`${lang}: field/snapshot provenance survives new case revisions and old replies remain unknown`,async()=>{
    const props=mount(lang);state.response={...model,citations:[{field:'plan.actions.0.quantity',label:'Quantity',value:3}],evidence_usage:{...model.evidence_usage,coverage:[{topic:'actions',status:'available'},{topic:'preserved',status:'omitted',shown:12,total:20},{topic:'delivery',status:'not_recorded'},{topic:'execution',status:'unavailable'}]}};
    const reply=await props.responder('Quantity?',[]),saved=structuredClone(reply);
    const revised=structuredClone(original);revised.revision.version=2;revised.plan.plan_id='b'.repeat(64);revised.plan.actions[0].quantity=99;
    const latest=mount(lang,'live',revised),html=render(reply,latest);
    assert.deepEqual(reply,saved);assert.match(html,/data-case-answer-context="earlier"/);
    assert.ok(html.includes(snapshot));assert.ok(html.includes('plan.actions.0.quantity'));assert.ok(html.includes('Quantity: 3'));
    assert.ok(!html.includes('Quantity: 99'));assert.ok(html.includes(translate(lang,'caseChat.snapshotFacts')));
    for(const key of ['coverageAvailable','coverageOmitted','coverageMissing','coverageUnavailable'])assert.ok(html.includes(translate(lang,`caseChat.${key}`)));
    const old={text:'Historical answer unchanged',meta:{mode:'case',citations:['Legacy case label'],passages:[]}};
    const legacy=render(old,latest);
    assert.match(legacy,/data-case-answer-source="unknown"/);assert.match(legacy,/data-case-answer-context="unknown"/);
    assert.doesNotMatch(legacy,/data-case-fact-citations/);assert.match(legacy,/data-related-evidence/);
    assert.equal(old.text,'Historical answer unchanged');assert.equal(old.meta.caseContext,undefined);
  });
  test(`${lang}: local fallback, server downgrade and actual tool activity are distinguishable`,async()=>{
    const props=mount(lang);state.failure=new TypeError('Failed to fetch');
    const local=await props.responder('数量与保留安排 / retained quantities',[]);
    assert.equal(local.meta.sourceMode,'local');assert.deepEqual(local.meta.citations,[]);assert.ok(local.text.includes('P00006'));
    assert.match(render(local),/data-case-answer-source="local"/);assert.doesNotMatch(render(local),/data-case-fact-citations/);
    state.failure=null;state.response={...model,source_mode:'deterministic',fallback_reason:'model_evidence_mismatch'};
    const downgraded=await props.responder('P00006',[]);assert.equal(downgraded.meta.sourceMode,'deterministic');
    assert.ok(render(downgraded).includes(translate(lang,'caseChat.fallbackMismatch')));
    state.response={...model,activity:[{id:'hidden',kind:'think',label:'private internal reasoning',status:'done'},{id:'real',kind:'get_case',label:'Read case section',status:'done'}]};
    const withTool=await props.responder('P00006',[]);
    assert.equal(withTool.meta.activity.steps.length,1);assert.equal(withTool.meta.activity.steps[0].kind,'get_case');
    assert.equal(withTool.meta.activity.hideDuration,true);assert.doesNotMatch(render(withTool),/private internal reasoning/);
  });
  test(`${lang}: incomplete execution receipts do not crash or become zero writes`,async()=>{
    const partial=structuredClone(original);delete partial.execution.writes;
    const props=mount(lang,'live',partial);state.response={...model,case_context:caseAnswerContext(partial)};
    const server=await props.responder('实际改了哪些单据、验收怎样？ / Which records changed during execution?',[]);
    assert.equal(server.meta.sourceMode,'model');assert.equal(state.calls.length,1);
    state.failure=new TypeError('Failed to fetch');
    const local=await props.responder('实际改了哪些单据、验收怎样？ / Which records changed during execution?',[]);
    assert.equal(local.meta.sourceMode,'local');assert.ok(local.text.includes(translate(lang,'runs.notRecorded')));
    assert.doesNotMatch(local.text,/0 writes|0 条单据|0 项/);
  });
  test(`${lang}: selection conflict hides stale plan details even with historical execution`,async()=>{
    const conflict=structuredClone(original);conflict.reason='product_selection_conflict';
    const props=mount(lang,'fixture',conflict);
    const reply=await props.responder('取消采购缺口多少，原有安排保留了什么？ / cancelled and retained quantities',[]);
    assert.equal(reply.meta.sourceMode,'local');assert.ok(reply.text.includes(translate(lang,'candidate.conflictBody')));
    assert.ok(!reply.text.includes('P00006'));assert.equal(state.calls.length,0);
  });
  test(`${lang}: server rejections never become a new local answer`,async()=>{
    const props=mount(lang);
    for(const status of [400,401,403,404,409,410,422,500,503]) {
      state.calls=[];state.failure=new ApiError('server rejection',status);
      await assert.rejects(props.responder('P00006',[]),e=>e===state.failure);
      assert.equal(state.calls.length,1);
    }
    for(const status of [405,501]) {
      state.failure=new ApiError('unsupported',status);
      assert.equal((await props.responder('P00006',[])).meta.sourceMode,'local');
    }
  });
  test(`${lang}: abort and resume failure do not fabricate local success or a second request`,async()=>{
    const props=mount(lang),controller=new AbortController();controller.abort();state.failure=new ApiError('stopped',503);
    await assert.rejects(props.responder('P00006',[],()=>{},controller.signal,'c',{id:'t',resume:false}));
    assert.equal(state.calls.length,1);
    state.calls=[];state.failure=new ApiError('stream_unavailable',503);
    await assert.rejects(props.responder('P00006',[],()=>{},undefined,'c',{id:'t',resume:true}));
    assert.equal(state.calls.length,1);
  });
}
