import { bindWorkspace, workspaceFetch } from "./workspace.js";
const $ = id => document.getElementById(id);
const API = "/api/investigation";
const PENDING_KEY = "forgeflow-investigation-pending-v1";
const statusLabels = {investigating:"调查中",awaiting_human:"等待你的决定",continuation_pending:"继续调查待启动",ready_for_review:"已准备，待交接",handoff_pending:"产品方案准备中",product_review:"待审批具体方案",closing:"正在结束，核对原操作",closed:"已结束",stopped:"已停止",cancelled:"已取消",prepared:"准备完成",needs_clarification:"等待补充信息",infeasible:"已证明无解",unsupported:"超出支持范围",technical_failure:"调查未完成",budget_exhausted:"资源额度已用完",pending:"等待启动"};
let current = null, config = null, owner = null, busy = false, timer = null;

async function request(path, options={}) {
  const response = await workspaceFetch(path, {credentials:"same-origin", ...options,
    headers: {"Content-Type":"application/json", ...options.headers}});
  let body; try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : "request_rejected");
    error.status = response.status; throw error;
  }
  return body;
}
function message(text) { $("message").textContent=text; $("message").hidden=!text; }
function errorText(error) {
  const structuredErrors={invalid_budget_amount:"请输入大于零、最多两位小数的金额，不使用逗号、符号或指数。",case_decision_paused:"预算输入已暂停。请先继续，再重新填写和确认。",ticket_binding_changed:"当前预算问题已变化，请刷新后重新输入。",structured_budget_required:"此事项使用结构化预算，请在金额表单中填写。",structured_budget_mode_required:"此事项保留原答复模式，请使用原入口。",decision_contract_mismatch:"此事项的输入版本不匹配，请保留原事项并核对配置。",ticket_facts_changed:"问题对应的事实已变化，旧候选不能确认。请核清并结束原事项，再在有效授权下开始新调查。"};
  const map={case_closing:"本事项正在结束，后续业务已停止。请核对原操作的收尾状态。",policy_expired:"原政策已过期。请使用结束事项操作核对并释放原占用。",case_revision_conflict:"这项事项已有更新。请查看最新内容，再重新确认。",draft_not_confirmable:"当前答复尚未形成可确认的预算，请继续明确你的决定。",case_not_waiting:"当前阶段已变化，请查看最新状态。",case_usage_unknown:"上次运行的资源用量不完整，当前事项已停止，不能自动继续。",environment_generation_changed:"业务环境已变化，原事项不能继续。",ticket_facts_changed:"提问时的业务事实已变化，需要重新核实。",investigation_unavailable:"当前工作台尚未配置调查续接。",investigation_schedule_unavailable:"业务操作已记录，但启动工作暂不可用。可以重取同一操作回执。",review_is_read_only:"这是只读评审实例。",case_not_found:"未找到属于你的这项事项。",environment_chain_active:"当前业务环境已有进行中的事项，请先查看它。"};
  return structuredErrors[error.message] || map[error.message] || (error.status===401 ? "请先登录工作台。" : "操作未完成。请刷新状态后核对；不会自动重复批准。");
}
function pendingKey(){return `${PENDING_KEY}:${owner||"anonymous"}`;}
function storedPending() { try{return JSON.parse(sessionStorage.getItem(pendingKey())||"null");}catch{return null;} }
function closureKey(){return `${pendingKey()}:closure`;}
function closurePending(){try{return JSON.parse(sessionStorage.getItem(closureKey())||"null");}catch{return null;}}
function isClosurePath(path){return /\/(?:cancel|close-expired|close-reconcile)$/.test(path);}
function businessClosed(){return !!current?.closure_intent||!!current?.environment_released||['closing','closed'].includes(current?.status);}
function needsReconciliation(){return current?.background?.requires_reconciliation===true;}
function showPending() { $("pending").hidden=!storedPending();$("retry").hidden=needsReconciliation()||businessClosed()||config?.read_only;$("pending-note").textContent=needsReconciliation()||businessClosed()?"上次操作及原文已保留。当前不能重试业务操作或重新确认；结束请求不会清除尚未核实的记录。":"上次请求的响应未完整收到。先核对当前状态，或重取同一操作的回执。";$("closure-pending").hidden=!closurePending();$("closure-retry").hidden=!closurePending()||config?.read_only; }
function selectId() { return new URLSearchParams(location.search).get("case"); }
function navigate(id) { const url=new URL(location.href); if(id)url.searchParams.set("case",id);else url.searchParams.delete("case");history.replaceState(null,"",url); }
function node(tag,text,cls) { const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(cls)item.className=cls;return item; }

