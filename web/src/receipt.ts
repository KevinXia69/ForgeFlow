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

function modeLabel(caseFile: CaseFile, deployment: string, lang: Lang): string {
  if (deployment === "review") return lang === "zh" ? "公开评审 · 只读" : "Public review · read-only";
  if (deployment === "research") return lang === "zh" ? "自主研究证据 · 不执行" : "Research evidence · no execution";
  if (deployment === "customer" || deployment === "customer_validation") {
    return lang === "zh" ? "客户验证 · 独立范围" : "Customer validation · independent scope";
  }
  if (caseFile.synthetic) return lang === "zh" ? "演示 / 合成数据 · 不写真实 ERP" : "Demo / synthetic · no real ERP write";
  return lang === "zh" ? "本地工作台" : "Local workbench";
}

/** Pure Markdown receipt from the public CaseFile projection. No owner uid or private path. */
export function receiptMarkdown(caseFile: CaseFile, lang: Lang, deployment = "local"): string {
  const plan = caseFile.plan;
  const approval = caseFile.approval;
  const execution = caseFile.execution;
  const executionStatus = caseFile.result?.execution_status ?? execution?.status ?? (lang === "zh" ? "未执行" : "Not executed");
  const verifier = caseFile.result?.verifier_status ?? execution?.verifier?.status ?? (lang === "zh" ? "未验证" : "Not verified");
  const readback = caseFile.result?.readback_status
    ?? (execution?.readback ? `${execution.readback.records?.length ?? 0} ${lang === "zh" ? "条记录" : "records"} · ${execution.readback.verified_steps ?? 0} ${lang === "zh" ? "步校验" : "verified steps"}` : (lang === "zh" ? "未记录" : "Not recorded"));
  const scope = plan
    ? `${plan.actions.length} ${lang === "zh" ? "张单据" : "documents"} · ${caseFile.order.units} ${lang === "zh" ? "台" : "units"}`
    : lang === "zh" ? "尚无选定方案" : "No selected plan";
  const lines = lang === "zh" ? [
    "# ForgeFlow 业务处理回执",
    "",
    `> 数据模式：${modeLabel(caseFile, deployment, lang)}`,
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
    `- 回读：${readback}`,
    `- 原始验收：${verifier}`,
    "",
    "## 证据索引",
    `- 计划：${short(plan?.plan_id, 20)}`,
    `- 快照：${short(plan?.snapshot_sha256, 20)}`,
    `- 政策：${short(plan?.policy_sha256, 20)}`,
    `- 案例更新时间：${caseFile.updated_at}`,
    "",
    "## 未记录 / 未验证",
    ...(caseFile.synthetic ? ["- 演示或合成数据；不代表真实 ERP 执行。"] : []),
    `- 成本、实际到货、运输或交付等未记录项，以案例页面为准。`,
    "",
    "> 此回执由 ForgeFlow 公开案例投影生成；ID 仅用于核对，不包含 owner、密钥或私有路径。",
  ] : [
    "# ForgeFlow business handling receipt",
    "",
    `> Data mode: ${modeLabel(caseFile, deployment, lang)}`,
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
    `- Readback: ${readback}`,
    `- Original verification: ${verifier}`,
    "",
    "## Evidence index",
    `- Plan: ${short(plan?.plan_id, 20)}`,
    `- Snapshot: ${short(plan?.snapshot_sha256, 20)}`,
    `- Policy: ${short(plan?.policy_sha256, 20)}`,
    `- Case updated: ${caseFile.updated_at}`,
    "",
    "## Not recorded / not verified",
    ...(caseFile.synthetic ? ["- Demo or synthetic data; not a real ERP execution."] : []),
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
