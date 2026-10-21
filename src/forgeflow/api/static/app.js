"use strict";

const state = { runId: null, planId: null };
const LABELS = {
  prepared: "已准备", succeeded: "已成功", stopped: "已停止",
  verified: "已验证", absent: "无记录", invalid: "无效",
  valid: "有效", revoked: "已撤销", expired: "已过期", used: "已使用",
};

const $ = (id) => document.getElementById(id);
function text(value) {
  if (value === null || value === undefined || value === "") return "未记录";
  return String(value);
}

function booleanText(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  return "未记录";
}
function label(value) {
  if (value === null || value === undefined || value === "") return "未记录";
  return LABELS[value] ? `${LABELS[value]}（${value}）` : String(value);
}

function setStatus(id, message, isError = false) {
  const node = $(id);
  node.textContent = message;
  node.classList.toggle("error", isError);
}

function fill(id, value) { $(id).textContent = text(value); }

function cell(row, value) {
  const td = row.appendChild(document.createElement("td"));
  td.textContent = text(value);
  return td;
}

function operationText(counts) {
  if (!counts) return "未记录";
  const names = [["保留", counts.keep], ["更新", counts.update],
    ["取消", counts.cancel], ["新建", counts.create]];
  return names.map(([name, value]) => `${name} ${value === null || value === undefined ? "—" : value}`).join(" · ");
}

async function api(path, options = {}) {
  const init = { credentials: "same-origin", ...options };
  if (init.body) {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
  }
  const response = await fetch(path, init);
  let body = null;
  try {
    body = await response.json();
  } catch (error) {
    body = null;
  }
  if (!response.ok) {
    let detail = body && body.detail;
    if (Array.isArray(detail)) {
      detail = detail.map((item) => item.msg || "请求无效").join("；");
    }
    const failure = new Error(detail ? String(detail) : `请求失败（HTTP ${response.status}）`);
    failure.status = response.status;
    throw failure;
  }
  return body;
}

function planPath() {
  return `/api/runs/${encodeURIComponent(state.runId)}/plans/${encodeURIComponent(state.planId)}`;
}

function showLoginError(message) {
  const node = $("login-error");
  node.textContent = message;
  node.hidden = !message;
}

function showConsole() {
  $("login-card").hidden = true;
  $("app-panel").hidden = false;
}

function showView(name) {
  for (const view of ["assistant", "runs"]) {
    const node = document.getElementById(`view-${view}`);
    if (node) node.hidden = view !== name;
  }
  for (const button of document.querySelectorAll(".nav-item")) {
    button.classList.toggle("is-active", button.dataset.view === name);
  }
}

function renderRecent(runs) {
  const list = $("recent-runs");
  list.replaceChildren();
  for (const run of runs.slice(0, 6)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "recent";
    button.title = run.run_id;
    const label = document.createElement("span");
    label.textContent = run.task_label || run.task || run.run_id;
    const score = document.createElement("span");
    score.className = "recent-score";
    score.textContent = run.score === null || run.score === undefined ? "—" : String(run.score);
    button.append(label, score);
    button.addEventListener("click", () => openRun(run.run_id));
    list.append(button);
  }
}

function openRun(runId) {
  showView("runs");
  const row = document.querySelector(`#runs-body tr[data-run-id="${CSS.escape(runId)}"]`);
  if (row) row.click();
}

function hashRun() {
  const match = /^#run=(.+)$/.exec(location.hash);
  return match ? decodeURIComponent(match[1]) : null;
}

async function loadRuns() {
  setStatus("runs-status", "加载中…");
  try {
    const runs = await api("/api/runs");
    const body = $("runs-body");
    body.replaceChildren();
    setStatus("runs-status", runs.length ? `共 ${runs.length} 条运行记录。` : "暂无运行记录。");
    for (const run of runs) {
      const row = document.createElement("tr");
      row.tabIndex = 0;
      row.dataset.runId = run.run_id;
      [run.run_id, run.task_label || run.task, run.score,
        booleanText(run.strict_success), booleanText(run.evaluation_valid),
        run.security_profile].forEach((value) => cell(row, value));
      const open = () => selectRun(run.run_id, row);
      row.addEventListener("click", open);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
      body.appendChild(row);
    }
    renderRecent(runs);
    const wanted = hashRun();
    if (wanted) {
      const selected = body.querySelector(`tr[data-run-id="${CSS.escape(wanted)}"]`);
      if (selected) await selectRun(wanted, selected);
    }
  } catch (error) {
    setStatus("runs-status", `加载运行记录失败：${error.message}`, true);
  }
}

let runEvents = null;

function watchRun(runId) {
  if (runEvents) {
    runEvents.close();
    runEvents = null;
  }
  if (typeof EventSource === "undefined") return;
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  runEvents = source;
  source.addEventListener("snapshot", (event) => {
    if (state.runId !== runId) {
      source.close();
      return;
    }
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    fill("run-score", payload.score);
    fill("run-valid", booleanText(payload.evaluation_valid));
    fill("run-attestation", label(payload.attestation));
  });
  source.addEventListener("end", () => {
    source.close();
    if (runEvents === source) runEvents = null;
  });
  source.onerror = () => {
    source.close();
    if (runEvents === source) runEvents = null;
  };
}

