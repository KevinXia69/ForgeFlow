import type { CaseFile } from "./types";

function short(value: string | null | undefined, length = 12): string {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function modeLabel(deployment: string): string {
  if (deployment === "review") return "Public review · read-only";
  if (deployment === "research") return "Research evidence view";
  if (deployment === "customer" || deployment === "customer_validation") return "Customer validation · independent scope";
  return "Workbench";
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

function executionLabel(caseFile: CaseFile): string {
  const evidence = executionEvidence(caseFile);
  if (evidence === "live") return "Real sandbox execution · writes and readback confirmed";
  if (evidence === "simulated") return "Simulated execution · no real ERP connection";
  return "Unverified · real sandbox execution not confirmed";
}

/** A summary alone cannot prove a missing receipt, or override a conflicting one. */
function recordedStatus(actual: string | undefined, summary: string | null | undefined): string {
  if (summary !== undefined && summary !== actual) return "Unconfirmed · missing or conflicting records";
  return actual ?? "Not recorded";
}

function verifierStatus(caseFile: CaseFile): string {
  const verifier = caseFile.execution?.verifier;
  const result = caseFile.result;
  if (verifier?.status === "passed" && (verifier.evaluation_valid !== true || verifier.strict_success !== true
    || (result && (result.evaluation_valid !== true || result.strict_success !== true)))) {
    return "Unverified · acceptance evidence is incomplete or conflicting";
  }
  return recordedStatus(verifier?.status, result?.verifier_status);
}

/** English-only Markdown receipt from the public CaseFile projection. */
export function receiptMarkdown(caseFile: CaseFile, _lang: "zh" | "en", deployment = "local"): string {
  const plan = caseFile.plan;
  const approval = caseFile.approval;
  const execution = caseFile.execution;
  const executionStatus = recordedStatus(execution?.status, caseFile.result?.execution_status);
  const simulated = execution?.verifier?.source === "synthetic_executor";
  const original = !simulated && execution?.verifier?.evaluation_scope === "product_original_verifier";
  const verifier = verifierStatus(caseFile);
  const readback = recordedStatus(execution?.readback
    ? execution.readback.execution_validated === true ? "passed" : execution.readback.execution_validated === false ? "failed" : "unknown"
    : undefined, caseFile.result?.readback_status);
  const scope = plan ? `${plan.actions.length} documents · ${caseFile.order.units} units` : "No selected plan";
  return [
    "# ForgeFlow business handling receipt",
    "",
    `> Data provenance: ${caseFile.synthetic ? "Synthetic benchmark data · non-production ERP" : "Data source not labelled"}`,
    `> Page mode: ${modeLabel(deployment)}`,
    `> Execution evidence: ${executionLabel(caseFile)}`,
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
