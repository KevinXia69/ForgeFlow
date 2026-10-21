/** English-only public behavior regression against isolated mock APIs; no ERP/model. */
import { spawn } from 'node:child_process';
import { access, mkdtemp, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
const temporary = process.env.FORGEFLOW_STATIC_DIR ? null : await mkdtemp(join(tmpdir(), 'forgeflow-public-smoke-'));
const root = process.env.FORGEFLOW_STATIC_DIR || temporary;
if (temporary) await symlink(fileURLToPath(new URL('../dist', import.meta.url)), join(temporary, 'app'), 'dir');
await access(join(root, 'app', 'index.html'));
const server = spawn('python3', ['-m','http.server','4178','--bind','127.0.0.1','--directory',root], { stdio:'ignore' });
await new Promise(resolve => setTimeout(resolve, 400));
let browser;
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const visibleEnglish = async (page, label) => {
  const text = await page.locator('body').innerText();
  assert(!/[\u3400-\u9fff]/.test(text), `${label}: visible CJK`);
  assert(text.trim().length > 0, `${label}: empty surface`);
};
try {
 if (server.exitCode !== null) throw new Error('public_smoke_server_failed_to_start');
 browser = await chromium.launch({ channel:'chrome',headless:true });
 const page = await browser.newPage({ viewport:{width:1280,height:900} });
 const errors=[]; page.on('pageerror', error=>errors.push(String(error)));
 let signedIn=false;
 const streamRequests=[], cancelled=new Set(), releases=new Map();
 const partialPlan={plan_id:'f'.repeat(64),state:'prepared',journal_present:true,success:null,prepared:{step_count:6}};
 await page.route('**/api/**', async route=>{
  const path=new URL(route.request().url()).pathname;
  const reply=(json,status=200)=>route.fulfill({json,status});
  if(path==='/api/auth/session' && route.request().method()==='POST'){ signedIn=true;return reply({ok:true}); }
  if(path==='/api/auth/me')return signedIn?reply({scope_id:'public-reviewer',display_name:'Reviewer public',deployment:'review'}):reply({detail:'unauthorized'},401);
  if(path==='/api/config')return reply({deployment:'review',runner_enabled:false});
  if(path==='/api/capabilities')return reply({case_read:true,case_qa:true,case_create:false,case_prepare:false,case_approve:false,environment:{execution:'unavailable',ready:false}});
  if(path==='/api/product/cases')return reply([]);
  if(path==='/api/assistant/cancel') { const body=route.request().postDataJSON(); cancelled.add(body.turn_id); releases.get(body.turn_id)?.(); return route.fulfill({status:204}); }
  if(path==='/api/assistant/stream') {
   const body=route.request().postDataJSON();streamRequests.push(body);
   if(body.question==='Stop this turn'&&!cancelled.has(body.turn_id))await new Promise(resolve=>releases.set(body.turn_id,resolve));
   const event=cancelled.has(body.turn_id)?{type:'error',code:'turn_cancelled'}:{type:'final',found:true,mode:'harness',activity:[{kind:'read',label:'Read synthetic evidence',status:'done'}],answer:`Public response: ${body.question}`,passages:[],citations:[]};
   return route.fulfill({contentType:'text/event-stream',body:`id: 1\ndata: ${JSON.stringify(event)}\n\n`}).catch(()=>{});
  }
  if(path==='/api/settings/model')return reply({configured:false});
  if(path==='/api/runs')return reply([{run_id:'historical-1',task:'2283_medium_repair_plan_medium',task_label:'\u5176\u4ed6\u4efb\u52a1',strict_success:null,evaluation_valid:null,score:100,started_at:'2026-10-22T12:00:00Z',agent:{name:'pi',category:'agent_evaluation'},experiment:{model:'synthetic',budget_seconds:10,contract_guidance:{profile:'reviewed-request-v1',sha256:'a'.repeat(64),recorded:true}}},{run_id:'historical-legacy',task:'2283_medium_repair_plan_medium',experiment:{model:'synthetic',budget_seconds:10,contract_guidance:{profile:'none',sha256:null,recorded:false}}}]);
  if(path==='/api/runs/historical-1')return reply({run_id:'historical-1',task:{id:'2283_medium_repair_plan_medium',label:'\u5176\u4ed6\u4efb\u52a1'},experiment:{contract_guidance:{profile:'reviewed-request-v1',sha256:'a'.repeat(64),recorded:true},planner:{profile:'cp-sat-recovery-v1',label:'\u89c4\u5212'},data_profile:{name:'planning-metadata-v1',label:'\u6570\u636e'}},evaluation:{score:100,strict_success:null,evaluation_valid:null,passed_rule_count:1,applicable_rule_count:2,failed_rule_count:1,rule_results:[{dimension:'constraint',rule:'origin_traceability',status:'FAIL',applicable:true},{dimension:'constraint',rule:'demand_coverage',status:'PASS',applicable:true}]}});
  if(path.endsWith('/plans'))return reply([partialPlan]);
  if(path.endsWith('/approvals'))return reply([]);
  if(path.includes('/plans/'))return reply(partialPlan);
  return reply({detail:'not_found'},404);
 });
 await page.goto('http://127.0.0.1:4178/app/');
 await page.locator('#invite-code').waitFor(); await visibleEnglish(page,'login');
 assert(await page.locator('#language-select').count()===0,'public language selector exposed');
 await page.locator('#invite-code').fill('mock-invite');await page.getByRole('button',{name:'Enter workbench',exact:true}).click();
 await page.locator('[data-home-view]').waitFor();await visibleEnglish(page,'home');
 assert(await page.locator('[data-home-card]').count()===4,'start page cards missing');
 assert(await page.locator('[data-home-environment]').count()===1,'start page does not state the environment');
 await page.locator('[data-nav="cases"]').click();
 await page.locator('[data-cases-ready="true"]').waitFor();await visibleEnglish(page,'cases');
 assert(await page.locator('[data-create-case]').isDisabled(),'review can create');
 assert(await page.locator('iframe').count()===0,'native shell exposed');
 await page.locator('[data-open-demo]').click();await page.locator('[data-case-detail]').waitFor();await visibleEnglish(page,'fixture');
 assert(await page.locator('[data-case-approve]').isDisabled(),'fixture can approve');
 await page.locator('[data-plan-view="diff"]').click(); await visibleEnglish(page,'diff');
 await page.locator('[data-ask-case]').click();await page.locator('[data-case-chat]').waitFor();
 assert(await page.locator('[data-case-detail]').count()===0,'chat has background decision controls');
 await page.locator('.chat-preset').first().click();await page.locator('.msg.assistant').waitFor();await visibleEnglish(page,'fixture answer');
 await page.reload();await page.locator('.msg.assistant').waitFor();
 await page.locator('[data-nav="runs"]').click();await page.locator('[data-runs-list]').waitFor();await visibleEnglish(page,'evidence list');
 assert((await page.locator('.recent').first().innerText()).includes('Task 2283'),'public generic label hides task identity');
 // The configuration filter is a listbox menu now: open it to read the labels.
 await page.getByRole('button',{name:'Recorded configuration',exact:true}).click();
 await page.getByRole('listbox').waitFor();
 const configLabels=await page.getByRole('option').allInnerTexts();
 await page.keyboard.press('Escape');
 await page.getByRole('listbox').waitFor({state:'detached'});
 assert(configLabels.some(text=>text.includes('contract: Reviewed request'))&&configLabels.some(text=>text.includes('contract: Not recorded')),'public contract factor not distinguished');
 await page.locator('.run-link').first().click();await page.locator('[data-evidence="panel"]').waitFor();await visibleEnglish(page,'run detail');
 assert((await page.locator('[data-evidence="strict-success"]').innerText()).includes('Unknown'),'missing strict result became verified');
 assert((await page.locator('[data-contract-guidance]').innerText())==='Reviewed request','public contract metadata missing');
 assert((await page.locator('.rule-failures').innerText()).includes('origin_traceability'),'public failure rule hidden');
 assert((await page.locator('[data-rule-counts]').innerText())==='Passed 1 / 2 applicable rules · Failed 1','public rule counts incorrect');
 await page.locator('.plan-picker').waitFor();
 assert((await page.locator('.plan-picker').innerText()).includes('Execution outcome unconfirmed; partial execution may have occurred'),'public legacy journal state falsely says not applied');
 assert(await page.getByRole('button',{name:/Approve|New run/}).count()===0,'history mutations visible');
 await page.locator('[data-nav="knowledge"]').click();await page.locator('[data-knowledge-view]').waitFor();await visibleEnglish(page,'knowledge');
 await page.locator('[data-composer-input]').fill('Stop this turn');await page.locator('button.send').click();
 await page.getByRole('button',{name:'Stop response',exact:true}).click();
 await page.locator('.msg.assistant').filter({hasText:'Stopped receiving this response'}).waitFor();
 await page.locator('[data-composer-input]').fill('A new turn');await page.locator('button.send').click();
 await page.locator('.msg.assistant').filter({hasText:'Public response: A new turn'}).waitFor();
 assert(streamRequests.length===2&&streamRequests[0].turn_id!==streamRequests[1].turn_id,'public new question reused stopped turn');
 assert((await page.locator('.msg.assistant').last().locator('[data-activity-timer]').innerText())==='Done','unknown service elapsed rendered as zero');
 await page.evaluate(()=>{const key='forgeflow.chat.v2.public-reviewer:knowledge';const saved=JSON.parse(localStorage.getItem(key));const activity=saved.sessions.find(item=>item.id===saved.active).messages.at(-1).meta.activity;delete activity.hideDuration;activity.startedAt=activity.finishedAt=Date.now();localStorage.setItem(key,JSON.stringify(saved));});
 await page.reload();await page.locator('.msg.assistant').last().locator('[data-activity-timer]').waitFor();
 assert((await page.locator('.msg.assistant').last().locator('[data-activity-timer]').innerText())==='Done','legacy elapsed fabricated zero still visible');

 await visibleEnglish(page,'stream stop and new turn');
 await page.getByRole('button',{name:'Settings',exact:true}).click();await page.getByRole('dialog').waitFor();await visibleEnglish(page,'settings');
 assert(await page.locator('#settings-language').count()===0,'public settings language exposed');
 await page.keyboard.press('Escape');
 await page.setViewportSize({width:390,height:844});await page.locator('[data-nav="cases"]').click();await page.locator('[data-open-demo]').click();await page.locator('[data-ask-case]').click();await page.locator('[data-case-chat]').waitFor();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth)<=392,'public narrow overflow');
 assert(errors.length===0,errors.join('\n'));
 console.log('public-smoke=ok (English-only; mock API; no ERP/model execution)');
} finally { await browser?.close(); server.kill(); if (temporary) await rm(temporary, { recursive: true }); }