function render(view) {
  const priorHash=current?.draft?.draft_sha256;
  const priorCase=current?.case_id,priorPaused=current?.decision_paused;
  current=view;$("case-panels").hidden=!view;$("empty").hidden=!!view;
  if(!view){$("phase").textContent="尚未选择事项";$("background-status").hidden=true;$("closure-panel").hidden=!closurePending();showPending();return;}
  const blocked=needsReconciliation(),background=view.background?.status;
  const reasons={worker_result_unknown:"原后台工作缺少可确认的完成回执。",case_usage_unknown:"原后台工作的资源用量尚未核实。",environment_generation_changed:"业务环境已变化。",ticket_facts_changed:"原问题对应的业务事实已变化。",host_handoff_changed:"方案交接的绑定信息需要核对。",worker_receipt_missing:"后台工作的回执尚不完整。",worker_receipt_mismatch:"后台工作回执与原操作不一致。"};
  const reason=(view.background?.jobs||[]).find(job=>job.status==="reconciliation_required")?.reason;
  $("background-status").hidden=!blocked&&!['queued','running'].includes(background);
  $("background-status").textContent=blocked?`需要宿主核对：${reasons[reason]||"后台操作未确认完成。"}原文与历史已保留；核对完成前不能继续确认或交接，也不会自动重试。`:background==='queued'?"后台工作已排队，请稍后刷新。":"后台工作正在进行，请稍后刷新。";
  showPending();
  $("phase").textContent=view.decision_paused?"已暂停等待":statusLabels[view.status]||"状态待核对";
  const ticket=view.ticket,draft=view.draft,proposal=draft?.proposal,structured=view.decision_input_mode==="structured_budget_v1",v2=!structured&&view.reply_contract_version===2;
  const structuredInput=(view.budget_inputs||[]).find(row=>row.input_id===draft?.input_id);
  const missingInput=structured&&proposal?.kind==="budget_authorization_proposal"&&typeof structuredInput?.raw_input?.amount!=="string";
  const unsupportedMode=structured?view.decision_contract_version!==1:![undefined,"legacy_reply"].includes(view.decision_input_mode);
  const inactive=businessClosed()||view.policy_expired===true||unsupportedMode;
  const waiting=view.status==="awaiting_human",paused=view.decision_paused===true,pending=storedPending();
  const editing=structured&&waiting&&!config?.read_only&&!inactive;
  const activeBackground=['queued','running'].includes(background);
  const reasonLabels={current_authorization:"报价、历史金额或被撤回的说明不能作为当前预算决定。",explicit_amount:"还需要一个明确的单笔预算金额。",single_order_amount:"需要目标订单自己的预算，不能代你分配多单总额。",unconditional_single_field_decision:"本次只能补齐预算，需要不附带其他政策变更的明确决定。"};
  $("question").textContent=(v2&&(draft?.host_question||view.reply_outcome?.host_question))||ticket?.question || (view.status==="investigating" ? "正在查明影响范围和缺失的业务决定。" : "本次调查没有可继续补齐的预算问题。");
  $("question-reason").textContent=v2?(draft?.host_reason||reasonLabels[proposal?.need]||""):"";
  if(unsupportedMode)$("question-reason").textContent="此事项的输入版本需要核对。请保留原记录，当前不能继续输入或确认。";
  $("question-reason").hidden=!$("question-reason").textContent;
  $("reply-history").replaceChildren(...(view.replies||[]).map(row=>node("p",row.text)));
  $("reply-history").hidden=structured;
  $("budget-history").hidden=!structured;
  $("budget-history").replaceChildren(...(view.budget_inputs||[]).map(row=>{
    const status=row.status==="invalidated"?"已失效":row.status==="confirmed"?"已确认":draft?.input_id===row.input_id?"待确认":"已记录";
    return node("p",`原始输入：${row.raw_input?.amount??"未提供"} ${view.currency} · ${status}`);
  }));
  $("budget-form").hidden=!editing||paused;
  $("budget-order").value=ticket?`${ticket.order_name}（系统编号 ${ticket.order_id}）`:"尚未确定";
  $("budget-currency").value=view.currency||"尚未核实";
  if(priorCase!==view.case_id||priorPaused!==paused)$("budget-amount").value="";
  if(pending?.path===`${API}/cases/${view.case_id}/budget`)$("budget-amount").value=pending.body.amount;
  $("budget-amount").disabled=busy||blocked||activeBackground||!!pending;
  $("budget-submit").disabled=busy||blocked||activeBackground||!!pending;
  $("budget-submit").textContent=draft?.source==="structured_budget"?"保存更正后的预算":"保存待确认预算";
  $("budget-controls").hidden=!editing;
  $("budget-paused").hidden=!paused;
  $("budget-withdraw").hidden=paused||proposal?.kind!=="budget_authorization_proposal";
  $("budget-pause").hidden=paused;
  $("budget-resume").hidden=!paused;
  for(const id of ["budget-withdraw","budget-pause","budget-resume"])$(id).disabled=busy||blocked||activeBackground||!!pending;
  $("reply-form").hidden=structured||!waiting||config?.read_only||inactive;
  $("reply-button").disabled=blocked||busy;
  $("reply-followup").hidden=v2||!proposal?.question;
  $("reply-followup").textContent=!v2&&proposal?.question?`模型原始追问：${proposal.question}`:"";
  const canConfirm=waiting&&proposal?.kind==="budget_authorization_proposal"&&!config?.read_only&&!blocked&&!inactive&&!paused&&!activeBackground&&!missingInput;
  const stale=(view.draft_history||[]).length>0||!!priorHash&&priorHash!==draft?.draft_sha256&&!view.confirmation;
  $("draft-invalidated").hidden=!stale||!!view.confirmation;
  $("draft-invalidated").textContent="此前未确认的预算候选已失效。只有下方当前答复对应的新候选可以确认；历史金额不会自动沿用。";
  $("confirmation-empty").textContent=blocked?"后台工作需要宿主核对，当前不能确认预算。原输入仍保留在左侧。":missingInput?"该候选的原始金额输入尚未核实，请刷新核对后再确认。":paused?"已暂停。继续后请重新输入预算，再单独确认。":structured?"保存预算后，会在这里展示待确认的单字段变更。":"明确答复后，会在这里展示待确认的单字段变更。";
  $("confirm-form").hidden=!canConfirm;$("confirmation-empty").hidden=canConfirm||!!view.confirmation;
  if(priorHash!==draft?.draft_sha256 || !canConfirm)$("confirm-check").checked=false;
  $("confirm-source-label").textContent=structured?"你的原始金额输入":"你的原始答复";
  if(canConfirm){$("confirm-order").textContent=`${ticket.order_name}（系统编号 ${ticket.order_id}）`;$("confirm-amount").textContent=`${proposal.amount} ${proposal.currency}`;$("confirm-source").textContent=structured?structuredInput.raw_input.amount:(view.replies||[]).find(row=>row.reply_id===draft.reply_id)?.text||"请在左侧核对该候选对应的原始答复。";}
  $("confirm-button").disabled=!$("confirm-check").checked||busy||blocked||!!pending;
  $("confirmed").hidden=!view.confirmation;
  $("confirmed").textContent=view.confirmation ? `已确认 ${ticket?.order_name||"该订单"} 的预算上限为 ${view.confirmation.after} ${view.confirmation.currency}。后续调查将重新核实业务事实。` : "";
  $("episodes").replaceChildren(...(view.episodes||[]).map((episode,index)=>{
    const item=node("li");item.append(node("strong",index===0?"首次调查 · 原政策":"续接调查 · 已确认预算政策"),node("span",statusLabels[episode.status]||"状态待核对"));return item;}));
  const known=view.known_usage||view.usage||{};
  $("usage").textContent=view.usage_complete ? `累计只读查询 ${known.queries??0} 次，校验 ${known.validations??0} 次，准备 ${known.prepares??0} 次。` : "资源记录尚不完整或工作仍在进行；未知用量不会按零处理。";
  $("stop-reason").hidden=!view.terminal||!!view.closure_intent;
  $("stop-reason").textContent=view.terminal ? `本次事项已停止：${statusLabels[view.terminal.status]||"需要进一步处理"}。${view.terminal.reason==="case_usage_unknown"?"上次工作用量未知，无法自动继续。":"请保留当前记录，核对停止原因后再安排新的业务处理。"}` : "";
  $("handoff-button").hidden=view.status!=="ready_for_review"||config?.read_only||blocked||inactive;
  const product=view.handoff?.product_case_id;
  $("product-link").hidden=!(view.handoff?.phase==="complete" && /^[a-f0-9]{32}$/.test(product||""));
  if(!$("product-link").hidden)$("product-link").href=`/#case=${encodeURIComponent(product)}`;
  $("handoff-note").textContent=view.handoff?.phase==="complete" ? "已关联新的产品事项。请查看它重新准备的方案，再决定是否批准执行。" : view.status==="handoff_pending" ? "正在创建和重新准备产品方案，请稍后刷新。" : "重新取证并准备完成后，可以进入既有方案审批流程。";
  const closure=view.closure_intent,closed=!!view.environment_released||closure?.phase==="complete"||view.status==="closed";
  $("closure-panel").hidden=!config?.can_close&&!closure&&!view.policy_expired&&!closurePending();
  $("cancel-button").hidden=!config?.can_close||!!closure||closed||view.policy_expired===true;
  $("expire-button").hidden=!config?.can_close||!view.policy_expired||!!closure||closed;
  $("closure-reconcile").hidden=!config?.can_close||!closure||closed;
  const executionSucceeded=closure?.receipt?.outcome==="execution_validated"||closure?.receipt?.state==="succeeded";
  $("prepared-outcome").textContent=(view.episodes||[]).some(row=>row.status==="prepared")||view.handoff?"调查方案已准备；产品方案仍需独立审批":"尚未完成方案准备";
  $("execution-outcome").textContent=executionSucceeded?"原方案执行回读已核验":"此页尚无已核验执行回执";
  $("verifier-outcome").textContent="请在产品方案中查看原任务验收回执；准备或执行回读不代替该结果。";
  $("closure-status").textContent=closed?(executionSucceeded?"事项已结案。原方案已经执行并核验成功，执行结果保留，没有撤销或回滚。":"事项已结束，原操作已核清，环境占用已释放。"):
    closure?"已记录结束请求，后续业务已停止推进。正在核对原操作；未知工作或执行结果核清前，环境仍保持占用。":
    view.policy_expired?"原政策已过期，不能继续补答、确认或准备方案。可以请求可信收尾。":"如不再继续此事项，可以请求结束。尚未核实的操作仍会保留并核对。";
}

