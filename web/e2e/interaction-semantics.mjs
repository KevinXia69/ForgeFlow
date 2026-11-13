/** Real browser, mocked business receipts; this is not Odoo acceptance. */
import assert from "node:assert/strict";
import {createServer} from "node:http";
import {readFile,mkdir} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import {chromium} from "playwright-core";

const root=fileURLToPath(new URL("../../src/forgeflow/api/static/",import.meta.url));
const output=fileURLToPath(new URL("../../.runtime/checks/interaction-ui/",import.meta.url));
await mkdir(output,{recursive:true});
const files={"/investigation":"investigation.html","/static/investigation.css":"investigation.css","/static/investigation.js":"investigation.js","/static/workspace.js":"workspace.js"};
const server=createServer(async(req,res)=>{
  const name=files[new URL(req.url,"http://localhost").pathname];
  if(!name){res.writeHead(404);res.end();return;}
  res.writeHead(200,{"Content-Type":name.endsWith(".js")?"application/javascript":name.endsWith(".css")?"text/css":"text/html"});
  res.end(await readFile(root+name));
});
await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
const browser=await chromium.launch({channel:"chrome",headless:true});
const base=`http://127.0.0.1:${server.address().port}`,id="ic-"+"a".repeat(32),parent="b".repeat(64);
const prefix=`/api/investigation/cases/${id}`,pendingKey="forgeflow-investigation-pending-v1:review-owner";
let readOnly=false,loseCancel=true;
let view={case_id:id,revision:2,status:"awaiting_human",reply_contract_version:2,policy_expired:false,
  currency:"USD",ticket:{order_name:"SO-30",order_id:"30",parent_policy_sha256:parent,question:"请明确 SO-30 的授权预算上限（USD）。"},
  draft:{reply_id:"quote",proposal:{schema_version:2,kind:"ask_followup",need:"current_authorization",question:"Wrong raw question: SO-999, 500 USD?"},
    host_question:"SO-30 当前获批的预算上限是多少（USD）？",host_reason:"报价不能代替你的当前预算决定。"},
  replies:[{reply_id:"quote",text:"报价 800 USD"}],draft_history:[],confirmation:null,handoff:null,terminal:null,closure_intent:null,environment_released:null,
  episodes:[{episode_id:id+"-e1",parent_episode:null,status:"needs_clarification"}],usage_complete:true,usage:{queries:8,validations:0,prepares:0},
  background:{status:"idle",requires_reconciliation:false,jobs:[]}};
