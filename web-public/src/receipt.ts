import type { CaseFile } from "./types";

function short(value: string | null | undefined, length = 12): string {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function modeLabel(caseFile: CaseFile, deployment: string): string {
  if (deployment === "review") return "Public review · read-only";
  if (deployment === "research") return "Research evidence · no execution";
  if (deployment === "customer" || deployment === "customer_validation") return "Customer validation · independent scope";
  if (caseFile.synthetic) return "Demo / synthetic · no real ERP write";
  return "Local workbench";
}

/** English-only Markdown receipt from the public CaseFile projection. */
export function receiptMarkdown(caseFile: CaseFile, _lang: "zh" | "en", deployment = "local"): string {
  const plan = caseFile.plan;
  const approval = caseFile.approval;
  const execution = caseFile.execution;
  const executionStatus = caseFile.result?.execution_status ?? execution?.status ?? "Not executed";
  const verifier = caseFile.result?.verifier_status ?? execution?.verifier?.status ?? "Not verified";
  const readback = caseFile.result?.readback_status
    ?? (execution?.readback ? `${execution.readback.records?.length ?? 0} records · ${execution.readback.verified_steps ?? 0} verified steps` : "Not recorded");
  const scope = plan ? `${plan.actions.length} documents · ${caseFile.order.units} units` : "No selected plan";
  return [
    "# ForgeFlow business handling receipt",
    "",
    `> Data mode: ${modeLabel(caseFile, deployment)}`,
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
    `- Approver: ${approval?.approver_label ?? "not recorded"}`,
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
  ].join("\n");
}

export function receiptFilename(caseFile: CaseFile): string {
  return `forgeflow-receipt-${caseFile.case_id.slice(0, 8)}.md`;
}

export function downloadReceipt(caseFile: CaseFile, lang: "zh" | "en", deployment = "local"): void {
  const blob = new Blob([receiptMarkdown(caseFile, lang, deployment)], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = receiptFilename(caseFile);
  document.body.append(anchor); anchor.click(); anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