async function refresh() {
  if(!config?.available)return;
  const data=await request(`${API}/cases`),wanted=selectId()||current?.case_id;
  $("case-picker").replaceChildren(node("option","选择一项调查"));$("case-picker").firstChild.value="";
  for(const view of data.cases){const item=node("option",`${view.ticket?.order_name||"供应中断调查"} · ${statusLabels[view.status]||view.status}`);item.value=view.case_id;$("case-picker").append(item);}
  const selected=wanted || data.cases[0]?.case_id;
  if(selected){const view=await request(`${API}/cases/${encodeURIComponent(selected)}`);navigate(selected);$("case-picker").value=selected;render(view);}else render(null);
}
async function perform(path,body,{retry=false}={}) {
  if(config?.read_only){message("这是只读评审实例。");return;}
  const closing=isClosurePath(path),creating=path===`${API}/cases`,key=closing?closureKey():pendingKey();
  const released=!!current?.environment_released;
  if(!closing&&!(creating&&released)&&(needsReconciliation()||businessClosed())){message("后台工作需要核对或本事项正在结束。当前操作与原文会保留，不会重试或继续确认。");return;}
  if(busy)return;
  // Preserve the old operation under its case after a trusted release receipt.
  // A different or unresolved operation still blocks a fresh create request.
  const previous=storedPending();
  if(creating&&!retry&&released&&previous?.path.startsWith(`${API}/cases/${current.case_id}/`)){
    sessionStorage.setItem(`${pendingKey()}:retained:${current.case_id}`,JSON.stringify(previous));
    sessionStorage.removeItem(pendingKey());
  }
  if(!retry&&(closing?closurePending():storedPending())){message("请先重取上次操作回执，避免创建重复请求。");return;}busy=true;message("");
  const operation=retry?{path,body}:{path,body:{operation_id:crypto.randomUUID(),...body}};
  sessionStorage.setItem(key,JSON.stringify(operation));showPending();
  for(const button of document.querySelectorAll("button"))button.disabled=true;
  try{
    const view=await request(operation.path,{method:"POST",body:JSON.stringify(operation.body)});
    sessionStorage.removeItem(key);showPending();if(operation.path.endsWith("/reply"))$("reply").value="";if(operation.path.endsWith("/budget"))$("budget-amount").value="";navigate(view.case_id);render(view);await refresh();
  }catch(error){
    const uncertainRetry = retry && error.message === "workspace_retired";
    if(error.status && error.status<500&&!uncertainRetry&&error.message!=="investigation_reconciliation_required"){sessionStorage.removeItem(key);showPending();}
    message(errorText(error));try{await refresh();}catch{/* Preserve the original failure. */}
  }finally{busy=false;for(const button of document.querySelectorAll("button"))button.disabled=false;$("create").disabled=!config?.can_create;render(current);showPending();}
}
async function boot(){
  try{owner=await bindWorkspace();config=await request(`${API}/config`);$("login").hidden=true;$("workspace").hidden=false;$("create").disabled=!config.can_create;showPending();
    if(!config.available)message("当前工作台尚未配置调查续接。");else await refresh();
    clearInterval(timer);timer=setInterval(()=>{if(!busy&&!document.hidden)refresh().catch(()=>{});},4000);
  }catch(error){if(error.status===401){$("login").hidden=false;$("workspace").hidden=true;}else message(errorText(error));}
}
$("login-form").addEventListener("submit",async event=>{event.preventDefault();try{await request("/api/auth/session",{method:"POST",body:JSON.stringify({invite_code:$("invite").value})});
  $("invite").value="";message("");await boot();}catch{message("登录未成功，请核对邀请码。");}});