const requests=[],errors=[];
try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});
  page.on("pageerror",error=>errors.push(String(error)));
  await page.route("**/api/**",async route=>{
    const r=route.request(),path=new URL(r.url()).pathname,body=r.postData()?r.postDataJSON():null;
    if(body)requests.push({path,body});
    if(path==="/api/auth/me")return route.fulfill({json:{scope_id:"review-owner"}});
    if(path==="/api/investigation/config")return route.fulfill({json:{available:true,can_create:!readOnly,can_close:!readOnly,read_only:readOnly}});
    if(path==="/api/investigation/cases")return route.fulfill({json:body?view:{cases:[view]}});
    if(path===prefix)return route.fulfill({json:view});
    if(path===prefix+"/reply"){
      if(view.draft?.proposal.kind==="budget_authorization_proposal")view.draft_history.push({reply_id:view.draft.reply_id,amount:view.draft.proposal.amount});
      const reply_id="reply-"+view.revision;
      view.replies.push({reply_id,text:body.text});view.revision++;
      view.draft={reply_id,draft_sha256:String(view.revision).repeat(64),proposal:{schema_version:2,kind:"budget_authorization_proposal",order_id:"30",amount:view.draft_history.length?"900.00":"800.00",currency:"USD"}};
      return route.fulfill({json:view});
    }
    if(path===prefix+"/cancel"){
      assert.equal(body.expected_revision,view.closure_intent?view.revision-1:view.revision);
      if(!view.closure_intent){view.revision++;view.status="closing";view.closure_intent={phase:"reconciliation_required",reason:"user_cancelled",blockers:["worker_result_unknown"]};}
      if(loseCancel){loseCancel=false;return route.abort("failed");}
      return route.fulfill({json:view});
    }
    if(path===prefix+"/close-reconcile"){
      view.status="closed";view.environment_released={at:1};
      view.closure_intent={...view.closure_intent,phase:"closed",blockers:[],receipt:{state:"succeeded",outcome:"execution_validated"}};
      return route.fulfill({json:view});
    }
    if(path===prefix+"/close-expired"){
      assert.deepEqual(Object.keys(body),["operation_id"]);
      view.status="closed";view.environment_released={at:2};view.closure_intent={phase:"closed",reason:"policy_expired",receipt:{kind:"investigation_stopped"}};
      return route.fulfill({json:view});
    }
    throw Error(`Unexpected request ${path}`);
  });
  await page.goto(base+"/investigation",{waitUntil:"networkidle"});
  await page.locator("#question").getByText("SO-30 当前获批").waitFor();
  assert.equal(await page.locator("body").getByText("Wrong raw question",{exact:false}).count(),0);
  await page.locator("#question-reason").getByText("报价不能代替").waitFor();
  view.reply_outcome={accepted:false,reason:"no_accepted_model_proposal",host_question:"What budget cap do you authorize for SO-30 (USD)?"};view.draft=null;
  await page.locator("#refresh").click();await page.locator("#question").getByText("What budget cap").waitFor();
  const original="SO-30 budget 800 USD 🧾\n<script>window.injected=true</script>";
  await page.locator("#reply").fill(original);await page.locator("#reply-button").click();
  await page.locator("#confirm-amount").getByText("800.00 USD").waitFor();
  assert.equal(await page.locator("#confirm-source").textContent(),original);
  assert.equal(await page.evaluate(()=>window.injected),undefined);
  await page.locator("#confirm-check").check();
  await page.locator("#reply").fill("Correction: budget 900 USD for SO-30.");await page.locator("#reply-button").click();
  await page.locator("#confirm-amount").getByText("900.00 USD").waitFor();
  assert.equal(await page.locator("#confirm-check").isChecked(),false);
  await page.locator("#draft-invalidated").getByText("此前未确认的预算候选已失效").waitFor();
  await page.screenshot({path:output+"decision.png",fullPage:true});
  const pending={path:prefix+"/reply",body:{operation_id:"original-pending",expected_revision:3,text:"原始待核对答复"}};
  await page.evaluate(({key,value})=>sessionStorage.setItem(key,JSON.stringify(value)),{key:pendingKey,value:pending});
  view.background={status:"reconciliation_required",requires_reconciliation:true,jobs:[{status:"reconciliation_required",reason:"worker_result_unknown"}]};
  await page.locator("#refresh").click();await page.locator("#background-status").getByText("需要宿主核对").waitFor();
  assert.equal(await page.locator("#confirm-form").isVisible(),false);
  await page.locator("#create").click();assert.equal(requests.filter(r=>r.path==="/api/investigation/cases").length,0);
  await page.locator("#cancel-button").click();await page.locator("#closure-retry").waitFor({state:"visible"});
  await page.locator("#closure-retry").click();await page.locator("#closure-retry").waitFor({state:"hidden"});
  const cancels=requests.filter(r=>r.path.endsWith("/cancel"));assert.equal(cancels.length,2);assert.deepEqual(cancels[0].body,cancels[1].body);
  assert.deepEqual(await page.evaluate(key=>JSON.parse(sessionStorage.getItem(key)),pendingKey),pending);
  assert.equal(await page.locator("#retry").isVisible(),false);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.screenshot({path:output+"closing-mobile.png",fullPage:true});
  await page.locator("#closure-reconcile").click();await page.locator("#closure-status").getByText("原方案已经执行并核验成功").waitFor();
  assert.equal(requests.filter(r=>/\/(?:confirm|handoff|approve|apply)$/.test(r.path)).length,0);
  await page.locator("#create").click();
  assert.equal(requests.filter(r=>r.path==="/api/investigation/cases").length,1);
  assert.deepEqual(await page.evaluate(key=>JSON.parse(sessionStorage.getItem(key)),`${pendingKey}:retained:${id}`),pending);
  view={...view,status:"awaiting_human",closure_intent:null,environment_released:null,policy_expired:true};
  await page.locator("#refresh").click();await page.locator("#expire-button").waitFor({state:"visible"});
  assert.equal(await page.locator("#reply-form").isVisible(),false);
  assert.equal(await page.locator("#cancel-button").isVisible(),false);
  await page.locator("#expire-button").click();await page.locator("#closure-status").getByText("环境占用已释放").waitFor();
  readOnly=true;await page.reload({waitUntil:"networkidle"});
  for(const name of ["cancel-button","expire-button","closure-reconcile","closure-retry"])assert.equal(await page.locator("#"+name).isVisible(),false);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:"passed",scope:"browser with mocked receipts only",canonical_question:true,source_text_exact:true,old_candidate_invalidated:true,
    cancellation_available_during_unknown:true,cancel_replay_identical:true,original_pending_retained:true,new_case_after_release:true,fallback_question_visible:true,executed_success_not_cancelled:true,expiry_and_read_only:true,mobile_no_overflow:true}));
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
