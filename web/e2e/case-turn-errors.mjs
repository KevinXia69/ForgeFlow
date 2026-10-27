/** Browser checks for turn admission errors; the API is simulated, with no model or ERP. */
import assert from 'node:assert/strict';
import {readFile, realpath} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
import {chromium} from 'playwright-core';

const root=fileURLToPath(new URL('../',import.meta.url));
const isPublic=root.includes('/web-public/');
const fixture=JSON.parse(await readFile(new URL('../../web/e2e/fixtures/approved-case.json',import.meta.url),'utf8'));
const server=await createServer({root,server:{host:'127.0.0.1',port:0,fs:{allow:[root,await realpath(root+'node_modules')]}},logLevel:'error'});
await server.listen();
const base=`http://127.0.0.1:${server.httpServer.address().port}/app/`;
const browser=await chromium.launch({channel:'chrome',headless:true});
const variants=[
 {detail:'turn_id_upgrade_required',status:410,resume:false,zh:'当前页面需要更新',en:'This page needs an update'},
 {detail:'turn_expired',status:410,resume:true,zh:'此回答已过期',en:'This response has expired'},
 {detail:'turn_cancelled',status:409,resume:true,zh:'此回答已取消',en:'This response was cancelled'},
 {detail:'turn_unavailable',status:410,resume:true,zh:'上次回答已无法续接',en:'The previous response can no longer be resumed'},
 {detail:'turn_payload_mismatch',status:409,resume:true,zh:'上次回答已无法续接',en:'The previous response can no longer be resumed'},
];
try {
 for(const lang of isPublic?['en']:['zh','en'])for(const variant of variants) {
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const owner='turn-error-owner',key=`forgeflow.chat.v2.${owner}:case:${fixture.case_id}`;
  const question='Keep this exact question after an admission error\nKeep the second line as well';
  const turnId='legacy-turn-for-resume',conversationId='turn-error-conversation';
  await context.addInitScript(({lang,key,question,resume,turnId,conversationId})=>{
   localStorage.setItem('forgeflow.lang',lang);
   if(resume)localStorage.setItem(key,JSON.stringify({active:conversationId,sessions:[{
    id:conversationId,title:question,messages:[{id:'original-user',role:'user',text:question}],pending:true,
    pendingTurn:{id:turnId,question,lastEventId:'1',lang},updated:Date.now(),
   }]}));
  },{lang,key,question,resume:variant.resume,turnId,conversationId});
  const page=await context.newPage(),requests=[];
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
    requests.push(route.request().postDataJSON());
    return route.fulfill({status:variant.status,json:{detail:variant.detail}});
   }
   if(route.request().method()!=='GET')throw new Error(`Unexpected fallback/write: ${path}`);
   return route.fulfill({json:[]});
  });
  try {
   await page.goto(`${base}#case=${fixture.case_id}`);
   await page.locator('[data-ask-case]').click();
   if(!variant.resume) {
    await page.locator('[data-composer-input]').fill(question);
    await page.locator('button.send').click();
   }
   await page.locator('.msg.assistant').filter({hasText:variant[lang]}).first().waitFor();
   await page.waitForFunction(storageKey=>JSON.parse(localStorage.getItem(storageKey)||'{}').sessions?.every(s=>!s.pendingTurn&&!s.pending),key);
   assert.equal(await page.locator('.msg.user').count(),1);
   assert.equal(await page.locator('.msg.user').textContent(),question);
   assert.equal(await page.locator('.msg.assistant').count(),1);
   assert.ok(requests.length>0);
   assert.ok(requests.every(r=>r.question===question&&r.resume===variant.resume));
   if(variant.resume) {
    assert.ok(requests.every(r=>r.turn_id===turnId&&r.lang===lang));
    assert.equal((await page.locator('[data-composer-input]').innerText()).trim(),'');
   } else {
    assert.equal(requests.length,1,'admission failure automatically retried');
    assert.equal(await page.locator('[data-composer-input]').innerText(),question,'rejected new question was not restored');
   }
   let userMessages=1;
   if(!variant.resume) {
    const composer=page.locator('[data-composer-input]');
    await composer.evaluate(element=>{
     element.focus();
     const walker=document.createTreeWalker(element,NodeFilter.SHOW_TEXT);let last,node;
     while((node=walker.nextNode()))last=node;
     const range=document.createRange();range.selectNodeContents(last);range.collapse(false);
     const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
    });
    await composer.pressSequentially(' Additional details');
    let edited=question+' Additional details';
    assert.equal(await composer.innerText(),edited,'editing restored multiline text lost content');
    await composer.press('Shift+Enter');
    await composer.pressSequentially('Next paragraph');
    edited+='\nNext paragraph';
    assert.equal(await composer.innerText(),edited,'adding a paragraph lost or rearranged restored text');
    assert.equal(requests.length,1,'editing implicitly sent a request');
    await page.locator('button.send').click();
    await page.waitForFunction(()=>document.querySelectorAll('.msg.assistant').length===2);
    await page.waitForFunction(storageKey=>JSON.parse(localStorage.getItem(storageKey)).sessions.every(s=>!s.pendingTurn&&!s.pending),key);
    assert.equal(requests.length,2,'manual send should create exactly one additional request');
    assert.equal(requests[1].question,edited);
    assert.equal(requests[1].resume,false);
    assert.notEqual(requests[1].turn_id,requests[0].turn_id);
    assert.equal(await composer.innerText(),edited);
    userMessages=2;
   }
   const saved=await page.evaluate(storageKey=>JSON.parse(localStorage.getItem(storageKey)),key);
   const active=saved.sessions.find(s=>s.id===saved.active);
   assert.equal(active.messages[0].text,question);
   assert.equal(active.pendingTurn,null);
   const count=requests.length;
   // Navigating back into the conversation must not restart the rejected turn.
   await page.locator('.case-chat-back').click();
   await page.locator('[data-ask-case]').click();
   await page.locator('.msg.assistant').filter({hasText:variant[lang]}).first().waitFor();
   assert.equal(requests.length,count,'a rejected turn was retried on remount');
   assert.equal(await page.locator('.msg.user').count(),userMessages);
   console.log(`PASS ${isPublic?'public':'local'} ${lang} ${variant.detail}: question retained, no automatic new turn`);
  } finally {await context.close();}
 }
} finally {await browser.close();await server.close();}
