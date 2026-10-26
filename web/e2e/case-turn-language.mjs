/** Real browser navigation with a payload-checking fake API; no model or ERP. */
import assert from 'node:assert/strict';
import { readFile, realpath } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';
import { chromium } from 'playwright-core';

const root=fileURLToPath(new URL('../',import.meta.url));
const isPublic=root.includes('/web-public/');
const fixture=JSON.parse(await readFile(new URL('../../web/e2e/fixtures/approved-case.json',import.meta.url),'utf8'));
const server=await createServer({root,server:{host:'127.0.0.1',port:0,fs:{allow:[root,await realpath(root+'node_modules')]}},logLevel:'error'});
await server.listen();
const base=`http://127.0.0.1:${server.httpServer.address().port}/app/`;
const browser=await chromium.launch({channel:'chrome',headless:true});
const cases=isPublic
  ? [{initial:'en',next:'en'}, {initial:'en',next:'en',legacy:'zh'}, {initial:'en',next:'en',legacy:'absent'}]
  : [{initial:'zh',next:'en'}, {initial:'en',next:'zh'}, {initial:'zh',next:'en',legacy:'zh'}, {initial:'zh',next:'en',legacy:'absent'}];
try {
 for(const variant of cases) {
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  await context.addInitScript(lang=>{if(!localStorage.getItem('forgeflow.lang'))localStorage.setItem('forgeflow.lang',lang);},variant.initial);
  const page=await context.newPage();
  const owner='language-owner',key=`forgeflow.chat.v2.${owner}:case:${fixture.case_id}`;
  const requests=[],turns=new Map();let starts=0,release,observeStart;
  const firstStarted=new Promise(resolve=>{observeStart=resolve;});
  const hold=new Promise(resolve=>{release=resolve;});
  await page.route('**/api/**',async route=>{
   const path=new URL(route.request().url()).pathname;
   if(path==='/api/auth/me')return route.fulfill({json:{scope_id:owner,display_name:'Synthetic reviewer',deployment:'local'}});
   if(path==='/api/config')return route.fulfill({json:{deployment:'local',runner_enabled:false}});
   if(path==='/api/settings/model')return route.fulfill({json:{configured:false}});
   if(path==='/api/capabilities')return route.fulfill({json:{product_scenarios:[],case_read:true,case_create:false,case_prepare:false,case_approve:false,environment:{execution:'synthetic_executor',ready:false}}});
   if(path==='/api/product/cases')return route.fulfill({json:[fixture]});
   if(path===`/api/product/cases/${fixture.case_id}`)return route.fulfill({json:fixture});
   if(path.endsWith('/events'))return route.fulfill({contentType:'text/event-stream',body:'event: end\ndata: {}\n\n'});
   if(path.endsWith('/ask/stream')) {
    const body=route.request().postDataJSON();requests.push(body);
    if(!body.resume) {
     assert.ok(!turns.has(body.turn_id));starts++;
     turns.set(body.turn_id,{question:body.question,lang:variant.legacy==='absent'?undefined:variant.legacy??body.lang});
     observeStart();
     await hold;
     return route.fulfill({contentType:'text/event-stream',body:''}).catch(()=>{});
    }
    const original=turns.get(body.turn_id);
    if(!original)return route.fulfill({status:410,json:{detail:'turn_unavailable'}});
    if(original.question!==body.question||original.lang!==body.lang)return route.fulfill({status:409,json:{detail:'turn_payload_mismatch'}});
    return route.fulfill({contentType:'text/event-stream',body:'id: 2\ndata: '+JSON.stringify({type:'final',found:true,answer:'Restored original response',source_mode:'model',citations:[]})+'\n\n'});
   }
   if(route.request().method()!=='GET')throw new Error(`Unexpected request: ${path}`);
   return route.fulfill({json:[]});
  });
  try {
   await page.goto(`${base}#case=${fixture.case_id}`);
   await page.locator('[data-ask-case]').click();
   await page.locator('[data-composer-input]').fill('Recover this exact case turn');
   await page.locator('button.send').click();
   await page.waitForFunction(storageKey=>!!JSON.parse(localStorage.getItem(storageKey)||'{}').sessions?.find(s=>s.pendingTurn)?.pendingTurn,key);
   const pending=await page.evaluate(storageKey=>JSON.parse(localStorage.getItem(storageKey)).sessions.find(s=>s.pendingTurn).pendingTurn,key);
   assert.equal(pending.lang,variant.initial);
   await firstStarted;
   await page.waitForFunction(()=>document.querySelector('[data-live-reasoning]'));
   // Leave while the original stream is pending, change language, and return.
   await page.locator('.case-chat-back').click();
   if(variant.legacy)await page.evaluate(storageKey=>{const store=JSON.parse(localStorage.getItem(storageKey));delete store.sessions.find(s=>s.pendingTurn).pendingTurn.lang;localStorage.setItem(storageKey,JSON.stringify(store));},key);
   if(!isPublic) {
    await page.getByRole('button',{name:variant.initial==='zh'?'设置':'Settings',exact:true}).click();
    await page.locator('#settings-language').click();
    await page.getByRole('option',{name:variant.next==='zh'?'简体中文':'English',exact:true}).click();
    await page.getByRole('button',{name:variant.next==='zh'?'关闭设置':'Close settings',exact:true}).click();
   }
   await page.locator('[data-ask-case]').click();
   await page.locator('.msg.assistant').filter({hasText:'Restored original response'}).waitFor();
   assert.equal(starts,1,'restoring a turn created another model producer');
   assert.equal(requests.filter(r=>!r.resume).length,1);
   assert.ok(requests.slice(1).every(r=>r.resume&&r.turn_id===pending.id&&r.question===pending.question));
   // React development StrictMode can attach twice; both must reuse the saved language.
   if(!variant.legacy)assert.ok(requests.slice(1).every(r=>r.lang===pending.lang),'saved-language resume probed a different language');
   assert.equal(await page.locator('.msg.user').count(),1);
   await page.waitForFunction(storageKey=>JSON.parse(localStorage.getItem(storageKey)).sessions.every(s=>!s.pendingTurn),key);
   console.log(`PASS ${isPublic?'public':'local'} ${variant.initial}->${variant.next} ${variant.legacy??'saved language'}: ${requests.length} HTTP requests, ${starts} producer`);
  } finally {release();await context.close();}
 }
} finally {await browser.close();await server.close();}
