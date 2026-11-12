import { recordedApproval, hasNewSpendBasis, currentDecisionMetrics } from "./decisionSummary";
import { budgetCheckBasis } from "./caseUi";
import { disruptionScope } from "./impactFacts";
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

function selectionMatches(caseFile: CaseFile): boolean {
  const id = caseFile.plan?.plan_id;
  return !!id && /^[a-f0-9]{64}$/.test(id)
    && (caseFile.approval?.plan_id === undefined || caseFile.approval.plan_id === id)
    && (caseFile.comparison?.status !== "ready" || caseFile.comparison.selected_plan_id === id);
}

function consistentVerdict(caseFile: CaseFile): boolean {
  const verifier = caseFile.execution?.verifier;
  const summary = caseFile.result;
  return verifier?.evaluation_valid === true
    && (verifier.status === "passed" && verifier.strict_success === true
      || verifier.status === "failed" && verifier.strict_success === false)
    && !(verifier.strict_success === true && (verifier.original_passed === false
      || (verifier.failed_rule_count ?? 0) > 0 || verifier.checks?.some(check => check.passed === false)))
    && (!summary || (summary.verifier_status === verifier.status && summary.evaluation_valid === true
      && summary.strict_success === verifier.strict_success));
}

/** Record exactly the selected plan's preparation checks, independent of the later ERP outcome. */
function planChecks(caseFile: CaseFile, unknown: string): string {
  const plan = caseFile.plan;
  if (!selectionMatches(caseFile) || !Array.isArray(plan?.checks) || !plan.checks.length) return unknown;
  return ["margin", "budget", "demand", "on_time", "policy"].map(code => {
    const rows = plan.checks!.filter(check => check?.code === code);
    const check = rows[0];
    const known = rows.length === 1 && ["pass", "warn", "block", "planned"].includes(check.status)
      && (code !== "budget" || budgetCheckBasis(check, plan) === "per_order");
    return code + "=" + (known ? check.status : unknown);
  }).join(", ");
}

function originalDetails(caseFile: CaseFile, unknown: string): string {
  const verifier = caseFile.execution?.verifier;
  if (!consistentVerdict(caseFile) || !verifier
    || caseFile.result && caseFile.result.verification_source !== "original_verifier") return unknown;
  const { passed_rule_count: passed, applicable_rule_count: total, failed_rule_count: failed, score } = verifier;
  if (typeof passed !== "number" || !Number.isSafeInteger(passed) || passed < 0
    || typeof total !== "number" || !Number.isSafeInteger(total) || total < 1 || passed > total
    || failed !== total - passed || verifier.strict_success === true && passed !== total
    || typeof score !== "number" || !Number.isFinite(score) || score < 0 || score > 100) return unknown;
  return `${passed}/${total} · ${score}/100`;
}

/** An independently scored verifier can fail after successful writes and readback. */
function executionEvidence(caseFile: CaseFile): "live" | "simulated" | "unverified" {
  const execution = caseFile.execution;
  const verifier = execution?.verifier;
  const result = caseFile.result;
  if (caseFile.approval && !recordedApproval(caseFile)) return "unverified";
  if (caseFile.state !== "succeeded" || !execution
    || !["succeeded", "success", "applied", "ok", "completed"].includes(execution.status)
    || execution.readback?.execution_validated !== true
    || !Array.isArray(execution.writes) || !execution.writes.length || !selectionMatches(caseFile)
    || !consistentVerdict(caseFile) || !verifier
    || result && (result.execution_status !== execution.status || result.readback_status !== "passed")) return "unverified";
  if (verifier.source === "synthetic_executor") {
    return verifier.strict_success && (!result || (result.simulation_passed === true && !result.verified
      && result.verification_source === "synthetic_executor")) ? "simulated" : "unverified";
  }
  if (verifier.evaluation_scope !== "product_original_verifier"
    || result && (result.verification_source !== "original_verifier" || result.verified !== verifier.strict_success
      || result.simulation_passed === true)) return "unverified";
  if (verifier.strict_success === true) return "live";
  const readback = execution.readback;
  return caseFile.approval?.plan_id === caseFile.plan?.plan_id
    && /^[a-f0-9]{64}$/.test(readback.readback_sha256 ?? "")
    && /^[a-f0-9]{64}$/.test(readback.receipt_sha256 ?? "")
    && Array.isArray(readback.records) && readback.records.length > 0
    && typeof readback.verified_steps === "number" && Number.isSafeInteger(readback.verified_steps)
    && readback.verified_steps > 0 ? "live" : "unverified";
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
  if ((verifier?.status === "passed" || verifier?.status === "failed") && !consistentVerdict(caseFile)) {
    return "Unverified · acceptance evidence is incomplete or conflicting";
  }
  return recordedStatus(verifier?.status, result?.verifier_status);
}