$("create").addEventListener("click",()=>perform(`${API}/cases`,{}));
$("refresh").addEventListener("click",()=>refresh().catch(error=>message(errorText(error))));
$("case-picker").addEventListener("change",()=>{navigate($("case-picker").value);current=null;refresh().catch(error=>message(errorText(error)));});
$("reply-form").addEventListener("submit",async event=>{event.preventDefault();if(!current||!$("reply").value.trim())return;const text=$("reply").value;await perform(`${API}/cases/${current.case_id}/reply`,{expected_revision:current.revision,text});if(!storedPending())$("reply").value="";});
$("budget-form").addEventListener("submit",event=>{event.preventDefault();if(!current?.ticket||current.decision_paused||$("budget-submit").disabled||$("budget-form").hidden)return;perform(`${API}/cases/${current.case_id}/budget`,{expected_revision:current.revision,ticket_id:current.ticket.ticket_id,amount:$("budget-amount").value});});
for(const [id,action] of [["budget-withdraw","withdraw"],["budget-pause","pause"],["budget-resume","resume"]])$(id).addEventListener("click",()=>{if(current&&!$(id).hidden&&!$(id).disabled)perform(`${API}/cases/${current.case_id}/budget-control`,{expected_revision:current.revision,action});});
$("confirm-check").addEventListener("change",()=>{$("confirm-button").disabled=!$("confirm-check").checked||busy||needsReconciliation()||!!storedPending()||current?.decision_paused===true;});
$("confirm-form").addEventListener("submit",event=>{event.preventDefault();if(!current?.draft||!$("confirm-check").checked||$("confirm-form").hidden||$("confirm-button").disabled)return;perform(`${API}/cases/${current.case_id}/confirm`,{expected_revision:current.revision,draft_sha256:current.draft.draft_sha256,parent_policy_sha256:current.ticket.parent_policy_sha256,confirmed:true});});
$("handoff-button").addEventListener("click",()=>{if(current)perform(`${API}/cases/${current.case_id}/handoff`,{expected_revision:current.revision});});
$("cancel-button").addEventListener("click",()=>{if(current)perform(`${API}/cases/${current.case_id}/cancel`,{expected_revision:current.revision});});
$("expire-button").addEventListener("click",()=>{if(current)perform(`${API}/cases/${current.case_id}/close-expired`,{});});
$("closure-reconcile").addEventListener("click",()=>{if(current)perform(`${API}/cases/${current.case_id}/close-reconcile`,{});});
$("closure-retry").addEventListener("click",()=>{const saved=closurePending();if(saved&&saved.path.startsWith(`${API}/cases/`)&&isClosurePath(saved.path))perform(saved.path,saved.body,{retry:true});});
$("retry").addEventListener("click",()=>{const saved=storedPending();if(saved&&saved.path.startsWith(`${API}/cases`))perform(saved.path,saved.body,{retry:true});});
boot();
