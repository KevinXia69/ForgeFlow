/** Hermetic UI interaction only; mocked receipts are not business evidence. */
import assert from "node:assert/strict";
import {createServer} from "node:http";
import {readFile,mkdir} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import {chromium} from "playwright-core";

const root=fileURLToPath(new URL("../../src/forgeflow/api/static/",import.meta.url));
const output=fileURLToPath(new URL("../../.runtime/checks/investigation-ui/",import.meta.url));
await mkdir(output,{recursive:true});
const files={"/investigation":"investigation.html","/static/investigation.css":"investigation.css","/static/investigation.js":"investigation.js","/static/workspace.js":"workspace.js"};
const server=createServer(async(req,res)=>{
  const path=new URL(req.url,"http://localhost").pathname,name=files[path];
  if(!name){res.writeHead(404);res.end();return;}
  const type=name.endsWith(".js")?"application/javascript":name.endsWith(".css")?"text/css":"text/html";
  res.writeHead(200,{"Content-Type":type});res.end(await readFile(root+name));
});
await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
const base=`http://127.0.0.1:${server.address().port}`;
const browser=await chromium.launch({channel:"chrome",headless:true});
const caseId="ic-"+"a".repeat(32),productId="b".repeat(32),hash="c".repeat(64),parentHash="d".repeat(64);
let view={case_id:caseId,status:"awaiting_human",revision:2,currency:"USD",ticket:{order_name:"SO-30",order_id:"30",parent_policy_sha256:parentHash,question:"请明确 SO-30 的授权预算上限（USD）。"},draft:null,confirmation:null,handoff:null,terminal:null,replies:[],episodes:[{episode_id:caseId+"-e1",status:"needs_clarification",parent_episode:null}],usage_complete:true,usage:{queries:8,validations:0,prepares:0}};
let loseReply=true;
const requests=[],errors=[];
try{
  const page=await browser.newPage({viewport:{width:1360,height:1000}});
  page.on("pageerror",error=>errors.push(String(error)));
  await page.route("**/api/**",async route=>{
    const request=route.request(),path=new URL(request.url()).pathname,body=request.postData()?request.postDataJSON():null;
    if(body)requests.push({path,body});
    if(path==="/api/auth/me")return route.fulfill({json:{scope_id:"owner-A",display_name:"Alex"}});
    if(path==="/api/investigation/config")return route.fulfill({json:{available:true,can_create:true,read_only:false}});
    if(path==="/api/investigation/cases")return route.fulfill({json:{cases:[view]}});
    if(path===`/api/investigation/cases/${caseId}`)return route.fulfill({json:view});
    if(path.endsWith("/reply")){
      if(!view.replies.length){view.replies=[{text:body.text}];view.revision=3;view.draft={draft_sha256:hash,proposal:{kind:"budget_authorization_proposal",order_id:"30",amount:"800.00",currency:"USD"}};}
      if(loseReply){loseReply=false;return route.abort("failed");}
      return route.fulfill({json:view});
    }
    if(path.endsWith("/confirm")){
      assert.equal(body.confirmed,true);assert.equal(body.draft_sha256,hash);assert.equal(body.parent_policy_sha256,parentHash);
      assert.equal(body.expected_revision,3);assert.ok(!("actor" in body));
      view={...view,revision:4,status:"continuation_pending",confirmation:{after:"800.00",currency:"USD"},episodes:[...view.episodes,{episode_id:caseId+"-e2",parent_episode:caseId+"-e1",status:"pending"}]};
      return route.fulfill({json:view});
    }
    if(path.endsWith("/handoff")){
      view={...view,revision:6,status:"handoff_pending",handoff:{phase:"pending",product_case_id:productId}};
      return route.fulfill({json:view});
    }
    throw Error(`unexpected request ${path}`);
  });
  await page.goto(base+"/investigation",{waitUntil:"networkidle"});
  await page.locator("#question").getByText("请明确 SO-30").waitFor();
  assert.equal(await page.locator("#confirm-form").isVisible(),false);
  await page.locator("#reply").fill("SO-30 最多批准 800，其他条件照旧。<script>window.bad=true</script>");
  await page.locator("#reply-button").click();
  await page.locator("#pending").waitFor({state:"visible"});
  await page.locator("#retry").click();
  await page.locator("#pending").waitFor({state:"hidden"});
  const replies=requests.filter(row=>row.path.endsWith("/reply"));
  assert.equal(replies.length,2);assert.deepEqual(replies[0].body,replies[1].body);
  assert.equal(await page.evaluate(()=>window.bad),undefined);
  view.replies[0].text="SO-30 最多批准 800，其他条件照旧。";
  await page.locator("#refresh").click();
  await page.locator("#confirm-amount").getByText("800.00 USD").waitFor();
  assert.equal(await page.locator("#confirm-button").isDisabled(),true);
  await page.screenshot({path:output+"desktop.png",fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:output+"mobile.png",fullPage:true});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.locator("#confirm-check").check();
  await page.locator("#confirm-button").click();
  await page.locator("#confirmed").waitFor({state:"visible"});
  assert.equal(requests.filter(row=>row.path.includes("/approve")).length,0);
  view={...view,revision:5,status:"ready_for_review",episodes:view.episodes.map((row,index)=>index?{...row,status:"prepared"}:row)};
  await page.locator("#refresh").click();
  await page.locator("#handoff-button").click();
  view={...view,status:"product_review",handoff:{phase:"complete",product_case_id:productId}};
  await page.locator("#refresh").click();
  await page.locator("#product-link").waitFor({state:"visible"});
  assert.equal(await page.locator("#product-link").getAttribute("href"),`/#case=${productId}`);
  view={...view,usage_complete:false,usage:{queries:null,validations:null,prepares:null}};
  await page.locator("#refresh").click();
  await page.locator("#usage").getByText("未知用量不会按零处理").waitFor();
  // A durable background failure is visible even while the public business
  // state remains awaiting_human. Keep the original pending operation intact.
  const pending={path:`/api/investigation/cases/${caseId}/reply`,body:{operation_id:"keep-original",expected_revision:3,text:"原始未确认答复"}};
  await page.evaluate(value=>sessionStorage.setItem("forgeflow-investigation-pending-v1:owner-A",JSON.stringify(value)),pending);
  view={...view,status:"awaiting_human",confirmation:null,handoff:null,background:{status:"reconciliation_required",requires_reconciliation:true,jobs:[{action:"reply",status:"reconciliation_required",reason:"worker_result_unknown"}]}};
  await page.locator("#refresh").click();
  await page.locator("#background-status").getByText("需要宿主核对").waitFor();
  assert.equal(await page.locator("#confirm-form").isVisible(),false);
  assert.equal(await page.locator("#handoff-button").isVisible(),false);
  assert.equal(await page.locator("#reply-button").isDisabled(),true);
  assert.equal(await page.locator("#retry").isVisible(),false);
  assert.equal(await page.locator("#pending").isVisible(),true);
  assert.match(await page.locator("#reply-history").innerText(),/SO-30/);
  await page.locator("#reply").fill("正在编辑的原文保留");
  const beforeBlocked=requests.length;
  await page.locator("#retry").evaluate(button=>button.click());
  await page.locator("#refresh").click();
  assert.equal(requests.length,beforeBlocked);
  assert.equal(await page.locator("#reply").inputValue(),"正在编辑的原文保留");
  assert.deepEqual(await page.evaluate(()=>JSON.parse(sessionStorage.getItem("forgeflow-investigation-pending-v1:owner-A"))),pending);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.screenshot({path:output+"reconciliation.png",fullPage:true});
  view={...view,status:"ready_for_review"};
  await page.locator("#refresh").click();
  assert.equal(await page.locator("#handoff-button").isVisible(),false);
  view={...view,status:"awaiting_human",background:{status:"running",requires_reconciliation:false,jobs:[{action:"reply",status:"running",reason:null}]}};
  await page.locator("#refresh").click();
  await page.locator("#background-status").getByText("后台工作正在进行").waitFor();
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:"passed",scope:"hermetic UI only",reply_replay_identical:true,explicit_confirmation:true,no_approval_call:true,mobile_no_overflow:true,reconciliation_visible_blocks_actions:true,pending_and_original_text_retained:true,screenshots:[".runtime/checks/investigation-ui/desktop.png",".runtime/checks/investigation-ui/mobile.png",".runtime/checks/investigation-ui/reconciliation.png"]}));
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
