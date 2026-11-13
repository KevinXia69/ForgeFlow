/** Real browser with simulated HTTP receipts; no model or ERP acceptance claim. */
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright-core';

const root=fileURLToPath(new URL('../../src/forgeflow/api/static/',import.meta.url));
const output=fileURLToPath(new URL('../../.runtime/checks/structured-recovery-ui/',import.meta.url));
await mkdir(output,{recursive:true});
const files={'/investigation':'investigation.html','/static/investigation.css':'investigation.css','/static/investigation.js':'investigation.js','/static/workspace.js':'workspace.js'};
const server=createServer(async(req,res)=>{
  const name=files[new URL(req.url,'http://localhost').pathname];
  if(!name){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html'});
  res.end(await readFile(root+name));
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({channel:'chrome',headless:true});
const base=`http://127.0.0.1:${server.address().port}`,id='ic-'+'d'.repeat(32),policy='e'.repeat(64);
const prefix=`/api/investigation/cases/${id}`,pendingKey='forgeflow-investigation-pending-v1:structured-owner';
let readOnly=false,loseBudget=true,loseConfirm=true,childCount=0;
let view={case_id:id,revision:2,status:'awaiting_human',decision_input_mode:'structured_budget_v1',decision_contract_version:1,
  decision_paused:false,budget_inputs:[],currency:'USD',ticket:{ticket_id:'ticket-1',order_name:'SO-30',order_id:'30',
    parent_policy_sha256:policy,question:'请提供 SO-30 当前授权的预算上限（USD）。'},draft:null,draft_history:[],confirmation:null,
  replies:[],handoff:null,terminal:null,closure_intent:null,environment_released:null,policy_expired:false,
  episodes:[{episode_id:id+'-e1',parent_episode:null,status:'needs_clarification'}],usage_complete:true,
  usage:{queries:8,validations:1,prepares:0},background:{status:'idle',requires_reconciliation:false,jobs:[]}};
const requests=[],errors=[],operations=new Map();
function invalidate(reason,operation){
  if(view.draft){const row=view.budget_inputs.find(row=>row.input_id===view.draft.input_id);row.status='invalidated';row.invalidation_reason=reason;
    view.draft_history.push({...view.draft,invalidation_reason:reason,invalidated_by_operation_id:operation});view.draft=null;}
}
try{
  const page=await browser.newPage({viewport:{width:1280,height:1100}});
  page.on('pageerror',error=>errors.push(String(error)));
  await page.route('**/api/**',async route=>{
    const req=route.request(),path=new URL(req.url()).pathname,body=req.postData()?req.postDataJSON():null;
    if(body)requests.push({path,body});
    if(path==='/api/auth/me')return route.fulfill({json:{scope_id:'structured-owner'}});
    if(path==='/api/investigation/config')return route.fulfill({json:{available:true,can_create:!readOnly,can_close:!readOnly,read_only:readOnly}});
    if(path==='/api/investigation/cases')return route.fulfill({json:body?view:{cases:[view]}});
    if(path===prefix)return route.fulfill({json:view});
    if(body&&operations.has(body.operation_id)){
      assert.deepEqual(operations.get(body.operation_id),{path,body});
      return route.fulfill({json:view});
    }
    if(path===prefix+'/budget'){
      assert.deepEqual(Object.keys(body).sort(),['amount','expected_revision','operation_id','ticket_id']);
      assert.equal(typeof body.amount,'string');assert.equal(body.ticket_id,view.ticket.ticket_id);
      assert.equal(body.expected_revision,view.revision);assert.equal(view.decision_paused,false);
      operations.set(body.operation_id,{path,body});invalidate('replaced',body.operation_id);view.revision++;
      const [whole,fraction='']=body.amount.split('.'),amount=whole+'.'+fraction.padEnd(2,'0');
      const input_id='input-'+view.revision;
      view.budget_inputs.push({input_id,raw_input:{ticket_id:body.ticket_id,amount:body.amount},canonical_amount:amount,status:'candidate'});
      view.draft={input_id,source:'structured_budget',decision_input_mode:'structured_budget_v1',decision_contract_version:1,
        draft_sha256:String(view.revision).padStart(64,'0'),parent_policy_sha256:policy,
        proposal:{kind:'budget_authorization_proposal',order_id:'30',amount,currency:'USD'}};
      if(loseBudget){loseBudget=false;return route.abort('failed');}
      return route.fulfill({json:view});
    }
    if(path===prefix+'/budget-control'){
      assert.deepEqual(Object.keys(body).sort(),['action','expected_revision','operation_id']);
      assert.equal(body.expected_revision,view.revision);operations.set(body.operation_id,{path,body});
      invalidate(body.action,body.operation_id);view.decision_paused=body.action==='pause';view.revision++;
      return route.fulfill({json:view});
    }
    if(path===prefix+'/confirm'){
      assert.equal(body.confirmed,true);assert.equal(body.draft_sha256,view.draft.draft_sha256);
      assert.equal(body.parent_policy_sha256,policy);assert.equal(body.expected_revision,view.revision);
      operations.set(body.operation_id,{path,body});childCount++;view.revision++;view.status='continuation_pending';
      view.confirmation={after:view.draft.proposal.amount,currency:'USD',input_id:view.draft.input_id};
      view.budget_inputs.find(row=>row.input_id===view.draft.input_id).status='confirmed';
      view.episodes.push({episode_id:id+'-e2',parent_episode:id+'-e1',status:'pending'});
      if(loseConfirm){loseConfirm=false;return route.abort('failed');}
      return route.fulfill({json:view});
    }
    if(path===prefix+'/handoff'){
      operations.set(body.operation_id,{path,body});view.revision++;view.status='product_review';
      view.handoff={phase:'complete',product_case_id:'f'.repeat(32)};return route.fulfill({json:view});
    }
    if(path===prefix+'/cancel'){
      operations.set(body.operation_id,{path,body});view.revision++;view.status='closing';
      view.closure_intent={phase:'reconciliation_required',reason:'user_cancelled',blockers:['worker_result_unknown']};
      return route.fulfill({json:view});
    }
    throw Error(`Unexpected HTTP operation ${path}`);
  });
  await page.goto(base+'/investigation',{waitUntil:'networkidle'});
  await page.locator('#budget-form').waitFor({state:'visible'});
  assert.equal(await page.locator('#reply-form').isVisible(),false);
  assert.equal(await page.locator('#budget-order').inputValue(),'SO-30（系统编号 30）');
  assert.equal(await page.locator('#budget-order').getAttribute('readonly'),'');
  assert.equal(await page.locator('#budget-currency').inputValue(),'USD');
  await page.locator('#budget-amount').fill('8e2');await page.locator('#budget-submit').click();
  assert.equal(requests.filter(r=>r.path.endsWith('/budget')).length,0);
  await page.locator('#budget-amount').fill('800.0');await page.locator('#budget-submit').click();
  await page.locator('#pending').waitFor({state:'visible'});
  const original=await page.evaluate(key=>JSON.parse(sessionStorage.getItem(key)),pendingKey);
  assert.equal(original.body.amount,'800.0');
  await page.reload({waitUntil:'networkidle'});
  assert.equal(await page.locator('#budget-amount').inputValue(),'800.0');
  assert.equal(await page.locator('#budget-submit').isDisabled(),true);
  assert.equal(await page.locator('#confirm-button').isDisabled(),true);
  await page.locator('#retry').click();await page.locator('#pending').waitFor({state:'hidden'});
  assert.deepEqual(requests.filter(r=>r.path.endsWith('/budget')).map(r=>r.body),[original.body,original.body]);
  assert.equal(await page.locator('#confirm-source').textContent(),'800.0');
  await page.locator('#confirm-amount').getByText('800.00 USD').waitFor();
  const inputs=view.budget_inputs;view.budget_inputs=[];
  await page.locator('#refresh').click();await page.locator('#confirmation-empty').getByText('原始金额输入尚未核实').waitFor();
  assert.equal(await page.locator('#confirm-form').isVisible(),false);
  view.budget_inputs=inputs;await page.locator('#refresh').click();await page.locator('#confirm-form').waitFor({state:'visible'});
  await page.locator('#confirm-check').check();
  await page.locator('#budget-amount').fill('900.25');await page.locator('#budget-submit').click();
  await page.locator('#confirm-amount').getByText('900.25 USD').waitFor();
  assert.equal(await page.locator('#confirm-check').isChecked(),false);
  await page.locator('#draft-invalidated').waitFor({state:'visible'});
  await page.screenshot({path:output+'corrected-budget.png',fullPage:true});
  await page.locator('#budget-withdraw').click();await page.locator('#confirm-form').waitFor({state:'hidden'});
  assert.equal(view.closure_intent,null);assert.equal(view.status,'awaiting_human');
  await page.locator('#budget-amount').fill('950');await page.locator('#budget-submit').click();
  await page.locator('#confirm-form').waitFor({state:'visible'});
  await page.locator('#budget-pause').click();await page.locator('#budget-paused').waitFor({state:'visible'});
  assert.equal(await page.locator('#budget-form').isVisible(),false);
  assert.equal(await page.locator('#confirm-form').isVisible(),false);
  await page.reload({waitUntil:'networkidle'});await page.locator('#budget-resume').click();
  await page.locator('#budget-form').waitFor({state:'visible'});
  assert.equal(await page.locator('#budget-amount').inputValue(),'');
  assert.equal(await page.locator('#confirm-form').isVisible(),false);
  await page.locator('#budget-amount').fill('1000.10');await page.locator('#budget-submit').click();
  await page.locator('#confirm-check').check();await page.locator('#confirm-button').click();
  await page.locator('#pending').waitFor({state:'visible'});await page.reload({waitUntil:'networkidle'});
  await page.locator('#retry').click();await page.locator('#pending').waitFor({state:'hidden'});
  const confirmations=requests.filter(r=>r.path.endsWith('/confirm'));
  assert.equal(confirmations.length,2);assert.deepEqual(confirmations[0].body,confirmations[1].body);
  assert.equal(childCount,1);assert.equal(view.episodes.length,2);
  view.status='ready_for_review';view.episodes[1].status='prepared';
  await page.locator('#refresh').click();await page.locator('#handoff-button').click();
  await page.locator('#product-link').waitFor({state:'visible'});
  assert.equal(await page.locator('#product-link').getAttribute('href'),'/#case='+'f'.repeat(32));
  await page.locator('#prepared-outcome').getByText('调查方案已准备').waitFor();
  assert.match(await page.locator('#execution-outcome').textContent(),/尚无/);
  assert.match(await page.locator('#verifier-outcome').textContent(),/不代替/);
  // Simulate another waiting-state projection to exercise blocked controls.
  view={...view,status:'awaiting_human',confirmation:null,handoff:null,
    background:{status:'reconciliation_required',requires_reconciliation:true,jobs:[{status:'reconciliation_required',reason:'worker_result_unknown'}]}};
  await page.locator('#refresh').click();await page.locator('#background-status').waitFor({state:'visible'});
  assert.equal(await page.locator('#budget-submit').isDisabled(),true);
  assert.equal(await page.locator('#budget-pause').isDisabled(),true);
  assert.equal(await page.locator('#confirm-form').isVisible(),false);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.screenshot({path:output+'blocked-mobile.png',fullPage:true});
  await page.locator('#cancel-button').click();await page.locator('#closure-reconcile').waitFor({state:'visible'});
  assert.equal(view.environment_released,null);
  view={...view,status:'awaiting_human',closure_intent:null,policy_expired:true};
  await page.locator('#refresh').click();await page.locator('#expire-button').waitFor({state:'visible'});
  assert.equal(await page.locator('#budget-form').isVisible(),false);
  assert.equal(await page.locator('#confirm-form').isVisible(),false);
  view.policy_expired=false;view.decision_contract_version=2;
  await page.locator('#refresh').click();await page.locator('#question-reason').getByText('输入版本需要核对').waitFor();
  assert.equal(await page.locator('#budget-form').isVisible(),false);
  assert.equal(await page.locator('#confirm-form').isVisible(),false);
  view.decision_contract_version=1;
  readOnly=true;await page.reload({waitUntil:'networkidle'});
  for(const selector of ['budget-form','budget-controls','confirm-form','cancel-button','expire-button','closure-reconcile','retry'])assert.equal(await page.locator('#'+selector).isVisible(),false);
  assert.equal(requests.filter(r=>/\/(?:reply|approve|apply|execute)$/.test(r.path)).length,0);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:'passed',scope:'browser with simulated HTTP receipts, no model or ERP',
    structured_original_input:true,read_only_scope:true,candidate_correction_resets_confirmation:true,
    pause_resume_requires_new_input:true,withdraw_is_not_case_cancel:true,refresh_replay_identical:true,
    unique_confirmed_child:true,unknown_and_expiry_keep_closure_available:true,three_outcomes_separate:true,
    mobile_no_overflow:true,legacy_browser_regression:'run separately'}));
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
