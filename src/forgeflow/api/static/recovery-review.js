import { bindWorkspace, workspaceFetch } from "./workspace.js";
const byId = id => document.getElementById(id);
const api = "/api/recovery-review/session";
let owner, current, sending = false, polling = false, refreshSequence = 0;
const pendingKey = "recovery-review.pending";
async function request(url, body) {
  const response = await workspaceFetch(url, body ? {method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)} : {});
  const value = await response.json();
  if (!response.ok) throw Object.assign(new Error(value.detail || "请求失败"), {status:response.status});
  return value;
}
function element(tag, text) {
  const el = document.createElement(tag);
  el.textContent = text;
  return el;
}
function options(select, items) {
  const previous = select.value;
  select.replaceChildren(new Option("请选择", ""), ...items.map(([name, value]) => new Option(name, value)));
  select.value = previous;
}
function clearReview(error) {
  const wasVisible = !byId("review").hidden;
  current = undefined;
  byId("review").hidden = true;
  byId("login").hidden = false;
  byId("turns").replaceChildren();
  byId("event").textContent = "";
  byId("question-form").reset();
  byId("clarify-form").hidden = true;
  byId("notice").replaceChildren();
  byId("line").replaceChildren();
  if (error.status === 403) {
    sessionStorage.removeItem(pendingKey);
    byId("message").textContent = "当前身份无权访问此工作台，请使用登记身份重新登录。";
  } else if (wasVisible) {
    byId("message").textContent = "";
  }
}
async function refresh() {
  const sequence = ++refreshSequence;
  let next;
  try {
    if (!owner) owner = await bindWorkspace();
    next = await request(api);
  } catch (err) {
    if (sequence !== refreshSequence) return;
    if (err.status !== 401 && err.status !== 403) throw err;
    clearReview(err);
    return;
  }
  if (sequence !== refreshSequence) return;
  const pending = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
  if ((current && current.registration_sha256 !== next.registration_sha256)
    || (pending && pending.body.registration_sha256 !== next.registration_sha256)) {
    byId("question-form").reset();
    byId("notice").value = byId("line").value = "";
    if (pending && pending.body.registration_sha256 !== next.registration_sha256) sessionStorage.removeItem(pendingKey);
    byId("message").textContent = "当前登记或登录身份已变化。请核对当前事件后重新填写。";
  }
  current = next;
  byId("login").hidden = true;
  byId("review").hidden = false;
  byId("event").textContent = current.event.text;
  const turns = current.turns.filter(turn => turn.kind !== "fact");
  byId("question-form").hidden = turns.length > 0;
  byId("turns").replaceChildren();
  for (const turn of turns) {
    const section = element("section", "");
    section.dataset.reviewTurn = turn.operation_id;
    section.dataset.status = turn.status;
    const noticeFailure = {
      additional_purchase_cancellation_unsupported: "方案还会取消通知范围以外的采购单，已停止送审。",
      shared_offer_cancellation_unsupported: "同一报价还有其他有效采购承诺。当前规划器无法单独取消通知所指的采购单，已停止送审。",
      cancellation_subject_changed: "采购单或采购行与通知绑定不一致，需要重新调查。"
    }[turn.detail];
    section.append(element("h2", turn.kind === "clarify" ? "澄清后调查" : "调查记录"),
      element("p", turn.text), element("p", noticeFailure || turn.detail || turn.status));
    if (turn.dispatch_state === "not_dispatched") {
      const reason = turn.detail === "environment_generation_unavailable"
        ? "暂时无法核对 ERP 环境身份" : "ERP 暂时无法读取";
      section.append(element("p", reason + "，本次调查尚未派发给模型。"));
      if (turn.operation_id === current.turns.at(-1)?.operation_id && turn.retries_remaining > 0) {
        const retry = element("button", "按原请求重试（剩余 " + turn.retries_remaining + " 次）");
        retry.onclick = () => submit("retry", {previous:turn.operation_id});
        section.append(retry);
      }
    }
    if (turn.answer) section.append(element("p", turn.answer));
    if (turn.clarification_scope) section.append(element("p", turn.clarification_scope));
    for (const row of turn.commitments || []) section.append(element("p",
      row.name + " · 采购行 " + row.purchase_line_id + " · " + row.product + " · 关联销售订单 " + Object.values(row.target_orders || {}).join("、")));
    if (turn.product_case_id) {
      const link = element("a", "查看并评审具体方案");
      link.href = "/#case=" + encodeURIComponent(turn.product_case_id);
      section.append(link);
    }
    byId("turns").append(section);
  }
  const last = turns.at(-1);
  byId("clarify-form").hidden = last?.status !== "awaiting_clarification";
  if (!byId("clarify-form").hidden) {
    options(byId("notice"), current.notices.map(n => [n.source + " · " + n.text,n.notice_id]));
    options(byId("line"), last.commitments.map(r => [r.name+" · 行 "+r.purchase_line_id,r.purchase_line_id]));
  }
  if (pending?.body.registration_sha256 === current.registration_sha256) {
    if (current.turns.some(t => t.operation_id === pending.body.operation_id)) sessionStorage.removeItem(pendingKey);
    else if (pending.action === "investigate") byId("question").value = pending.body.text;
    else if (pending.action === "clarify") {
      byId("notice").value = pending.body.notice_id;
      byId("line").value = String(pending.body.purchase_line_id);
    }
  }
}
async function submit(action, fields) {
  if (sending) return;
  sending = true;
  try {
    const existing = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
    const body = existing?.body || {operation_id:crypto.randomUUID(), event_sha256:current.event.sha256,
      registration_sha256:current.registration_sha256,...fields};
    if (existing && (existing.action !== action || body.registration_sha256 !== current.registration_sha256
      || Object.keys(fields).some(key => body[key] !== fields[key])))
      throw Error("上一条请求尚未确认。请刷新核对原请求后重试。");
    sessionStorage.setItem(pendingKey, JSON.stringify({action,body}));
    await request(api+"/"+action,body);
    sessionStorage.removeItem(pendingKey);
    byId("message").textContent="";
    await refresh();
  } catch (err) {
    if (err.status && err.status < 500) sessionStorage.removeItem(pendingKey);
    if (err.status === 401 || err.status === 403) {
      ++refreshSequence;
      clearReview(err);
      return;
    }
    byId("message").textContent=err.message === "review_registration_changed"
      ? "登记已变化，请刷新并核对当前事件后重试。" : err.message;
  } finally { sending=false; }
}
byId("question-form").onsubmit=e=>{e.preventDefault();submit("investigate",{text:byId("question").value});};
byId("clarify-form").onsubmit=e=>{e.preventDefault();submit("clarify",{
  previous:current.turns.at(-1).operation_id,notice_id:byId("notice").value,purchase_line_id:Number(byId("line").value)});};
byId("login-form").onsubmit=async e=>{e.preventDefault();try {
  await request("/api/auth/session",{invite_code:byId("invite").value});
  byId("invite").value="";
  byId("message").textContent="";await refresh();
} catch(err){byId("message").textContent=err.message;}};
async function sync() {
  if(sending || polling)return;
  polling = true;
  try{await refresh();}catch(err){byId("message").textContent=err.message;}
  finally{polling = false;}
}
document.addEventListener("visibilitychange",()=>{if(!document.hidden)sync();});
sync();setInterval(()=>{if(!document.hidden)sync();},3500);
