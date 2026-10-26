import type { CaseFile } from "./types";

type Lang = "zh" | "en";

function short(value: string | null | undefined, length = 12): string {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

/**
 * The server labels the local principal as "Operator <8 hex>". The hex part is
 * an identifier, so the business receipt names the role instead.
 */
function actorName(value: string | null | undefined, lang: Lang): string {
  if (!value) return lang === "zh" ? "未记录" : "not recorded";
  return /^Operator [0-9a-f]{8}$/.test(value.trim())
    ? lang === "zh" ? "本地操作员" : "Local operator"
    : value;
}

function modeLabel(deployment: string, lang: Lang): string {
  if (deployment === "review") return lang === "zh" ? "公开评审 · 只读" : "Public review · read-only";
  if (deployment === "research") return lang === "zh" ? "自主研究证据视图" : "Research evidence view";
  if (deployment === "customer" || deployment === "customer_validation") {
    return lang === "zh" ? "客户验证 · 独立范围" : "Customer validation · independent scope";
  }
  return lang === "zh" ? "工作台" : "Workbench";
}

/** Data provenance and deployment mode do not establish how a case executed. */
function executionEvidence(caseFile: CaseFile): "live" | "simulated" | "unverified" {
  const execution = caseFile.execution;
  const verifier = execution?.verifier;
  const result = caseFile.result;
  const successful = (status: string | null | undefined) =>
    ["succeeded", "success", "applied", "ok", "completed"].includes(status ?? "");
  const completed = caseFile.state === "succeeded" && successful(execution?.status)
    && execution?.readback?.execution_validated === true
    && Array.isArray(execution.writes) && execution.writes.length > 0
    && verifier?.evaluation_valid === true && verifier.strict_success === true
    && verifier.status === "passed"
    && (!result || (successful(result.execution_status) && result.readback_status === "passed" && result.verifier_status === "passed"
      && result.execution_status === execution.status
      && result.evaluation_valid === true && result.strict_success === true));
  if (!completed) return "unverified";
  if (verifier?.source === "synthetic_executor") {
    return !result || result.simulation_passed === true ? "simulated" : "unverified";
  }
  return verifier?.evaluation_scope === "product_original_verifier"
    && (!result || (result.verification_source === "original_verifier" && result.verified === true))
    ? "live" : "unverified";
}

function executionLabel(caseFile: CaseFile, lang: Lang): string {
  const evidence = executionEvidence(caseFile);
  if (evidence === "live") return lang === "zh"
    ? "真实沙箱执行 · 写入与回读已确认" : "Real sandbox execution · writes and readback confirmed";
  if (evidence === "simulated") return lang === "zh"
    ? "模拟执行 · 不连接真实 ERP" : "Simulated execution · no real ERP connection";
  return lang === "zh"
    ? "未验证 · 未确认真实沙箱执行" : "Unverified · real sandbox execution not confirmed";
}

/** A summary alone cannot prove a missing receipt, or override a conflicting one. */
function recordedStatus(actual: string | undefined, summary: string | null | undefined, lang: Lang): string {
  if (summary !== undefined && summary !== actual) {
    return lang === "zh" ? "未确认 · 记录缺失或不一致" : "Unconfirmed · missing or conflicting records";
  }
  return actual ?? (lang === "zh" ? "未记录" : "Not recorded");
}

function verifierStatus(caseFile: CaseFile, lang: Lang): string {
  const verifier = caseFile.execution?.verifier;
  const result = caseFile.result;
  if (verifier?.status === "passed" && (verifier.evaluation_valid !== true || verifier.strict_success !== true
    || (result && (result.evaluation_valid !== true || result.strict_success !== true)))) {
    return lang === "zh" ? "未验证 · 验收通过证据不完整或不一致" : "Unverified · acceptance evidence is incomplete or conflicting";
  }
  return recordedStatus(verifier?.status, result?.verifier_status, lang);
}

/** Pure Markdown receipt from the public CaseFile projection. No owner uid or private path. */
export function receiptMarkdown(caseFile: CaseFile, lang: Lang, deployment = "local"): string {
  const plan = caseFile.plan;
  const approval = caseFile.approval;
  const execution = caseFile.execution;
  const executionStatus = recordedStatus(execution?.status, caseFile.result?.execution_status, lang);
  const simulated = execution?.verifier?.source === "synthetic_executor";
  const original = !simulated && execution?.verifier?.evaluation_scope === "product_original_verifier";
  const verifier = verifierStatus(caseFile, lang);
  const readback = recordedStatus(execution?.readback
    ? execution.readback.execution_validated === true ? "passed" : execution.readback.execution_validated === false ? "failed" : "unknown"
    : undefined, caseFile.result?.readback_status, lang);
  const scope = plan
    ? `${plan.actions.length} ${lang === "zh" ? "张单据" : "documents"} · ${caseFile.order.units} ${lang === "zh" ? "台" : "units"}`
    : lang === "zh" ? "尚无选定方案" : "No selected plan";
  const lines = lang === "zh" ? [
    "# ForgeFlow 业务处理回执",
    "",
    `> 数据属性：${caseFile.synthetic ? "合成基准数据 · 非生产 ERP" : "数据来源未标注"}`,
    `> 页面模式：${modeLabel(deployment, lang)}`,
    `> 执行证据：${executionLabel(caseFile, lang)}`,
    "",
    "## 异常",
    `- 产品：${caseFile.order.product}`,
    `- 影响：${caseFile.order.units} 台，${caseFile.order.ref}`,
    `- 客户：${caseFile.order.customer}`,
    `- 异常：${caseFile.disruption.kind}`,
    `- 案例状态：${caseFile.state}`,
    "",
    "## 选定版本",
    `- 版本：${caseFile.revision?.version ?? 1}`,
    `- 方案标识：${short(plan?.plan_id)}`,
    `- 批准范围：${scope}`,
    "",
    "## 批准与执行",
    `- 批准主体：${actorName(approval?.approver_label, "zh")}`,
    `- 批准时间：${approval?.issued_at ?? "未记录"}`,
    `- 执行状态：${executionStatus}`,
    `- ${simulated ? "模拟回读" : "回读"}：${readback}`,
    `- ${simulated ? "模拟检查" : original ? "原始验收" : "验收"}：${verifier}`,
    "",
    "## 证据索引",
    `- 计划：${short(plan?.plan_id, 20)}`,
    `- 快照：${short(plan?.snapshot_sha256, 20)}`,
    `- 政策：${short(plan?.policy_sha256, 20)}`,
    `- 案例更新时间：${caseFile.updated_at}`,
    "",
    "## 未记录 / 未验证",
    ...(caseFile.synthetic ? ["- 使用合成基准数据，不代表客户生产业务；是否执行以本回执中的执行证据为准。"] : []),
    `- 成本、实际到货、运输或交付等未记录项，以案例页面为准。`,
    "",
    "> 此回执由 ForgeFlow 公开案例投影生成；ID 仅用于核对，不包含 owner、密钥或私有路径。",
  ] : [
    "# ForgeFlow business handling receipt",
    "",
    `> Data provenance: ${caseFile.synthetic ? "Synthetic benchmark data · non-production ERP" : "Data source not labelled"}`,
    `> Page mode: ${modeLabel(deployment, lang)}`,
    `> Execution evidence: ${executionLabel(caseFile, lang)}`,
    "",
    "## Exception",
    `- Product: ${caseFile.order.product}`,
    `- Impact: ${caseFile.order.units} units, ${caseFile.order.ref}`,
    `- Customer: ${caseFile.order.customer}`,
    `- Disruption: ${caseFile.disruption.kind}`,
    `- Case state: ${caseFile.state}`,
    "",
    "## Selected version",
    `- Version: ${caseFile.revision?.version ?? 1}`,
    `- Plan: ${short(plan?.plan_id)}`,
    `- Approval scope: ${scope}`,
    "",
    "## Approval and execution",
    `- Approver: ${actorName(approval?.approver_label, "en")}`,
    `- Approved at: ${approval?.issued_at ?? "not recorded"}`,
    `- Execution: ${executionStatus}`,
    `- ${simulated ? "Simulated readback" : "Readback"}: ${readback}`,
    `- ${simulated ? "Simulated checks" : original ? "Original verification" : "Verification"}: ${verifier}`,
    "",
    "## Evidence index",
    `- Plan: ${short(plan?.plan_id, 20)}`,
    `- Snapshot: ${short(plan?.snapshot_sha256, 20)}`,
    `- Policy: ${short(plan?.policy_sha256, 20)}`,
    `- Case updated: ${caseFile.updated_at}`,
    "",
    "## Not recorded / not verified",
    ...(caseFile.synthetic ? ["- Synthetic benchmark data is not customer production business; use the execution evidence above to assess what ran."] : []),
    "- Cost, actual receipt, transport or delivery unknowns remain as shown on the case page.",
    "",
    "> Generated from ForgeFlow's public case projection. IDs are for checking only; no owner, secret or private path is included.",
  ];
  return lines.join("\n");
}

export function receiptFilename(caseFile: CaseFile): string {
  return `forgeflow-receipt-${caseFile.case_id.slice(0, 8)}.md`;
}

export function downloadReceipt(caseFile: CaseFile, lang: Lang, deployment = "local"): void {
  const blob = new Blob([receiptMarkdown(caseFile, lang, deployment)], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = receiptFilename(caseFile);
  document.body.append(anchor); anchor.click(); anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