async function selectRun(runId, row) {
  showView("runs");
  state.runId = runId;
  history.replaceState(null, "", `#run=${encodeURIComponent(runId)}`);
  state.planId = null;
  for (const other of $("runs-body").querySelectorAll("tr")) other.classList.remove("selected");
  row.classList.add("selected");
  $("plan-detail").hidden = true;
  $("approval-panel").hidden = true;
  setStatus("plan-status", "加载运行详情…");
  try {
    const detail = await api(`/api/runs/${encodeURIComponent(runId)}`);
    const evaluation = detail.evaluation || {};
    const timing = detail.timing || {};
    const experiment = detail.experiment || {};
    const attestation = detail.attestation || {};
    fill("run-score", evaluation.score);
    fill("run-valid", booleanText(evaluation.evaluation_valid));
    fill("run-attestation", label(attestation.status));
    fill("run-model", experiment.model);
    fill("run-started", timing.started_at);
    await loadPlans();
    watchRun(runId);
  } catch (error) {
    setStatus("plan-status", `加载运行详情失败：${error.message}`, true);
  }
}

async function loadPlans() {
  if (!state.runId) return;
  setStatus("plan-status", "加载计划…");
  const plans = await api(`/api/runs/${encodeURIComponent(state.runId)}/plans`);
  const body = $("plans-body");
  body.replaceChildren();
  setStatus("plan-status", plans.length ? `共 ${plans.length} 个计划。` : "该运行没有可读取的计划。");
  for (const plan of plans) {
    const prepared = plan.prepared || {};
    const row = document.createElement("tr");
    row.tabIndex = 0;
    row.dataset.planId = plan.plan_id;
    const idCell = cell(row, plan.plan_id);
    idCell.className = "mono break";
    cell(row, label(plan.state));
    cell(row, prepared.step_count);
    cell(row, prepared.expected_new_spend);
    cell(row, operationText(prepared.operation_counts));
    const open = () => selectPlan(plan.plan_id, row);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    body.appendChild(row);
  }
}

function renderPlanDetail(plan) {
  const prepared = plan.prepared || {};
  const fields = [
    ["计划 ID", plan.plan_id],
    ["状态", label(plan.state)],
    ["步骤数", prepared.step_count],
    ["预期支出", prepared.expected_new_spend],
    ["操作计数", operationText(prepared.operation_counts)],
    ["策略摘要", plan.policy_sha256],
    ["快照摘要", plan.snapshot_sha256],
    ["执行日志", plan.journal_present ? "已记录" : "未记录"],
  ];
  const list = document.createElement("dl");
  list.className = "facts";
  for (const [name, value] of fields) {
    const dt = document.createElement("dt");
    dt.textContent = name;
    const dd = document.createElement("dd");
    dd.textContent = text(value);
    if (name.endsWith("摘要")) dd.className = "mono break";
    list.append(dt, dd);
  }
  const node = $("plan-detail");
  node.replaceChildren(list);
  node.hidden = false;
}

async function selectPlan(planId, row) {
  state.planId = planId;
  for (const other of $("plans-body").querySelectorAll("tr")) other.classList.remove("selected");
  row.classList.add("selected");
  $("plan-detail").hidden = true;
  $("approval-panel").hidden = true;
  try {
    const plan = await api(planPath());
    renderPlanDetail(plan);
    $("approval-panel").hidden = false;
    setStatus("approval-status", "");
    await loadApprovals();
  } catch (error) {
    setStatus("plan-status", `加载计划详情失败：${error.message}`, true);
  }
}

async function loadApprovals() {
  if (!state.runId || !state.planId) return;
  try {
    const records = await api(`${planPath()}/approvals`);
    const body = $("approvals-body");
    body.replaceChildren();
    if (!records.length) {
      setStatus("approval-status", "当前计划没有审批记录。");
      return;
    }
    for (const record of records) {
      const row = document.createElement("tr");
      const nonce = cell(row, record.nonce);
      nonce.className = "mono break";
      cell(row, label(record.status));
      cell(row, record.expires_at_utc);
      const action = document.createElement("td");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary small";
      button.textContent = "撤销";
      button.addEventListener("click", () => revokeApproval(record.nonce));
      action.appendChild(button);
      row.appendChild(action);
      body.appendChild(row);
    }
    renderRecent(runs);
    const wanted = hashRun();
    if (wanted) {
      const selected = body.querySelector(`tr[data-run-id="${CSS.escape(wanted)}"]`);
      if (selected) await selectRun(wanted, selected);
    }
  } catch (error) {
    setStatus("approval-status", `加载审批失败：${error.message}`, true);
  }
}

