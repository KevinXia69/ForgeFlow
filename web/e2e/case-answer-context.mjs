/** Real browser history checks over invented HTTP, never ERP/model acceptance. */
import { chromium } from 'playwright-core';
import { mkdir } from 'node:fs/promises';
import { startRevisionFixture, initialCase, CASE_ID } from './revision-fixture.mjs';
import { caseAnswerContext } from '../src/caseAnswerContext.ts';
const instance = await startRevisionFixture({directory: process.env.FORGEFLOW_STATIC_APP});
const {fixture, url} = instance;
const output = process.env.FORGEFLOW_SCREENSHOT_DIR || '/tmp/forgeflow-history-context';
await mkdir(output, {recursive: true});
const browser = await chromium.launch({channel:'chrome', headless:true});
const assert = (value, message) => { if (!value) throw new Error(message); };
let active;
try {
 for (const lang of process.env.FORGEFLOW_PUBLIC_UI ? ['en'] : ['zh','en']) {
  fixture.current = initialCase(); fixture.calls = [];
  const context = await browser.newContext({viewport:{width:1280,height:900}});
  const page = await context.newPage(); active = page; page.setDefaultTimeout(10000);
  const errors = []; page.on('pageerror', error=>errors.push(String(error)));
  await page.addInitScript(lang=>localStorage.setItem('forgeflow.lang',lang),lang);
  let askCalls = 0, mode = 'server';
  await page.route('**/ask/stream', route=>route.fulfill({status:503,json:{detail:'stream_unavailable'}}));
  await page.route('**/ask', route=>{
   askCalls++;
   if(mode==='fallback') return route.fulfill({status:503,json:{detail:'test_unavailable'}});
   return route.fulfill({json:{found:true,answer:'Nothing has been executed in this recorded answer.',mode:'case',citations:[],case_context:caseAnswerContext(fixture.current)}});
  });
  await page.goto(`${url}/app/#case=${CASE_ID}&chat=1`);
  await page.locator('[data-case-chat]').waitFor();
  const ask = async text=>{
   const prior = await page.locator('.msg.assistant').count();
   await page.locator('[contenteditable="true"]').fill(text); await page.locator('.send').click();
   await page.waitForFunction(prior=>document.querySelectorAll('.msg.assistant').length>prior,prior);
  };
  await ask('What is the current plan?');
  assert(await page.locator('[data-case-answer-context="recorded"]').count()===1,'new answer context not recorded');
  assert(await page.locator('[data-case-history-context]').count()===0,'unchanged case marked as historical');
  const original = await page.locator('.assistant-markdown').first().innerText();
  fixture.current = {...fixture.current, state:'succeeded', revision:{...fixture.current.revision,version:2},
   plan:{...fixture.current.plan,plan_id:'b'.repeat(64)}, updated_at:'2026-10-23T12:03:00Z',
   execution:{status:'succeeded',writes:[],readback:{execution_validated:true},verifier:{status:'passed',source:'synthetic_executor'}},
   decision:{...fixture.current.decision,can_approve:false,can_reject:false,can_request_changes:false},
   result:{...fixture.current.result,simulation_passed:true,outcome:'simulation_passed',execution_status:'succeeded'}};
  await page.reload(); await page.locator('[data-case-answer-context="earlier"]').waitFor();
  assert(askCalls===1,'history reload reissued the old question');
  assert(await page.locator('[data-case-history-context]').count()===1,'old-context warning not outside scroll area');
  assert((await page.locator('[data-case-answer-context="earlier"]').innerText()).includes('2'),'latest version not explained');
  assert(await page.locator('.assistant-markdown').first().innerText()===original,'old answer rewritten');
  await page.screenshot({path:`${output}/${lang}-earlier.png`,fullPage:true,animations:'disabled'});
  await page.evaluate(()=>{
   for(const key of Object.keys(localStorage).filter(key=>key.startsWith('forgeflow.chat.v2.'))) {
    const store=JSON.parse(localStorage.getItem(key));
    for(const session of store.sessions) for(const message of session.messages) if(message.meta) delete message.meta.caseContext;
    localStorage.setItem(key,JSON.stringify(store));
   }
  });
  await page.reload(); await page.locator('[data-case-answer-context="unknown"]').waitFor();
  assert(await page.locator('.assistant-markdown').first().innerText()===original,'legacy message rewritten');
  const retrofitted = await page.evaluate(()=>Object.keys(localStorage).filter(key=>key.startsWith('forgeflow.chat.v2.')).some(key=>JSON.parse(localStorage.getItem(key)).sessions.some(session=>session.messages.some(message=>message.meta?.caseContext))));
  assert(!retrofitted,'legacy history was assigned current metadata');
  mode='fallback'; await ask('What happened during execution?');
  assert(await page.locator('[data-case-answer-context="recorded"]').count()===1,'fallback lacks last-retrieved context');
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth)<=392,'history warning overflows narrow screen');
  await page.screenshot({path:`${output}/${lang}-legacy-mobile.png`,fullPage:true,animations:'disabled'});
  assert(errors.length===0,errors.join('\n'));
  assert(!fixture.calls.some(call=>/(approve|select|revision|reprepare)$/.test(call.path)),'history read mutated a case');
  await context.close();
  console.log(`history_context=${lang}: server snapshot, earlier version, legacy unknown, unchanged text, reload, fallback and 390px passed; invented HTTP only`);
 }
} catch(error) { if(active && !active.isClosed()) await active.screenshot({path:`${output}/failure.png`,fullPage:true}); throw error; }
finally { await browser.close(); await instance.close(); }
