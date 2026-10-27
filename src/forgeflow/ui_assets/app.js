const byId = (id) => document.getElementById(id);
let snapshot = null;
let selected = null;
let filter = "all";
let dataFilter = "all";
let refreshing = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function measured(value, suffix = "") {
  return value === null || value === undefined ? "未测量" : `${value}${suffix}`;
}
function booleanLabel(value) {
  return value === true ? "通过" : value === false ? "未通过" : "尚未验收";
}
function timeLabel(value) {
  if (!value) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  return date.toLocaleString("zh-CN", {timeZone: "America/Los_Angeles", hour12: false});
}
function badge(text, kind = "neutral") {
  return element("span", `badge ${kind}`, text);
}
function status(record) {
  if (record.state === "running" || record.state === "preflight") return ["运行中", "pending"];
  if (record.state === "interrupted") return ["已中断", "failure"];
  if (record.state === "failed" || record.evaluation_valid === false) return ["运行失败", "failure"];
  if (record.category === "environment_control" && record.expected_control_outcome === true) return ["符合预期", "success"];
  if (record.strict_success === true) return ["严格通过", "success"];
  if (record.strict_success === false) return ["未通过", "failure"];
  return ["尚未验收", "neutral"];
}
function agentLabel(agent) {
  return {pi: "Pi 评测", oracle: "官方参考", nop: "空操作"}[agent] || "未分类";
}
function dataLabel(record) {
  return record.data_profile_label || "数据类型未记录";
}
function guidanceLabel(record) {
  const guidance = record.execution_guidance || {};
  if (guidance.profile === "none") return "提示：未追加";
  return `提示：${guidance.profile || "未记录"}`;
}
function outputBudgetLabel(record) {
  if (record.max_output_tokens_source === "provider_default") return "服务端默认（数值未返回）";
  return record.max_output_tokens == null ? "未记录" : `${record.max_output_tokens} tokens`;
}
function outputPolicyLabel(record) {
  if (record.output_policy_version === "all-requests-v2") return "all-requests-v2（普通请求及上下文压缩）";
  if (record.output_policy_version === "explicit") return "explicit（显式输出预算）";
  return "未记录";
}
function thinkingModeLabel(record) {
  return {"provider-default": "服务端默认", off: "关闭思考（独立实验）"}[record.thinking_mode] || "未记录";
}
function fact(label, value) {
  byId("detail-facts").append(element("dt", "", label), element("dd", "", value));
}
function visibleRecords() {
  return snapshot.runs.filter((record) =>
    (filter === "all" || record.category === filter) &&
    (dataFilter === "all" || record.data_profile === dataFilter));
}
function clearDetail() {
  selected = null;
  byId("detail").hidden = true;
  byId("detail-empty").hidden = false;
  byId("detail-badge").textContent = "未选择";
  byId("detail-badge").className = "badge neutral";
}
function renderRows() {
  const records = visibleRecords();
  byId("run-rows").replaceChildren();
  byId("visible-count").textContent = `${records.length} 条记录`;
  byId("empty-runs").hidden = records.length > 0;
  byId("empty-title").textContent = snapshot.runs.length ? "此分类暂无记录" : "尚无 ERP 任务记录";
  byId("empty-description").textContent = snapshot.runs.length ? "切换分类查看已有运行。新完成的记录会自动显示。" : "完成一次官方参考执行、空操作或 Pi 评测后，记录会自动显示在这里。";
  for (const record of records) {
    const row = element("tr", `run-row${selected === record.run_id ? " selected" : ""}`);
    row.tabIndex = 0;
    row.setAttribute("aria-label", `${record.task_label}，${dataLabel(record)}，${agentLabel(record.agent)}，${status(record)[0]}`);
    const taskCell = element("td");
    taskCell.append(element("div", "task-name", record.task_label), element("div", "run-name", record.run_id));
    taskCell.append(element("div", `profile-tag${record.data_profile === "planning-metadata-v1" ? " variant" : ""}`, dataLabel(record)));
    const typeCell = element("td", "", agentLabel(record.agent));
    if (record.agent === "pi") {
      typeCell.append(element("div", "configuration-note", guidanceLabel(record)));
      if (record.planner_profile !== "none") typeCell.append(element("div", "configuration-note", `工具：${record.planner_label || record.planner_profile || "未记录"}`));
    }
    const statusCell = element("td");
    statusCell.append(badge(...status(record)));
    row.append(taskCell, typeCell, statusCell, element("td", "numeric", measured(record.score)));
    row.addEventListener("click", () => selectRun(record.run_id));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectRun(record.run_id);
      }
    });
    byId("run-rows").append(row);
  }
}
async function selectRun(runId) {
  selected = runId;
  renderRows();
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {cache: "no-store"});
    if (!response.ok) throw new Error("这条运行记录暂时无法读取，请稍后刷新。");
    const record = await response.json();
    if (selected !== runId) return;
    byId("detail-empty").hidden = true;
    byId("detail").hidden = false;
    byId("detail-task").textContent = `${record.task_label} · ${agentLabel(record.agent)}`;
    byId("detail-id").textContent = record.run_id;
    const [label, kind] = status(record);
    byId("detail-badge").textContent = label;
    byId("detail-badge").className = `badge ${kind}`;
    byId("detail-facts").replaceChildren();
    fact("运行类别", record.category === "agent_evaluation" ? `${dataLabel(record)} · 自主评测` : record.category === "environment_control" ? `${dataLabel(record)} · 环境对照` : "未分类");
    fact("数据类型", `${record.data_profile || "未记录"}${record.data_profile_recorded === false ? "（历史默认）" : ""}`);
    byId("detail-experiment-note").textContent = record.data_profile === "planning-metadata-v1"
      ? "本试次公开既有规划参数，属于数据可见性变体。分数与原始任务分开统计，不据此宣称同条件改善。"
      : record.data_profile === "original"
        ? "使用原始任务数据。比较试次时，还需核对执行提示、输出预算及模型等配置。"
        : "数据类型无法确认，单独展示，不并入原始任务或规划参数可见性变体。";
    fact("原始分数", record.score === null ? "未测量" : `${record.score} / 100`);
    fact("严格任务验收", booleanLabel(record.strict_success));
    fact("验收链路", booleanLabel(record.evaluation_valid));
    if (record.category === "environment_control") fact("对照预期", record.expected_control_outcome === true ? "符合预期" : record.expected_control_outcome === false ? "不符合预期" : "尚未确认");
    fact("约束失败数", measured(record.failed_rule_count));
    fact("模型", record.model || (record.category === "agent_evaluation" ? "未记录" : "此对照不调用模型"));
    fact("开始时间", timeLabel(record.started_at));
    fact("运行耗时", measured(record.elapsed_seconds, " 秒"));
    fact("Agent 时间预算", measured(record.budget_seconds, " 秒"));
    if (record.agent === "pi") {
      const guidance = record.execution_guidance || {};
      fact("执行提示", `${guidance.profile || "未记录"}${guidance.recorded === false ? "（历史默认）" : ""}`);
      fact("求解工具配置", `${record.planner_profile || "未记录"}${record.planner_recorded === false ? "（历史默认）" : ""}`);
      fact("求解工具入口", record.planner_label || "未记录");
      if (record.planner_profile && !["none", "unknown"].includes(record.planner_profile)) fact("求解工具证据", "已配置；实际调用以工具轨迹为准");
      if (guidance.profile && !["none", "unknown"].includes(guidance.profile)) {
        fact("提示版本", guidance.version === null ? "未记录" : `v${guidance.version}`);
        fact("提示 SHA-256", guidance.sha256 || "未记录");
      }
      const outputBudget = outputBudgetLabel(record);
      fact("单响应输出预算", record.max_output_tokens_source === "provider_default" || record.max_output_tokens == null ? outputBudget : `${outputBudget}（推理与回答共享）`);
      fact("输出策略版本", outputPolicyLabel(record));
      fact("思考模式", thinkingModeLabel(record));
      fact("上下文预算", record.context_window_tokens == null ? "未记录" : `${record.context_window_tokens} tokens`);
      const sampling = record.sampling_params || {};
      fact("采样参数", sampling.temperature === null && sampling.top_p === null ? "未记录" : `temperature ${sampling.temperature ?? "未记录"} / top_p ${sampling.top_p ?? "未记录"}`);
    }
    fact("输入 / 输出 tokens", `${measured(record.metrics.n_input_tokens)} / ${measured(record.metrics.n_output_tokens)}`);
    const usageAccounting = record.metrics.usage_accounting || {};
    if (record.agent === "pi") {
      fact("用量记录", ({complete: "普通调用与压缩用量已完整记录", incomplete: "记录不完整；以上为已记录小计"})[usageAccounting.status] || "完整性未知；旧记录可能未包含压缩用量");
    }
    fact("模型成本", record.metrics.cost_usd === null ? "未测量" : `$${record.metrics.cost_usd}${usageAccounting.cost_status === "complete" ? "" : "（已记录金额，完整性未确认）"}`);
    if (record.error_type) fact("失败类型", record.error_type);
    if (record.issue_count) fact("证据校验问题", `${record.issue_count} 项`);
    const trace = record.trace || {status: "unavailable", steps: []};
    byId("trace-section").hidden = record.agent !== "pi";
    const finalResponse = trace.final_response;
    const responseLabels = {
      length: "达到单次输出上限，响应未完成",
      error: "响应出错，未完成",
      aborted: "响应已中止",
      stop: "响应正常结束",
      toolUse: "响应停在工具调用",
      pending: "响应尚未结束",
      deferred: "响应已延后",
    };
    const reason = finalResponse?.stop_reason;
    byId("response-status").textContent = reason && responseLabels[reason] ? `${responseLabels[reason]}（${reason}）` : "未知：未记录可确认的最终响应状态。";
    byId("response-status").className = `small${["length", "error", "aborted"].includes(reason) ? " response-incomplete" : ""}`;
    const usage = finalResponse?.usage || {};
    byId("response-usage").textContent = `最后一次响应 tokens · 输入 ${measured(usage.input)} / 输出 ${measured(usage.output)} / 推理 ${measured(usage.reasoning)} / 缓存读取 ${measured(usage.cacheRead)}`;
    byId("trace-steps").replaceChildren();
    byId("trace-count").textContent = trace.steps.length ? `${trace.steps.length}${trace.truncated ? "+" : ""} 次` : "";
    const traceNotes = {
      waiting: "等待运行结束后导出并脱敏工具轨迹。",
      unavailable: "该运行没有可用的 Pi 工具轨迹文件，未生成回放。",
      no_tool_events: "已读取轨迹，但未发现可解析的工具开始或结束事件。",
      available: "按实际工具开始顺序展示。工具执行完成不等于业务验收通过。逐工具耗时未记录。",
    };
    byId("trace-note").textContent = (traceNotes[trace.status] || "") + (trace.truncated ? " 已显示有限数量的记录。" : "") + (trace.invalid_line_count ? ` ${trace.invalid_line_count} 行无法解析，未作为工具操作展示。` : "");
    for (const step of trace.steps) {
      const item = element("li", "trace-step");
      const heading = element("div", "trace-heading");
      const state = {succeeded: ["完成", "success"], failed: ["失败", "failure"], incomplete: ["未记录结束", "pending"], unknown: ["状态未知", "neutral"]}[step.status] || ["状态未知", "neutral"];
      heading.append(element("span", "mono", `${step.sequence}. ${step.tool}`), badge(...state));
      item.append(heading, element("p", "trace-summary", step.argument_summary));
      if (step.result_summary) item.append(element("p", "trace-result", step.result_summary));
      if (!step.start_recorded) item.append(element("p", "muted small", "此工具缺少开始事件。"));
      byId("trace-steps").append(item);
    }
    byId("rules").replaceChildren();
    byId("rule-count").textContent = record.rules.length ? `${record.rules.length}${record.rules_truncated ? "+" : ""} 项` : "";
    if (!record.rules.length) byId("rules").append(element("p", "muted small", "尚无可用规则明细；不据此推断任务通过。"));
    for (const rule of record.rules) {
      const row = element("div", "rule");
      const ruleText = element("div");
      ruleText.append(element("span", "rule-group", rule.dimension || "未分类"), element("code", "", rule.rule || "未记录规则名称"));
      row.append(badge(rule.status, rule.status === "PASS" ? "success" : rule.status === "FAIL" ? "failure" : "neutral"), ruleText);
      byId("rules").append(row);
    }
    byId("evidence").replaceChildren();
    for (const path of Object.values(record.evidence)) {
      if (path) byId("evidence").append(element("li", "", path));
    }
    if (trace.evidence) byId("evidence").append(element("li", "", trace.evidence));
  } catch (error) {
    if (selected === runId) clearDetail();
    byId("error").hidden = false;
    byId("error").textContent = error.message;
  }
}
function renderGroups() {
  const rows = byId("agent-group-rows");
  rows.replaceChildren();
  const groups = snapshot.agent_groups || [];
  byId("empty-groups").hidden = groups.length > 0;
  for (const group of groups) {
    const row = element("tr");
    const task = element("td", "", group.task_label);
    task.append(element("div", "configuration-note", group.model || "模型未记录"));
    const data = element("td");
    data.append(element("div", `profile-tag${group.data_profile === "planning-metadata-v1" ? " variant" : ""}`, dataLabel(group)), element("div", "configuration-note mono", group.data_profile));
    const guidance = element("td");
    guidance.append(element("div", "", guidanceLabel(group)));
    const profile = group.execution_guidance || {};
    if (profile.version != null) guidance.append(element("div", "configuration-note", `v${profile.version} · SHA ${profile.sha256 ? profile.sha256.slice(0, 12) : "未记录"}`));
    guidance.append(element("div", "configuration-note", `求解工具：${group.planner_label || group.planner_profile || "未记录"}`));
    const budget = element("td", "", outputBudgetLabel(group));
    budget.append(element("div", "configuration-note", `输出策略：${outputPolicyLabel(group)}`));
    budget.append(element("div", "configuration-note", `思考模式：${thinkingModeLabel(group)}`));
    const sampling = group.sampling_params || {};
    budget.append(element("div", "configuration-note", `上下文 ${measured(group.context_window_tokens)} · 时间 ${measured(group.budget_seconds, " 秒")}`), element("div", "configuration-note", `temperature ${measured(sampling.temperature)} · top_p ${measured(sampling.top_p)}`));
    row.append(task, data, guidance, budget, element("td", "numeric", `${group.strict_agent_successes} / ${group.measured_agent_runs}`));
    rows.append(row);
  }
}
function renderProbes() {
  byId("probe-rows").replaceChildren();
  if (!snapshot.probes.length) {
    byId("probe-rows").append(element("div", "probe-row muted", "尚无模型接口检查记录。"));
    return;
  }
  for (const probe of snapshot.probes.slice(0, 8)) {
    const row = element("div", "probe-row");
    const label = {passed: "通过", failed: "失败", interrupted: "已中断"}[probe.status] || "未知";
    row.append(badge(label, probe.status === "passed" ? "success" : "failure"), element("span", "probe-model", probe.model || "模型未配置"), element("span", "probe-note", `工具执行 ${measured(probe.tool_calls_executed)} 次`), element("span", "probe-time", timeLabel(probe.started_at)));
    byId("probe-rows").append(row);
  }
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  byId("refresh").disabled = true;
  try {
    const response = await fetch("/api/runs", {cache: "no-store"});
    if (!response.ok) throw new Error("本地结果暂时无法读取，请确认查看器仍在运行。");
    snapshot = await response.json();
    byId("run-count").textContent = snapshot.counts.erp_runs;
    byId("agent-count").textContent = snapshot.counts.measured_agent_runs;
    byId("control-count").textContent = snapshot.counts.environment_controls ? `${snapshot.counts.expected_environment_controls} / ${snapshot.counts.environment_controls}` : "未测量";
    byId("probe-status").textContent = snapshot.probes.length ? ({passed: "通过", failed: "失败", interrupted: "已中断"}[snapshot.probes[0].status] || "未知") : "未运行";
    byId("updated").textContent = `更新于 ${timeLabel(snapshot.generated_at)}`;
    byId("error").hidden = true;
    renderRows();
    renderGroups();
    renderProbes();
    const visible = visibleRecords();
    if (selected && visible.some((record) => record.run_id === selected)) await selectRun(selected);
    else if (visible.length) await selectRun(visible[0].run_id);
    else clearDetail();
  } catch (error) {
    byId("error").hidden = false;
    byId("error").textContent = error.message;
  } finally {
    refreshing = false;
    byId("refresh").disabled = false;
  }
}
document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button));
    if (snapshot) {
      const visible = visibleRecords();
      if (!visible.some((record) => record.run_id === selected)) {
        clearDetail();
        if (visible.length) selectRun(visible[0].run_id);
      }
      renderRows();
    }
  });
});
byId("data-profile-filter").addEventListener("change", (event) => {
  dataFilter = event.target.value;
  if (snapshot) {
    const visible = visibleRecords();
    if (!visible.some((record) => record.run_id === selected)) {
      clearDetail();
      if (visible.length) selectRun(visible[0].run_id);
    }
    renderRows();
  }
});
byId("refresh").addEventListener("click", refresh);
refresh();
setInterval(refresh, 5000);