/** English-only Markdown receipt from the public CaseFile projection. */
export function receiptMarkdown(caseFile: CaseFile, _lang: "zh" | "en", deployment = "local"): string {
  const plan = caseFile.plan;
  const approval = recordedApproval(caseFile);
  const metrics = currentDecisionMetrics(plan), impact = disruptionScope(plan), unknown = "unknown";
  const margin = metrics?.margin.new_spend_revenue === "0" && metrics.margin.planned_cost === "0"
    ? "not applicable (all stock and zero planned cost)"
    : typeof plan?.margin === "number" && Number.isFinite(plan.margin) ? `${plan.margin.toFixed(2)}%` : unknown;
  const objectives = { preserve_quantities: "Minimize path quantity deviations, then cost",
    min_planned_cost: "Minimize planned cost; changes the original preservation priority",
    preserve_capacity: "Minimize standard manufacturing minutes; changes the original preservation priority" };
  const objective = plan?.objective ? objectives[plan.objective] : unknown;
  const retention = plan?.retention_refinement;
  const retentionNames = { improved: "improved", unchanged: "unchanged", fallback: "incomplete; original plan retained", skipped: "skipped" };
  const retentionLine = retention?.profile === "fixed-totals-retention-v1" && retention.status in retentionNames
    ? `- Optional retention experiment: ${retentionNames[retention.status]}; ${retention.retained_before} → ${retention.retained_after} retained units. Original repair/cost optima and totals are fixed; no global minimum ERP-write claim.` : null;

  const impactLines = impact ? [
    ...impact.demands.map(row => `- Recorded directly linked demand: ${row.orders} orders, ${row.units ?? unknown} units of ${row.product ?? unknown}`),
    ...impact.supplies.map(row => `- Identified failed supply: ${row.documents} documents, ${row.units ?? unknown} units of ${row.product ?? unknown}`),
  ] : ["- Directly linked demand and failed supply: unknown; total planning scope is not a substitute."];
  const marginLines = hasNewSpendBasis(plan) ? [
    `- New-spend margin: ${margin}`,
    `- Purchase/manufacturing coverage revenue: ${metrics?.margin.new_spend_revenue ?? unknown} USD; finished-stock coverage: ${metrics?.margin.stock_units ?? unknown} units`,
    `- Effective floor: ${metrics?.margin.effective_floor_pct ?? unknown}%; cost headroom: ${metrics?.margin.cost_headroom ?? unknown} USD`,
  ] : [`- Recorded margin (legacy basis): ${margin}; not rechecked against new-spend coverage revenue and cannot establish current floor compliance.`];
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
    `- Total planning scope: ${caseFile.order.units} units, ${caseFile.order.ref}`,
    ...impactLines,
    `- Customer: ${caseFile.order.customer}`,
    `- Disruption: ${caseFile.disruption.kind}`,
    `- Case state: ${caseFile.state}`,
    "",
    "## Selected version",
    `- Version: ${caseFile.revision?.version ?? 1}`,
    `- Plan: ${short(plan?.plan_id)}`,
    `- Approval scope: ${scope}`,
    `- Selected objective: ${objective}`,
    `- Registered scenario plan checks: ${planChecks(caseFile, unknown)} (at preparation; post-execution business rows require a separate check)`,
    ...marginLines,
    ...(retentionLine ? [retentionLine] : []),
    `- Planned purchase and manufacturing cost: ${plan?.costs?.planned.total ?? plan?.new_spend ?? unknown} USD (includes retained documents; not a cost delta)`,
    simulated ? "- Simulated checks do not establish task acceptance or a real ERP outcome." : "- Original verification still scores the repair objective; meeting business constraints does not guarantee full marks.",
    "",
    "## Approval and execution",
    `- Approver: ${approval?.approver_label ?? "not recorded"}`,
    `- Approved at: ${approval?.issued_at ?? "not recorded"}`,
    `- Execution: ${executionStatus}`,
    `- Business document actions: ${execution?.writes?.length ?? unknown}; verified execution steps: ${execution?.readback?.verified_steps ?? unknown} (neither is a database write count)`,
    `- ${simulated ? "Simulated readback" : "Readback"}: ${readback}`,
    `- ${simulated ? "Simulated checks" : original ? "Original verification" : "Verification"}: ${verifier}`,
    ...(original ? [`- Original verifier details: ${originalDetails(caseFile, unknown)} (passed/applicable rules · score)`] : []),
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