async function approvePlan() {
  if (!state.runId || !state.planId) return;
  const button = $("approve-button");
  button.disabled = true;
  setStatus("approval-status", "正在批准…");
  try {
    const record = await api(`${planPath()}/approvals`, {
      method: "POST",
      body: JSON.stringify({ ttl_seconds: 900 }),
    });
    await loadApprovals();
    setStatus("approval-status",
      `已批准：nonce=${record.nonce}，到期时间 ${record.expires_at_utc}`);
  } catch (error) {
    const prefix = error.status === 409 ? "批准冲突" : "批准失败";
    setStatus("approval-status", `${prefix}：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function revokeApproval(nonce) {
  if (!state.runId || !state.planId) return;
  try {
    await api(`${planPath()}/approvals/${encodeURIComponent(nonce)}/revoke`, { method: "POST" });
    await loadApprovals();
    setStatus("approval-status", "已撤销该审批。");
  } catch (error) {
    setStatus("approval-status", `撤销失败：${error.message}`, true);
  }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("invite-code");
  const code = input.value;
  if (!code) {
    showLoginError("请输入邀请码。");
    return;
  }
  $("login-button").disabled = true;
  showLoginError("");
  try {
    await api("/api/auth/session", {
      method: "POST",
      body: JSON.stringify({ invite_code: code }),
    });
    input.value = "";
    showConsole();
    showView("assistant");
    await loadRuns();
  } catch (error) {
    input.value = "";
    showLoginError(`登录失败：${error.message}`);
  } finally {
    $("login-button").disabled = false;
  }
});

async function createRun() {
  const scenario = $("scenario-select").value;
  const key = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  $("create-run").disabled = true;
  setStatus("create-status", "正在创建运行…");
  try {
    const created = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ scenario, idempotency_key: key }),
    });
    setStatus("create-status", `已创建 ${created.run_id}（${created.state}）`);
    await loadRuns();
  } catch (error) {
    setStatus("create-status", `创建失败：${error.message}`, true);
  } finally {
    $("create-run").disabled = false;
  }
}

async function applyDeployment() {
  try {
    const info = await api("/api/config");
    if (info.deployment === "review") {
      document.body.classList.add("review");
      $("review-banner").hidden = false;
    }
  } catch (error) {
    // The banner is informational; a missing config must not block the console.
  }
}

async function restoreSession() {
  try {
    await api("/api/runs");
  } catch (error) {
    return;  // stay on the login card; 401 is the normal first visit
  }
  showConsole();
  showView(hashRun() ? "runs" : "assistant");
  await loadRuns();
}

$("new-chat").addEventListener("click", () => {
  $("kb-results").replaceChildren();
  conversation.length = 0;
  setStatus("kb-status", "");
  showView("assistant");
  $("kb-question").focus();
});

for (const button of document.querySelectorAll(".nav-item")) {
  button.addEventListener("click", () => showView(button.dataset.view));
}

for (const button of document.querySelectorAll("[data-quick]")) {
  button.addEventListener("click", () => {
    const target = button.dataset.quick;
    if (target === "A" || target === "B") $("scenario-select").value = target;
    showView("runs");
  });
}

$("create-run").addEventListener("click", createRun);
$("runs-refresh").addEventListener("click", loadRuns);
$("approve-button").addEventListener("click", approvePlan);


const conversation = [];

function appendTurn(role, question, passages) {
  const article = document.createElement("article");
  article.className = `kb-turn ${role}`;
  const heading = document.createElement("p");
  heading.className = role === "user" ? "kb-question" : "kb-answer-head";
  heading.textContent = role === "user" ? question : "引用原文";
  article.append(heading);
  if (role === "assistant") {
    if (!passages.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "文档中没有可引用的段落。";
      article.append(empty);
    }
    for (const passage of passages) {
      const citation = document.createElement("p");
      citation.className = "citation";
      citation.textContent = `${passage.path}:${passage.start_line}-${passage.end_line}`
        + `（相关度 ${Number(passage.score).toFixed(2)}）`;
      const body = document.createElement("pre");
      body.textContent = passage.text;
      article.append(citation, body);
    }
  }
  $("kb-results").append(article);
}

async function askKnowledge(event) {
  event.preventDefault();
  const question = $("kb-question").value.trim();
  if (!question) return;
  const input = $("kb-question");
  input.value = "";
  appendTurn("user", question, []);
  conversation.push({ role: "user", text: question });
  $("kb-button").disabled = true;
  setStatus("kb-status", "查询中…");
  try {
    const answer = await api("/api/knowledge/answer", {
      method: "POST",
      body: JSON.stringify({ question }),
    });
    const passages = answer.found ? answer.passages : [];
    appendTurn("assistant", question, passages);
    conversation.push({ role: "assistant", question, passages });
    setStatus("kb-status", answer.found
      ? `找到 ${passages.length} 段原文。`
      : "文档中没有可引用的段落。", !answer.found);
    input.focus();
  } catch (error) {
    setStatus("kb-status", `查询失败：${error.message}`, true);
  } finally {
    $("kb-button").disabled = false;
  }
}

$("kb-form").addEventListener("submit", askKnowledge);

applyDeployment();
restoreSession();
