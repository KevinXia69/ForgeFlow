import { planningAttention, decisionFactLines, hasNewSpendBasis, marginLabel, marginValue } from "./decisionSummary";
import { impactAnswer, impactIntent, disruptionScopeLines } from "./impactFacts";
import { recordedDueRange } from "./orderReadinessDates";
import { originalScoreThresholdMiss } from "./verificationExplanation";
import { ApiError } from "./api";
import { costMoney } from "./costAmounts";
import { environmentAccessNotice } from "./environmentAccess";
import type { Lang, MessageKey, Translate } from "./i18n";
import type {
  ActivityState,
  Capabilities,
  CaseFile,
  ProductAction,
  ProductPlan,
  ProductPlanCheck,
  ProductVerifierCheck,
} from "./types";

export type CaseMode = "loading" | "live" | "fixture";
export type CaseTone = "neutral" | "amber" | "green" | "red" | "demo";
export type Severity = "high" | "medium" | "low" | "unknown";
export type StepState = "done" | "active" | "failed" | "pending";

export const STATE_LABELS: Record<string, MessageKey> = {
  loading: "case.state.loading",
  demo: "case.state.demo",
  ready: "case.state.ready",
  preparing: "case.state.preparing",
  revision_requested: "v10.revisionPendingTitle",
  awaiting_approval: "case.state.awaiting",
  awaiting_selection: "candidate.awaiting",
  executing: "case.state.executing",
  execution_unknown: "v9.executionUnknown",
  succeeded: "case.state.succeeded",
  failed: "case.state.failed",
  rejected: "case.state.rejected",
  expired: "case.state.expired",
};

export const SUCCESS_STATES = new Set(["succeeded", "success", "applied", "ok"]);
const SUCCESS_EXECUTION_STATUSES = new Set([
  "succeeded",
  "success",
  "applied",
  "ok",
  "completed",
]);
const TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "rejected",
  "expired",
]);

export function stateLabel(state: string, t: Translate): string {
  const key = STATE_LABELS[state];
  return key ? t(key) : state;
}

export function errorDetail(error: unknown): string {
  if (error instanceof ApiError) return error.message || `HTTP ${error.status}`;
  if (error instanceof Error && error.message) return error.message;
  return "unknown";
}

export function isUnavailable(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.status === 405 || error.status === 501 || error.status === 503)
  );
}

export function stateTone(state: string): CaseTone {
  switch (state) {
    case "demo":
      return "demo";
    case "preparing":
    case "awaiting_approval":
    case "awaiting_selection":
    case "executing":
      return "amber";
    case "succeeded":
      return "green";
    case "failed":
    case "rejected":
    case "expired":
      return "red";
    default:
      return "neutral";
  }
}

export function isTerminal(state: string): boolean {
  return TERMINAL_STATES.has(state);
}

export function isExecutionSuccess(
  status: string | null | undefined,
): boolean {
  return status ? SUCCESS_EXECUTION_STATUSES.has(status) : false;
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

export function formatDateTime(
  value: string | null | undefined,
  lang: Lang,
): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(lang === "zh" ? "zh-CN" : "en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatAge(
  createdAt: string | null | undefined,
  t: Translate,
  now = Date.now(),
): string {
  if (!createdAt) return "—";
  const created = Date.parse(createdAt);
  if (Number.isNaN(created)) return "—";
  const minutes = Math.max(0, Math.floor((now - created) / 60_000));
  if (minutes < 1) return t("cases.ageJustNow");
  if (minutes < 60) return t("cases.ageMinutes", { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("cases.ageHours", { count: hours });
  return t("cases.ageDays", { count: Math.floor(hours / 24) });
}

export function formatCurrency(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatPercent(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)}%`;
}

export function formatQuantity(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

export function formatCheckValue(
  value: string | number | boolean | null | undefined,
  t: Translate,
): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? t("runs.yes") : t("runs.no");
  if (typeof value === "number") return formatQuantity(value);
  // Long digests belong in Evidence; the Decision rail needs a scannable value.
  if (/^[0-9a-f]{24,}$/i.test(value)) return `${value.slice(0, 12)}…`;
  return value;
}

/** Count sales orders from the compact ``SO-1..5`` reference. */
export function orderCount(ref: string | null | undefined): number | null {
  if (!ref) return null;
  const range = /(\d+)\s*\.\.\s*(\d+)/.exec(ref);
  if (range) {
    const start = Number(range[1]);
    const end = Number(range[2]);
    if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
      return end - start + 1;
    }
  }
  return null;
}

export function severityOf(caseFile: CaseFile): Severity {
  if (["failed", "execution_unknown"].includes(caseFile.state)) return "high";
  return planningAttention(caseFile.plan);
}

export function severityLabel(severity: Severity, t: Translate): string {
  if (severity === "unknown") return t("cases.severityUnknown");
  if (severity === "high") return t("cases.severityHigh");
  if (severity === "medium") return t("cases.severityMedium");
  return t("cases.severityLow");
}

export function exceptionText(caseFile: CaseFile, t: Translate): string {
  switch (caseFile.disruption.kind) {
    case "workcenter_outage":
      return t("cases.exceptionWorkcenter", { workcenter: caseFile.disruption.workcenter || caseFile.disruption.workcenter_code || t("v9.unknown") });
    case "supplier_cancellation":
      return t("cases.exceptionSupplierCancellation", {
        vendor: caseFile.disruption.vendor || t("v9.unknown"),
      });
    case "supplier_disruption":
    case "supplier_cancellation_risk":
      return t("cases.exceptionSupplierDisruption", {
        vendor: caseFile.disruption.vendor || t("v9.unknown"),
      });
    default:
      return caseFile.disruption.kind
        ? t("cases.exceptionUnknown")
        : t("cases.exceptionUnknown");
  }
}

export function dueWindow(caseFile: CaseFile, t: Translate): string {
  const recorded = recordedDueRange(caseFile);
  if (recorded) return recorded.earliest === recorded.latest ? recorded.earliest : `${recorded.earliest} – ${recorded.latest}`;
  const { due_days: earliest, due_days_max: latest } = caseFile.order;
  return typeof latest === "number" && latest > earliest
    ? t("case.daysRange", { earliest, latest })
    : t("case.days", { count: earliest });
}

export function actionKindLabel(
  kind: string | undefined,
  t: Translate,
): string {
  switch (kind) {
    case "cancel_po":
      return t("case.actionCancel");
    case "create_po":
      return t("case.actionCreate");
    case "adjust_mo":
    case "update_mo":
      return t("case.actionAdjust");
    case "create_mo":
      return t("case.actionCreateMo");
    case "cancel_mo":
      return t("case.actionCancelMo");
    case "update_po":
      return t("case.actionUpdate");
    default:
      return kind || t("case.actionOther");
  }
}

export function actionCountsLabel(actions: ProductAction[], t: Translate): string {
  const counts = new Map<string, number>();
  for (const action of actions) {
    const label = actionKindLabel(action.kind, t);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts].map(([label, count]) => `${label}: ${count}`).join("; ");
}

export function actionKindClass(kind: string | undefined): string {
  switch (kind) {
    case "cancel_po":
    case "cancel_mo":
      return "is-cancel";
    case "create_po":
    case "create_mo":
      return "is-create";
    case "adjust_mo":
    case "update_mo":
    case "update_po":
      return "is-adjust";
    default:
      return "is-other";
  }
}

/**
 * The executor exposes internal tokens such as ``new:purchase.order:0006``.
 * Business views must show a human label; the raw value stays available only
 * in evidence fields (and even there we avoid it in the main body).
 */
export function friendlyDocument(
  document: string | null | undefined,
  t: Translate,
  actions?: readonly ProductAction[],
): string {
  if (!document) return "—";
  const prefix = document.startsWith("new:purchase.order:") ? "new:purchase.order:" : document.startsWith("new:mrp.production:") ? "new:mrp.production:" : null;
  if (prefix) {
    const labels = [...new Set(actions?.filter(action => action.document.startsWith(prefix)).map(action => action.document) ?? [])];
    const position = labels.indexOf(document);
    const number = position >= 0 ? position + 1 : null;
    return `${t(prefix === "new:mrp.production:" ? "case.docNewMo" : "case.docNewPo")}${number !== null ? ` ${number}` : ""}`;
  }
  if (document.startsWith("new:") || /^new\s/i.test(document)) return t("case.docNew");
  if (/^P\d/i.test(document) || /^purchase/i.test(document)) {
    return t("case.docPo", { document });
  }
  if (/^MO/i.test(document)) return t("case.docMo", { document });
  return document;
}

export function actionSummary(
  action: ProductAction,
  _caseFile: CaseFile,
  t: Translate,
): string {
  const labels: Record<string, MessageKey> = {
    cancel_po: "case.actionSummaryCancel", create_po: "case.actionSummaryCreate", update_po: "case.actionSummaryUpdatePo",
    create_mo: "case.actionSummaryCreateMo", cancel_mo: "case.actionSummaryCancelMo", update_mo: "case.actionSummaryAdjust", adjust_mo: "case.actionSummaryAdjust",
  };
  return labels[action.kind] ? t(labels[action.kind]) : t("case.actionSummaryGeneric", { summary: action.summary });
}

/** Read the legacy new_spend field as planned cost, without deriving an incremental cost. */
export function planNewSpend(plan: ProductPlan | null | undefined): number | null {
  if (!plan) return null;
  if (typeof plan.new_spend === "number" && Number.isFinite(plan.new_spend)) {
    return plan.new_spend;
  }
  return null;
}

/** Only the server's strict result projection can label a case verified. */
export function isCaseVerified(caseFile: CaseFile): boolean {
  return caseFile.result?.verified === true
    && caseFile.result.strict_success === true
    && caseFile.result.evaluation_valid === true;
}

/** Saved views shared by the case list and the start page cards. */
export type SavedCaseView = "decision" | "risk" | "executing" | "verified" | "all";

export function matchesSavedView(item: CaseFile, view: SavedCaseView): boolean {
  if (view === "decision") return ["awaiting_selection", "awaiting_approval"].includes(item.state);
  if (view === "executing") return ["preparing", "executing"].includes(item.state);
  if (view === "verified") return isCaseVerified(item);
  // Views are disjoint on purpose: a case waiting for a decision is counted
  // there, not here as well, so the counters can be added up.
  if (view === "risk") {
    return ["failed", "rejected", "expired"].includes(item.state)
      || (!!item.execution && !isCaseVerified(item) && item.result?.simulation_passed !== true);
  }
  return true;
}

export function caseOutcomeLabel(caseFile: CaseFile, t: Translate): string {
  if (caseFile.reason === "product_selection_conflict" && !caseFile.execution) return t("candidate.conflictTitle");
  if (caseFile.decision?.selection_pending) return t("candidate.pendingTitle");
  if (preparationPending(caseFile)) return t(preparationPending(caseFile)!.title);
  if (caseFile.execution?.status === "not_started") return t("v10.unstartedClosed");
  if (caseFile.state === "failed") return t("case.state.failed");
  if (caseFile.result?.outcome === "execution_unknown") return t("v9.executionUnknown");
  if (caseFile.result?.simulation_passed === true) return t("v9.simulatedPassed");
  if (isCaseVerified(caseFile)) return t(caseFile.execution?.verifier?.source === "synthetic_executor" ? "v9.simulatedPassed" : "v9.verified");
  if (caseFile.result?.verifier_status === "failed" || caseFile.execution?.verifier?.strict_success === false) return t("v9.verificationFailed");
  if (caseFile.execution) return t("v9.verificationUnknown");
  return stateLabel(caseFile.state, t);
}

export function normalizeCheckStatus(
  status: ProductPlanCheck["status"] | null | undefined,
): "pass" | "warn" | "block" | "planned" | "unknown" {
  if (status === "pass" || status === "warn" || status === "block") {
    return status;
  }
  if (status === "planned") return "planned";
  return "unknown";
}

export function marginFloor(plan: ProductPlan | null | undefined): number | null {
  if (!plan?.checks || !hasNewSpendBasis(plan)) return null;
  for (const check of plan.checks) {
    if (!check.code.includes("margin")) continue;
    if (typeof check.threshold === "number" && Number.isFinite(check.threshold)) {
      return check.threshold;
    }
    if (typeof check.threshold === "string") {
      const parsed = Number.parseFloat(check.threshold);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

/**
 * Server measurements and statuses remain unchanged; known descriptions are localized. Until the API returns them,
 * the panel shows derived facts explicitly marked ``planned`` (unknown), so a
 * business reader is never told that an unchecked margin was verified.
 */
export type PresentedPlanCheck = ProductPlanCheck & {
  budgetBasis?: "per_order" | "legacy_total" | "unrecorded";
  /** Identifier kept out of the rendered value; surfaced only as a tooltip. */
  technical?: string;
  /** Set when no UI copy exists for this check code. */
  unknownCode?: string;
};

/** Basis identifies meaning, not approval eligibility. Never upgrade a legacy check from its label or metrics. */
export function budgetCheckBasis(check: ProductPlanCheck, plan: ProductPlan | null): PresentedPlanCheck["budgetBasis"] {
  if (check.code !== "budget") return undefined;
  const basis = check.basis;
  const { actual, threshold, status } = check;
  const validTotal = typeof threshold === "number" && Number.isInteger(threshold) && threshold > 0 && threshold <= 128;
  const validCount = validTotal && typeof actual === "number" && Number.isInteger(actual);
  const validResult = (status === "pass" && validCount && actual === threshold)
    || (status === "block" && validCount && actual >= 0 && actual < threshold)
    || (status === "unknown" && actual === null && (threshold === null || validTotal));
  if (validResult && basis && Object.keys(basis).length === 3 && basis.kind === "sales_order_value_per_customer_cap_v1"
      && /^[a-f0-9]{64}$/.test(basis.plan_id) && /^[a-f0-9]{64}$/.test(basis.snapshot_sha256)
      && basis.plan_id === plan?.plan_id && basis.snapshot_sha256 === plan.snapshot_sha256) return "per_order";
  if (!("basis" in check) && check.source === "solver planned cost compared with summed sales-order customer budget caps; per-order caps remain policy checks, not purchase authorization") return "legacy_total";
  return "unrecorded";
}

export function policyCheckValue(check: PresentedPlanCheck, t: Translate): string {
  const actual = formatCheckValue(check.actual, t);
  if (check.budgetBasis === "per_order") return t("budgetCheck.count", {
    actual: check.actual == null ? t("v9.unknownResults") : actual,
    threshold: check.threshold == null ? t("v9.unknownResults") : formatCheckValue(check.threshold, t),
  });
  return actual + (check.threshold == null ? "" : " / " + formatCheckValue(check.threshold, t));
}

export function budgetAggregateNote(checks: PresentedPlanCheck[], t: Translate): string {
  if (checks.some(check => check.budgetBasis === "unrecorded")) return t("budgetCheck.aggregateUnknown");
  if (checks.some(check => check.budgetBasis === "legacy_total")) return t("budgetCheck.aggregateLegacy");
  return "";
}

export function budgetCheckSummary(plan: ProductPlan | null, t: Translate): string {
  const raw = plan?.checks?.find(check => check.code === "budget");
  if (!raw) return t("budgetCheck.unrecordedNote");
  const check = presentedCheck(raw, t, plan);
  const status = normalizeCheckStatus(check.status);
  const label = t(status === "pass" ? "decision.checkStatusPass" : status === "warn" ? "decision.checkStatusWarn"
    : status === "block" ? "decision.checkStatusBlock" : status === "planned" ? "decision.checkStatusPlanned" : "decision.checkStatusUnknown");
  return `${check.label}: ${label}; ${policyCheckValue(check, t)}. ${check.source ?? ""}`;
}

/** Keep recorded values; legacy margin passes cannot establish current compliance. */
function presentedCheck(check: ProductPlanCheck, t: Translate, plan: ProductPlan | null): PresentedPlanCheck {
  if (check.code.includes("margin")) {
    const current = hasNewSpendBasis(plan);
    const measured = /^independent new-spend coverage check; original floor ([0-9]+(?:\.[0-9]+)?)%; effective floor ([0-9]+(?:\.[0-9]+)?)%; excludes finished stock revenue$/.exec(check.source ?? "");
    return { ...check, label: t(current ? "v10.checkMargin" : "decision.marginLegacy"),
      status: current ? check.status : "unknown", threshold: current ? check.threshold : null,
      source: !current ? t("decision.marginLegacyNote") : measured
        ? t("v10.sourceRevisedMargin", { original: measured[1], effective: measured[2] }) : t("v10.sourceMargin"),
    };
  }
  const budgetBasis = budgetCheckBasis(check, plan);
  if (budgetBasis) return { ...check, budgetBasis,
    label: t(budgetBasis === "per_order" ? "budgetCheck.perOrder" : budgetBasis === "legacy_total" ? "budgetCheck.legacy" : "budgetCheck.unrecorded"),
    source: t(budgetBasis === "per_order" ? "budgetCheck.perOrderNote" : budgetBasis === "legacy_total" ? "budgetCheck.legacyNote" : "budgetCheck.unrecordedNote"),
  };
  // Translate by check code, not by the server's English label: older plans
  // carry shorter labels ("Margin floor"), and matching them verbatim used to
  // leak raw English into the Chinese UI.
  const LABELS: Record<string, MessageKey> = {
    margin: "v10.checkMargin",
    demand: "v10.checkDemand",
    on_time: "v10.checkOnTime",
    policy: "v10.checkPolicy",
    date: "decision.checkDate",
  };
  const labelKey = LABELS[check.code];
  if (!labelKey) {
    // An unknown check keeps the server's own label but says so, instead of
    // passing an untranslated string through as if it were a product term.
    return { ...check, unknownCode: check.code, label: check.label || check.code };
  }
  const sources: Record<string, MessageKey> = {
    "solver planned purchasing and manufacturing cost, including retained documents; local request margin; not measured accounting margin": "v10.sourceMargin",
    "local request margin": "v10.sourceMargin",
    "local solver plan": "v10.sourceDemand",
    "bound plan allocations and change-set sales-order flows": "v10.sourceOnTime",
    "local plan binding": "v10.sourcePolicy",
  };
  const revised = /^independent document-cost check; original floor ([0-9]+(?:\.[0-9]+)?)%; additional floor ([0-9]+(?:\.[0-9]+)?)%; not measured accounting margin$/.exec(check.source ?? "");
  const source = revised ? t("v10.sourceRevisedMargin", { original: revised[1], effective: revised[2] })
    : sources[check.source ?? ""] ? t(sources[check.source!]) : check.source;
  const units = (value: ProductPlanCheck["actual"]) => typeof value === "string" && /^([0-9]+(?:\.[0-9]+)?) units$/.test(value)
    ? t("decision.unitsValuePlain", { units: value.slice(0, -6) }) : value;
  // A policy check's value is the bound contract hash. The hash is an
  // identifier, not a business fact, so it stays out of the rendered value.
  if (check.code === "policy") {
    const raw = typeof check.actual === "string" ? check.actual.trim() : null;
    const hashLike = raw !== null && /^[0-9a-f]{6,}(\.\.\.|…)?$/i.test(raw);
    return {
      ...check,
      label: t(labelKey),
      source,
      technical: hashLike ? raw ?? undefined : undefined,
      actual: hashLike ? t("v13.policyBound") : units(check.actual),
      threshold: check.threshold === "required" ? t("v13.policyRequired") : units(check.threshold),
    };
  }
  return { ...check, label: t(labelKey), source, actual: units(check.actual),
    threshold: check.code === "on_time" && check.threshold === "all selected orders by their deadlines" ? t("v10.allOrderDeadlines")
      : units(check.threshold) };
}

export function effectiveChecks(
  caseFile: CaseFile,
  t: Translate,
): { checks: PresentedPlanCheck[]; derived: boolean } {
  const plan = caseFile.plan;
  if (!plan) return { checks: [], derived: false };
  if (plan.checks && plan.checks.length > 0) {
    return {
      checks: plan.checks.map((check) => {
        const presented = presentedCheck(check, t, plan);
        return { ...presented, status: normalizeCheckStatus(presented.status) };
      }),
      derived: false,
    };
  }
  const orders = orderCount(caseFile.order.ref);
  return {
    derived: true,
    checks: [
      {
        code: "demand",
        label: t("decision.checkDemand"),
        status: "unknown",
        actual:
          orders && orders > 1
            ? t("decision.unitsValue", {
                units: formatQuantity(caseFile.order.units),
                orders,
              })
            : t("decision.unitsValuePlain", {
                units: formatQuantity(caseFile.order.units),
              }),
        threshold: null,
        source: t("case.checkSourceCase"),
      },
      {
        code: "margin",
        label: marginLabel(plan, t),
        status: "unknown",
        actual: marginValue(plan, t),
        threshold: null,
        source: t("case.checkSourcePolicy"),
      },
      {
        code: "budget",
        label: t("budgetCheck.unrecorded"),
        status: "unknown",
        actual: null,
        threshold: null,
        source: t("budgetCheck.unrecordedNote"),
        budgetBasis: "unrecorded",
      },
      {
        code: "date",
        label: t("decision.checkDate"),
        status: "unknown",
        actual: dueWindow(caseFile, t),
        threshold: null,
        source: t("case.checkSourcePlan"),
      },
    ],
  };
}

export function checkCounts(checks: ProductPlanCheck[]): {
  pass: number;
  warn: number;
  block: number;
  planned: number;
} {
  const counts = { pass: 0, warn: 0, block: 0, planned: 0 };
  for (const check of checks) {
    const status = normalizeCheckStatus(check.status);
    if (status === "pass" || status === "warn" || status === "block") {
      counts[status] += 1;
    } else if (status === "planned") {
      counts.planned += 1;
    }
  }
  return counts;
}

/** Human names for the checks the server treats as required for approval. */
const CHECK_NAMES: Record<string, MessageKey> = {
  margin: "v10.checkMargin",
  budget: "v13.checkBudgetName",
  demand: "v10.checkDemand",
  policy: "v10.checkPolicy",
  on_time: "v10.checkOnTime",
};

/** Unknown codes stay visible as the raw code instead of being dropped. */
export function checkDisplayName(code: string, t: Translate): string {
  const key = CHECK_NAMES[code];
  return key ? t(key) : code;
}

/**
 * Required checks the plan never recorded. The server marks these as unknown,
 * which blocks approval; the list must be shown, not silently omitted.
 */
export function missingRequiredChecks(
  unknownChecks: string[] | undefined,
  checks: ProductPlanCheck[],
): string[] {
  const present = new Set(checks.map((check) => check.code));
  return (unknownChecks ?? []).filter((code) => !present.has(code));
}

export type ApprovalBlock = {
  /** deployment = this workspace cannot approve at all; missing = a required check was never recorded. */
  kind: "deployment" | "environment" | "scenario" | "blocked" | "missing" | "state" | "legacy" | "unknown";
  reason: string;
  recovery: string | null;
  /** A second, independent reason that also has to be fixed. */
  also?: string | null;
};

export type ApprovalGate = { canApprove: boolean; block: ApprovalBlock | null };

/**
 * One place decides whether approval is possible and, when it is not, says why
 * in the reader's terms. Deployment-level blocks are never reported as check
 * problems, and a missing required check is always named.
 */
export function approvalGate(input: {
  caseFile: CaseFile;
  checks: PresentedPlanCheck[];
  fixture: boolean;
  live: boolean;
  awaiting: boolean;
  canDecide: boolean;
  approvalCapable: boolean;
  capabilities?: Capabilities | null;
  busy: boolean;
  t: Translate;
}): ApprovalGate {
  const { caseFile, checks, fixture, live, awaiting, canDecide, approvalCapable, busy, t } = input;
  const environment = !fixture ? environmentAccessNotice(t, input.capabilities, caseFile) : null;
  if (environment) return { canApprove: false, block: environment };
  const hasPlan = caseFile.plan !== null && caseFile.plan !== undefined;
  const reason = caseFile.decision?.reason ?? null;
  const blocked = checks.filter((check) => check.status === "block");
  const missing = missingRequiredChecks(caseFile.decision?.unknown_checks, checks);
  const canApprove = !fixture
    && live
    && canDecide
    && awaiting
    && hasPlan
    && blocked.length === 0
    && caseFile.decision?.can_approve === true
    && !busy;

  const names = (codes: string[]) => codes.map((code) => checkDisplayName(code, t)).join(t("v13.checkNameSeparator"));
  if (canApprove) return { canApprove: true, block: null };
  if (!approvalCapable || reason === "product_execution_unavailable") {
    // Connecting an executor is not enough when a required check was never
    // recorded, so say that here instead of letting the reader find out later.
    const also = missing.length > 0 || reason === "required_checks_unknown"
      ? t("v14.blockAlsoMissing", { names: names(missing) })
      : null;
    return {
      canApprove: false,
      block: { kind: "deployment",
        reason: t(input.capabilities?.deployment === "hosted" ? "environment.unavailable" : "v13.blockDeployment"),
        recovery: t(input.capabilities?.deployment === "hosted" ? "environment.unavailableRecovery" : "v13.recoveryDeployment"), also },
    };
  }
  if (reason === "product_scenario_unavailable") {
    return { canApprove: false, block: { kind: "scenario", reason: t("v13.blockScenario"), recovery: t("v13.recoveryScenario") } };
  }
  if (blocked.length > 0) {
    return {
      canApprove: false,
      block: { kind: "blocked", reason: t("v13.blockChecksBlocked", { names: names(blocked.map((check) => check.code)) }), recovery: t("v13.recoveryRevise") },
    };
  }
  if (missing.length > 0 || reason === "required_checks_unknown") {
    return {
      canApprove: false,
      block: { kind: "missing", reason: t("v13.blockChecksMissing", { names: names(missing) }), recovery: t("v13.recoveryReprepare") },
    };
  }
  if (reason === "plan_already_applied") {
    return { canApprove: false, block: { kind: "state", reason: t("v9.executionComplete"), recovery: null } };
  }
  if (reason === "approval_expired") {
    return { canApprove: false, block: { kind: "state", reason: t("v13.blockExpired"), recovery: t("v13.recoveryReprepare") } };
  }
  if (reason === "product_binding_missing") {
    return { canApprove: false, block: { kind: "missing", reason: t("v14.bindingMissing"), recovery: t("v13.recoveryReprepare") } };
  }
  if (reason === "product_version_required") {
    // The existing copy already tells the reader how to recover.
    return { canApprove: false, block: { kind: "legacy", reason: t("v10.legacyReadOnly"), recovery: null } };
  }
  if (!hasPlan) {
    return { canApprove: false, block: { kind: "state", reason: t("decision.disabledNoPlan"), recovery: t("v13.recoveryPrepare") } };
  }
  if (reason === "case_not_approvable" || !awaiting) {
    return { canApprove: false, block: { kind: "state", reason: t("v13.blockNotApprovable"), recovery: null } };
  }
  return { canApprove: false, block: { kind: "unknown", reason: t("v9.decisionUnavailable", { reason: stateLabel(caseFile.state, t) }), recovery: null } };
}

export type VerifierSummary = {
  status: string;
  passed: number | null;
  total: number | null;
  score: number | null;
  label: string;
  tone: "success" | "failed" | "neutral";
};

export function verifierSummary(
  execution: CaseFile["execution"] | null | undefined,
  t: Translate,
  result?: CaseFile["result"],
): VerifierSummary {
  const verifier = execution?.verifier ?? null;
  const checks: ProductVerifierCheck[] = verifier?.checks ?? [];
  const aggregate = checks.find(
    (check) => typeof check.total === "number" && check.total > 0,
  );
  const score =
    typeof verifier?.score === "number" && Number.isFinite(verifier.score)
      ? verifier.score
      : null;
  const strict = result ? result.strict_success : verifier?.strict_success;
  const valid = result ? result.evaluation_valid : verifier?.evaluation_valid;
  const contradictoryFailure = (verifier?.failed_rule_count ?? 0) > 0 || checks.some(check => check.passed === false);
  const status = strict === true && valid === true && !contradictoryFailure
    ? "passed"
    : strict === false || contradictoryFailure || verifier?.status === "failed" ? "failed" : "unknown";
  const label =
    status === "passed"
      ? t(verifier?.source === "synthetic_executor" ? "v9.simulatedPassed" : "case.verifierPassed")
      : status === "failed"
        ? t("case.verifierFailed")
        : t("v9.verificationUnknown");
  return {
    status,
    passed:
      typeof verifier?.passed_rule_count === "number"
        ? verifier.passed_rule_count
        : null,
    total: typeof verifier?.applicable_rule_count === "number" ? verifier.applicable_rule_count : typeof aggregate?.total === "number" ? aggregate.total : null,
    score,
    label,
    tone:
      status === "passed" ? "success" : status === "failed" ? "failed" : "neutral",
  };
}

/**
 * One repeatable sentence for a verified case: what was covered and on what
 * evidence. Returns null unless the server's strict result says verified.
 */
const VERIFIER_KINDS: Record<string, MessageKey> = {
  original_verifier: "v13.verifierKindOriginal",
  synthetic_executor: "v13.verifierKindSynthetic",
  local_verifier: "v13.verifierKindLocal",
};

/** Server verifier kind codes never reach the reader as snake_case. */
export function verifierKindKey(kind: string | null | undefined): MessageKey {
  return (kind && VERIFIER_KINDS[kind]) || "v13.verifierKindOther";
}

export function caseConclusion(caseFile: CaseFile, t: Translate): string | null {
  if (!isCaseVerified(caseFile)) return null;
  const summary = verifierSummary(caseFile.execution, t, caseFile.result);
  if (summary.passed === null || summary.total === null) return null;
  const vars = {
    orders: orderCount(caseFile.order.ref) ?? "?",
    units: formatQuantity(caseFile.order.units),
    product: caseFile.order.product,
    due: dueWindow(caseFile, t),
    passed: summary.passed,
    total: summary.total,
    score: summary.score === null ? "" : `${summary.score.toFixed(2)}/100`,
  };
  return t(summary.score === null ? "v13.conclusionNoScore" : "v13.conclusion", vars);
}

const READBACK_STATES: Record<string, MessageKey> = {
  cancel: "v14.readbackCancel",
  purchase: "v14.readbackPurchase",
  confirmed: "v14.readbackConfirmed",
  reflected: "v14.readbackReflected",
  draft: "v14.readbackDraft",
};

/**
 * Readback states arrive as server tokens. Known ones get plain words; an
 * unknown one is shown as-is and marked, exactly like an unknown check code.
 */
export function readbackState(state: string | null | undefined, t: Translate): { label: string; unknown: boolean } {
  const token = (state ?? "").trim();
  if (!token) return { label: t("v9.unknown"), unknown: false };
  const key = READBACK_STATES[token];
  return key ? { label: t(key), unknown: false } : { label: token, unknown: true };
}

/** Rules-only verifier line: the score is stated once, in the conclusion. */
export function verifierRuleText(summary: VerifierSummary, execution: CaseFile["execution"] | null | undefined, t: Translate): string {
  if (summary.passed === null || summary.total === null) return summary.label;
  return t(execution?.verifier?.source === "synthetic_executor" ? "v13.verifierRulesSimulated" : "v13.verifierRulesOriginal", {
    passed: summary.passed,
    total: summary.total,
  });
}

/**
 * The server labels the local principal as "Operator <8 hex>". Those eight hex
 * characters are an identifier, not a name, so they stay in technical details.
 */
export function approverLabel(label: string | null | undefined, t: Translate): string {
  if (!label) return t("runs.notRecorded");
  return /^Operator [0-9a-f]{8}$/.test(label.trim()) ? t("decision.approverDefault") : label;
}

export function verifierSummaryText(
  execution: CaseFile["execution"] | null | undefined,
  t: Translate,
  result?: CaseFile["result"],
): string {
  const summary = verifierSummary(execution, t, result);
  if (summary.total !== null && summary.passed !== null) {
    return t("decision.receiptVerifierScore", {
      passed: summary.passed,
      total: summary.total,
      score: summary.score === null ? "—" : summary.score.toFixed(2),
    });
  }
  if (summary.score !== null) {
    return `${summary.label} · ${summary.score.toFixed(2)}`;
  }
  return summary.label;
}

export type Step = {
  key: string;
  labelKey: MessageKey;
  status: StepState;
};

function approvalRecorded(caseFile: CaseFile): boolean {
  if (caseFile.decision?.authorization_pending) return false;
  return !!caseFile.approval || !!caseFile.plan && (caseFile.audit ?? []).some(row => !!row && typeof row === "object" && "action" in row && row.action === "approved" && "plan_id" in row && row.plan_id === caseFile.plan?.plan_id);
}

export function isExecutionUnknown(caseFile: CaseFile): boolean {
  return caseFile.state === "execution_unknown" || caseFile.result?.outcome === "execution_unknown" || ["unknown", "execution_unknown"].includes(caseFile.execution?.status ?? "");
}

export function preparationPending(caseFile: CaseFile): { title: MessageKey; body: MessageKey } | null {
  if (caseFile.decision?.revision_pending) return { title: "v10.revisionPendingTitle", body: "v10.revisionPendingBody" };
  if (caseFile.decision?.repreparation_pending) return { title: "v10.repreparationPendingTitle", body: "v10.repreparationPendingBody" };
  if (caseFile.decision?.preparation_pending) return { title: "v10.preparationPendingTitle", body: "v10.preparationPendingBody" };
  return null;
}

export function caseSteps(caseFile: CaseFile): Step[] {
  let statuses: StepState[] = ["active", "pending", "pending", "pending", "pending"];
  const state = caseFile.state;
  if (caseFile.execution?.status === "not_started") {
    statuses = ["done", "done", "done", "pending", "pending"];
  } else if (isExecutionUnknown(caseFile)) {
    statuses = ["done", caseFile.plan ? "done" : "pending", approvalRecorded(caseFile) ? "done" : "pending", "pending", "pending"];
  } else if (state === "succeeded" || state === "success") {
    statuses = ["done", "done", "done", "done", isCaseVerified(caseFile) || caseFile.result?.simulation_passed === true ? "done" : caseFile.result?.strict_success === false ? "failed" : "pending"];
  } else if (state === "awaiting_approval" || state === "awaiting_selection") {
    statuses = ["done", "done", "active", "pending", "pending"];
  } else if (state === "executing") {
    statuses = ["done", "done", "done", "active", "pending"];
  } else if (preparationPending(caseFile)) {
    statuses = ["done", "pending", "pending", "pending", "pending"];
  } else if (state === "preparing") {
    statuses = ["done", "active", "pending", "pending", "pending"];
  } else if (caseFile.reason === "product_selection_conflict" && !caseFile.execution) {
    statuses = ["done", caseFile.comparison?.status === "ready" ? "done" : "pending", "failed", "pending", "pending"];
  } else if (state === "failed") {
    if (caseFile.plan === null) {
      statuses = ["done", "failed", "pending", "pending", "pending"];
    } else if (approvalRecorded(caseFile)) {
      statuses = ["done", "done", "done", "failed", "pending"];
    } else {
      statuses = ["done", "done", "pending", "pending", "pending"];
    }
  } else if (state === "rejected") {
    statuses = ["done", "done", "failed", "pending", "pending"];
  } else if (state === "expired") {
    statuses = ["done", "done", "failed", "pending", "pending"];
  }
  // Step names follow the step's own status: a finished case must not still say
  // it is waiting for a decision.
  const decisionLabel = (status: StepState): MessageKey =>
    status === "done" ? "case.step.decisionApproved" : status === "failed" ? "case.step.decisionRejected" : "case.step.decision";
  const executingLabel = (status: StepState): MessageKey =>
    isExecutionUnknown(caseFile)
      ? "v9.executionUnknown"
      : status === "done" ? "case.step.executed" : status === "active" ? "case.step.executing" : "case.step.executingPending";
  const verifiedLabel = (status: StepState): MessageKey =>
    caseFile.result?.simulation_passed === true ? "v9.simulatedCheck" : status === "done" ? "case.step.verified" : "case.step.verifying";
  return statuses.map((status, index) => {
    const label: MessageKey = index === 2
      ? decisionLabel(status)
      : index === 3
        ? executingLabel(status)
        : index === 4
          ? verifiedLabel(status)
          : index === 0
            ? "case.step.detected"
            : "case.step.prepared";
    return { key: label, labelKey: label, status };
  });
}

export type TimelineEvent = {
  id: string;
  label: string;
  detail: string;
  time: string;
  status: StepState;
};

export function timelineEvents(
  caseFile: CaseFile,
  t: Translate,
  lang: Lang,
): TimelineEvent[] {
  const steps = caseSteps(caseFile);
  const state = caseFile.state;
  const plan = caseFile.plan;
  const execution = caseFile.execution;
  const checks = effectiveChecks(caseFile, t).checks;
  const summary = verifierSummary(execution, t, caseFile.result);
  const orderText = orderCount(caseFile.order.ref)
    ? t("decision.unitsValue", {
        units: formatQuantity(caseFile.order.units),
        orders: orderCount(caseFile.order.ref) ?? 1,
      })
    : t("decision.unitsValuePlain", {
        units: formatQuantity(caseFile.order.units),
      });

  const pending = preparationPending(caseFile);
  const comparison = caseFile.comparison?.status === "ready" ? caseFile.comparison : null;
  const preparedDetail = pending ? t(pending.body) : comparison && !plan
    ? t("candidate.basis", { count: comparison.plans.length }) : plan
    ? [
        t("case.timeline.detailActions", { count: plan.actions.length }),
        checks.length > 0
          ? effectiveChecks(caseFile, t).derived
            ? t("case.timeline.detailChecksPlanned")
            : t("case.timeline.detailChecks", { count: checks.length })
          : t("case.timeline.detailChecksPlanned"),
      ].join(" · ")
    : caseFile.decision?.preparation_pending ? t("v10.preparationPendingBody") : state === "failed" ? t("v9.preparationFailed") : t("case.noPlanBody");

  const decisionDetail = caseFile.reason === "product_selection_conflict" ? t("candidate.conflictBody") : caseFile.decision?.selection_pending ? t("candidate.pendingBody") : state === "awaiting_selection" ? t("candidate.chooseFirst") : execution?.status === "not_started" ? t("v10.unstartedClosed") : approvalRecorded(caseFile)
    ? t("case.timeline.decisionDone")
    : state === "rejected" || state === "expired"
      ? stateLabel(state, t)
      : state === "awaiting_approval"
        ? t("case.timeline.detailAwait")
        : t("v9.noApprovalRecorded");

  const executionDetail = caseFile.reason === "product_selection_conflict" && !execution ? t("v9.writeScopeUnknown") : execution?.status === "not_started" ? t("v10.unstartedReceipt") : isExecutionUnknown(caseFile) ? t("v9.writeScopeUnknown") : state === "executing" ? t("v9.executionInProgress") : execution
    ? t("case.timeline.detailExecuted", { count: execution.writes.length })
    : state === "failed" && approvalRecorded(caseFile)
      ? t("v9.executionUnknown")
      : t("case.noExecution");

  const verifiedDetail = caseFile.reason === "product_selection_conflict" && !execution ? t("v9.verificationUnknown") : execution?.status === "not_started" ? t("case.noExecution") : state === "executing" ? t("v9.executionInProgress") : execution
    ? t("case.timeline.detailVerifier", { status: summary.label })
    : t("case.noExecution");

  return [
    {
      id: "detected",
      label: t(caseFile.disruption.kind === "workcenter_outage" ? "case.timeline.intakeOutage" : "case.timeline.intake"),
      detail: `${exceptionText(caseFile, t)} · ${orderText}`,
      time: formatDateTime(caseFile.created_at, lang),
      status: steps[0].status,
    },
    {
      id: "prepared",
      label: pending ? t(pending.title) : plan || comparison ? t("case.timeline.prepared") : caseFile.decision?.preparation_pending ? t("v10.preparationPendingTitle") : state === "failed" ? t("v9.preparationFailed") : t("v9.planPreparation"),
      detail: preparedDetail,
      time: !pending && (plan || comparison) ? formatDateTime(plan?.prepared_at ?? comparison?.plans[0]?.prepared_at, lang) : "—",
      status: steps[1].status,
    },
    {
      id: "decision",
      label:
        approvalRecorded(caseFile)
          ? t("case.timeline.decisionDone")
          : caseFile.decision?.selection_pending ? t("candidate.pendingTitle") : state === "awaiting_selection" ? t("candidate.awaiting") : state === "awaiting_approval" ? t("case.timeline.decision") : t("case.step.decision"),
      detail: decisionDetail,
      time: formatDateTime(caseFile.approval?.issued_at, lang),
      status: steps[2].status,
    },
    {
      id: "executing",
      label: t(execution?.status === "not_started" ? "v10.unstartedClosed" : isExecutionUnknown(caseFile) ? "v9.executionUnknown" : "case.timeline.executing"),
      detail: executionDetail,
      time: execution ? formatDateTime(caseFile.updated_at, lang) : "—",
      status: steps[3].status,
    },
    {
      id: "verified",
      label: t("case.timeline.verified"),
      detail: verifiedDetail,
      time: execution ? formatDateTime(caseFile.updated_at, lang) : "—",
      status: steps[4].status,
    },
  ];
}

export function buildCaseActivity(
  caseFile: CaseFile,
  t: Translate,
): ActivityState | null {
  if (!caseFile) return null;
  const audit = (caseFile.audit ?? []).filter((row): row is { action: string; at: string; plan_id: string } => !!row && typeof row === "object" && "action" in row && "at" in row && "plan_id" in row && (row as { plan_id: unknown }).plan_id === caseFile.plan?.plan_id);
  const approvedAt = Date.parse(audit.find(row => row.action === "approved")?.at || "");
  const observedAt = Date.parse(audit.find(row => row.action === "execution_observed")?.at || "");
  const measured = Number.isFinite(approvedAt) && Number.isFinite(observedAt) && observedAt >= approvedAt;
  const base = measured ? approvedAt : 0;
  const finishedAt = measured ? observedAt : undefined;
  const plan = caseFile.plan;
  const execution = caseFile.execution;
  const state = caseFile.state;
  const steps: ActivityState["steps"] = [];
  const checks = effectiveChecks(caseFile, t);
  const summary = verifierSummary(execution, t, caseFile.result);
  const active = state === "executing";
  const pending = preparationPending(caseFile);

  steps.push({
    id: "read",
    kind: "read",
    label: t("case.activityRead"),
    detail: caseFile.order.ref,
    status: pending ? "pending" : "done",
  });
  if (pending) {
    steps.push({ id: "plan", kind: "solver", label: t(pending.title), detail: t(pending.body), status: "pending" });
  } else if (caseFile.comparison?.status === "ready" && !plan) {
    steps.push({ id: "plan", kind: "solver", label: t("case.activityPlan"),
      detail: t("candidate.basis", { count: caseFile.comparison.plans.length }), status: "done" });
  } else if (plan) {
    steps.push({
      id: "plan",
      kind: "solver",
      label: t("case.activityPlan"),
      detail: t("case.timeline.detailActions", { count: plan.actions.length }),
      status: "done",
    });
    steps.push({
      id: "policy",
      kind: "policy",
      label: t("case.activityPolicy"),
      detail: checks.derived
        ? t("case.timeline.detailChecksPlanned")
        : t("case.timeline.detailChecks", { count: checks.checks.length }),
      status: "done",
    });
  } else {
    steps.push({
      id: "plan",
      kind: "solver",
      label: t("case.activityPlan"),
      detail: caseFile.decision?.preparation_pending ? t("v10.preparationPendingBody") : caseFile.reason ?? t("case.noPlanBody"),
      status: caseFile.decision?.preparation_pending ? "pending" : state === "failed" ? "failed" : state === "preparing" ? "active" : "pending",
    });
  }
  steps.push({
    id: "approval",
    kind: "approval",
    label: caseFile.reason === "product_selection_conflict" ? t("candidate.conflictTitle") : caseFile.decision?.selection_pending ? t("candidate.pendingTitle") : state === "awaiting_selection" ? t("candidate.awaiting") : caseFile.approval
      ? t("case.activityApproval")
      : t("case.activityAwait"),
    detail: caseFile.reason === "product_selection_conflict" ? t("candidate.conflictBody") : caseFile.approval
      ? caseFile.approval.issued_at ?? t("caseChat.citeApproval")
      : state === "rejected"
        ? t("case.state.rejected")
        : undefined,
    status:
      approvalRecorded(caseFile) || execution || state === "succeeded"
        ? "done"
        : state === "rejected" || caseFile.reason === "product_selection_conflict"
          ? "failed"
          : "pending",
  });
  if (execution?.status === "not_started") {
    steps.push({ id: "unstarted-cancelled", kind: "executor", label: t("v10.unstartedClosed"), detail: t("v10.unstartedReceipt"), status: "done" });
  } else if (execution) {
    steps.push({
      id: "apply",
      kind: "executor",
      label: t("case.activityApply"),
      detail: isExecutionUnknown(caseFile) ? t("v9.writeScopeUnknown") : t("case.writesCount", { count: execution.writes.length }),
      status: isExecutionUnknown(caseFile) ? "pending" : isExecutionSuccess(execution.status) ? "done" : "failed",
    });
    steps.push({
      id: "readback",
      kind: "readback",
      label: t("case.activityReadback"),
      detail: Array.isArray(execution.readback?.records) ? t("decision.receiptRecord", { count: execution.readback.records.length }) : t("decision.receiptReadbackMissing"),
      status: execution.readback?.execution_validated === true ? "done" : execution.readback?.execution_validated === false ? "failed" : "pending",
    });
    steps.push({
      id: "verify",
      kind: "verify",
      label: t("case.activityVerify"),
      detail: summary.label,
      status: summary.tone === "success" ? "done" : summary.tone === "failed" ? "failed" : "pending",
    });
  } else if (active) {
    steps.push({
      id: "apply",
      kind: "executor",
      label: t("case.activityApply"),
      status: "active",
    });
    steps.push({
      id: "readback",
      kind: "readback",
      label: t("case.activityReadback"),
      status: "pending",
    });
    steps.push({
      id: "verify",
      kind: "verify",
      label: t("case.activityVerify"),
      status: "pending",
    });
  } else if (state === "failed" && approvalRecorded(caseFile)) {
    steps.push({
      id: "apply",
      kind: "executor",
      label: t("case.activityApply"),
      detail: caseFile.reason ?? state,
      status: "failed",
    });
  }

  return {
    active,
    startedAt: base,
    hideDuration: !measured,
    durationLabel: t("case.executionDuration"),
    finishedAt: active ? undefined : finishedAt,
    title: t("case.activityTitle"),
    tone: "verify",
    steps,
  } satisfies ActivityState;
}

function citationList(caseFile: CaseFile, t: Translate): string[] {
  const plan = caseFile.plan;
  const execution = caseFile.execution;
  const summary = verifierSummary(execution, t, caseFile.result);
  const citations = [t("caseChat.citeCase", { id: shortId(caseFile.case_id) })];
  if (plan) {
    citations.push(t("caseChat.citePlan", { id: shortId(plan.plan_id) }));
    citations.push(
      t("caseChat.citePolicy", { id: shortId(plan.policy_sha256) }),
    );
  }
  if (caseFile.approval) {
    citations.push(
      t("caseChat.citeApproval"),
    );
  }
  if (execution) {
    citations.push(
      t("caseChat.citeExecution", { status: execution.status }),
    );
    citations.push(t("caseChat.citeVerifier", { summary: summary.label }));
  }
  return [...new Set(citations)];
}

export type CaseAnswer = {
  text: string;
  citations: string[];
  /** True when the answer came from case fields, not the optional server ask API. */
  local: boolean;
};

/**
 * Deterministic fallback used when ``POST /api/product/cases/{id}/ask`` is not
 * available (or returns nothing useful). It references concrete case fields
 * instead of producing a generic knowledge-base answer.
 */
export function localCaseAnswer(
  caseFile: CaseFile,
  question: string,
  t: Translate,
  demo = false,
  compose = true,
): CaseAnswer {
  const q = question.toLowerCase();
  const plan = caseFile.plan;
  const execution = caseFile.execution;
  const orders = orderCount(caseFile.order.ref);
  const citations = citationList(caseFile, t);
  const demoLine = demo ? ` ${t("caseChat.answerDemo")}` : "";
  const candidateLabel = (candidate: Pick<ProductPlan, "objective">) => t(candidate.objective === "preserve_quantities"
    ? "candidate.quantity" : candidate.objective === "min_planned_cost" ? "candidate.cost" : candidate.objective === "preserve_capacity" ? "candidate.capacity" : "candidate.plan");
  if (caseFile.reason === "product_selection_conflict" && !execution) {
    return { text: `${t("candidate.conflictBody")}${demoLine}`,
      citations: [t("caseChat.citeCase", { id: shortId(caseFile.case_id) })], local: true };
  }
  if (caseFile.decision?.selection_pending) {
    return { text: `${t("candidate.pendingBody")}${demoLine}`,
      citations: [t("caseChat.citeCase", { id: shortId(caseFile.case_id) })], local: true };
  }
  const pending = preparationPending(caseFile);
  if (pending) {
    const request = caseFile.decision?.revision_pending ? caseFile.revision_request : caseFile.repreparation_request;
    const refs = [t("caseChat.citeCase", { id: shortId(caseFile.case_id) })];
    if (request) refs.push(t("v10.requestedVersion", { version: request.version }));
    if (caseFile.decision?.revision_pending) refs.push(t("v10.oldPlanVersion", { version: caseFile.revision?.version ?? 1 }));
    if (caseFile.decision?.repreparation_pending) refs.push(t("v10.failedVersion", { version: caseFile.revision?.version ?? 1 }));
    return { text: `${t(pending.body)}${demoLine}`, citations: refs, local: true };
  }
  const docs = plan ? plan.actions.length : 0;
  const spend = planNewSpend(plan);
  const verifier = verifierSummaryText(execution, t, caseFile.result);
  const checks = effectiveChecks(caseFile, t);
  const counts = checkCounts(checks.checks);
  const checksSummary = checks.derived
    ? t("caseChat.answerChecksNone")
    : t("caseChat.answerChecksPass", {
        summary: t("decision.checkCounts", {
          pass: counts.pass,
          warn: counts.warn,
          block: counts.block,
        }),
      });
  const budgetLine = budgetCheckSummary(plan, t);
  const checksLine = [checksSummary, budgetAggregateNote(checks.checks, t), budgetLine].filter(Boolean).join(" ");

  const executionIntent =
    /execut|odoo|verif|score|write|readback|receipt|approv|执行|验收|写入|回读|批准|改了/.test(
      q,
    );
  const planIntent =
    /plan|proposal|changes|cancel|create|new|buy|margin|budget|policy|spend|purchase|cost|方案|计划|变更|为什么取消|新建|毛利|预算|政策|采购|支出|成本/.test(
      q,
    );
  const riskIntent =
    /risk|impact|due|late|order|unit|why|delivery|交付|风险|影响|订单|台|断供|延期/.test(
      q,
    );

  if (executionIntent && execution?.status === "not_started") {
    return { text: `${t("v10.unstartedClosed")}. ${t("v10.unstartedReceipt")}${demoLine}`, citations, local: true };
  }

  if (executionIntent && (isExecutionUnknown(caseFile) || caseFile.state === "executing")) {
    const status = isExecutionUnknown(caseFile) ? t("v9.executionUnknown") : t("v9.executionInProgress");
    return { text: `${status}. ${t("v9.writeScopeUnknown")}${demoLine}`, citations, local: true };
  }

  const chainIntent = impactIntent(question);
  const readinessIntent = /order readiness|ready date|date buffer|per-order|逐单|每单|齐货|交期余量|每张订单/.test(q);
  const comparisonIntent = /compar|candidate|alternative|option|selection|cheaper|比较|候选|取舍|选择|对比|更便宜/.test(q);
  const headroomIntent = /headroom|slack|risk|余量|缓冲|风险/.test(q);
  const choiceIntent = /selection reason|approval comment|选择理由|批准备注|备注/.test(q)
    || (/why|reason|为什么|为何|理由/.test(q) && /choose|chosen|selected|candidate|selection|选择|候选|取舍/.test(q));
  const budgetIntent = /budget|预算/.test(q);
  const actionIntent = /changes|changed|cancel|create|变更|取消|新建|改了/.test(q);
  const budgetOnly = budgetIntent && !comparisonIntent && !choiceIntent && !actionIntent && !executionIntent
    && !/plan|proposal|policy|spend|cost|margin|price|purchase|date|ready|delivery|capacity|risk|计划|方案|政策|支出|成本|毛利|采购|金额|齐货|交期|交付|产能|风险/.test(q);
  if (compose && (chainIntent || headroomIntent || choiceIntent || actionIntent || budgetIntent || [comparisonIntent, readinessIntent, planIntent, executionIntent].filter(Boolean).length > 1)) {
    const parts: CaseAnswer[] = [];
    const add = (query: string) => parts.push(localCaseAnswer(caseFile, query, t, false, false));
    if (executionIntent) add("execution");
    if (comparisonIntent) add("compare candidates");
    else if (planIntent && !budgetOnly) add("plan");
    else if (headroomIntent && !budgetOnly) add("impact");
    if (chainIntent) parts.push({ text: impactAnswer(plan, question, t), citations, local: true });
    if (readinessIntent && !budgetOnly) add("per-order ready date");
    const lines: string[] = [];
    if (headroomIntent && !comparisonIntent && !budgetOnly) lines.push(...(plan ? decisionFactLines(plan, t, "en") : [t("decisionFacts.unknown")]), t("decisionFacts.note"), t("decisionFacts.capacityNote"));
    if (budgetIntent) {
      if (!parts.some(part => part.text.includes(budgetLine))) lines.push(budgetLine);
      const budgets = plan?.decision_metrics?.order_budgets;
      if (!budgets) lines.push(t("decisionFacts.unknown"));
      else for (const row of budgets.slice(0, 12)) lines.push(row.order_ref + ": " + t("decisionFacts.salesValue") + " USD " + row.sales_value
        + "; " + t("decisionFacts.customerCap") + " USD " + row.customer_cap + "; " + t("decisionFacts.capHeadroom") + " USD " + row.headroom + ".");
      if (budgets && budgets.length > 12) lines.push(t("readiness.firstTwelve"));
      lines.push(t("decisionFacts.budgetNote"));
    }
    if (choiceIntent) {
      const reason = caseFile.approval?.plan_id === plan?.plan_id ? caseFile.approval?.comment?.trim() : null;
      lines.push(reason ? t("decisionFacts.choice") + ": " + reason : t("decisionFacts.choiceUnknown"));
    }
    if (actionIntent && plan) {
      lines.push(t("decisionFacts.actionsNote"));
      for (const action of plan.actions.slice(0, 12)) lines.push("- " + action.document + ": " + action.summary);
      if (plan.actions.length > 12) lines.push(t("decisionFacts.moreActions"));
    }
    if (lines.length) parts.push({ text: lines.join("\n\n"), citations, local: true });
    return { text: parts.map(part => part.text).join("\n\n") + demoLine,
      citations: [...new Set(parts.flatMap(part => part.citations))], local: true };
  }

  if (chainIntent && !executionIntent) return { text: impactAnswer(plan, question, t) + demoLine, citations, local: true };

  if (readinessIntent && !executionIntent && plan) {
    const data = plan.order_readiness;
    if (!data) return { text: t("readiness.unknown") + demoLine, citations, local: true };
    const lines = [t("readiness.title"), t("readiness.basis", { date: data.basis.anchor_date })];
    for (const row of data.orders.slice(0, 12)) lines.push(`- ${row.order_ref}: ${t("readiness.coverage")} ${row.covered_units}/${row.demand_units}; ${t("readiness.ready")} ${row.planned_ready_date ?? t("v9.unknown")}; ${t("readiness.due")} ${row.customer_due_date}; ${t("readiness.buffer")} ${row.buffer_days == null ? t("v9.unknown") : t(Math.abs(row.buffer_days) === 1 ? "readiness.day" : "readiness.days", { count: row.buffer_days })}.`);
    if (data.orders.length > 12) lines.push(t("readiness.firstTwelve"));
    lines.push(t("readiness.note"));
    return { text: lines.join("\n\n") + demoLine, citations, local: true };
  }

  const comparison = caseFile.comparison;

  if (comparison && !executionIntent && (comparisonIntent || !plan || comparison.status !== "ready")) {
    if (comparison.status !== "ready") return { text: `${t("candidate.unavailable")}${demoLine}`, citations, local: true };
    const chosen = comparison.plans.find(candidate => candidate.plan_id === comparison.selected_plan_id);
    const lines = [t("candidate.basis", { count: comparison.plans.length }),
      chosen ? t("candidate.selectedScope", { name: candidateLabel(chosen) }) : t("candidate.awaiting")];
    for (const candidate of comparison.plans) {
      lines.push(`- ${candidateLabel(candidate)}: ${t("decision.newSpend")} ${costMoney(candidate.costs?.planned.total, "en", t("v9.unknown")).replace("$", "USD ")}; ${t("candidate.changes")}: ${candidate.actions.length}.`);
      if (candidate.capacity) lines.push(`${t("candidate.manufactured")}: ${candidate.capacity.manufactured_units}; ${t("candidate.finishedPurchase")}: ${candidate.capacity.finished_purchase_units}; ${t("candidate.standardMinutes")}: ${candidate.capacity.standard_minutes ?? t("v9.unknown")}.`);
    }
    for (const candidate of comparison.plans) lines.push(candidateLabel(candidate) + ": " + decisionFactLines(candidate, t, "en").join(" "), budgetCheckSummary(candidate, t));
    if (comparison.plans.some(candidate => candidate.capacity)) lines.push(t("candidate.capacityNote"));
    for (const attempt of comparison.attempts) if (attempt.status !== "prepared") lines.push(`${candidateLabel(attempt)}: ${t(attempt.status === "duplicate" ? "candidate.duplicate" : attempt.status === "infeasible" ? "candidate.infeasible" : "candidate.rejected")}`);
    lines.push(t("candidate.preferenceNote"), t("cost.deltaNote"));
    lines.push(t(["awaiting_selection", "awaiting_approval"].includes(caseFile.state) && !caseFile.approval && !execution ? "candidate.selectNote" : "candidate.recordedNote"));
    return { text: lines.join("\n\n") + demoLine, citations: [...citations, t("v9.planVersion", { version: comparison.version })], local: true };
  }

  if (!plan && caseFile.decision?.preparation_pending) {
    return { text: `${t("v10.preparationPendingBody")}${demoLine}`, citations, local: true };
  }

  if (!plan) {
    return {
      text: `${planIntent || executionIntent ? t("caseChat.answerNoPlan") : t("caseChat.answerCaseFacts", {
        disruption: exceptionText(caseFile, t),
        impact: disruptionScopeLines(plan, t).join(" "),
        product: caseFile.order.product,
        units: formatQuantity(caseFile.order.units),
        orders: orders ?? t("v9.unknown"),
        due: dueWindow(caseFile, t),
        state: stateLabel(caseFile.state, t),
      })}${demoLine}`,
      citations,
      local: true,
    };
  }

  if (executionIntent) {
    if (!execution) {
      return {
        text: `${t("caseChat.answerNoExecution", {
          plan: shortId(plan.plan_id),
        })}${demoLine}`,
        citations,
        local: true,
      };
    }
    return {
      text: `${t("caseChat.answerExecution", {
        writes: execution.writes.length,
        records: execution.readback?.records?.length ?? t("runs.notRecorded"),
        steps: execution.readback?.verified_steps ?? t("runs.notRecorded"),
        verifier,
        plan: shortId(plan.plan_id),
      })}${originalScoreThresholdMiss(execution) ? " " + t("case.scoreThresholdFailure") : ""}${demoLine}`,
      citations,
      local: true,
    };
  }

  if (planIntent) {
    return {
      text: `${plan.objective ? t("candidate.selectedScope", { name: candidateLabel(plan) }) + " \n\n" : ""}${t("caseChat.answerPlan", {
        actions: actionCountsLabel(plan.actions, t),
        spend: formatCurrency(spend),
        margin: marginValue(plan, t),
        marginLabel: marginLabel(plan, t),
        marginNote: t(hasNewSpendBasis(plan) ? "decision.marginBasis" : "decision.marginLegacyNote"),
        checks: checksLine,
      })}${demoLine}`,
      citations,
      local: true,
    };
  }

  if (riskIntent) {
    return {
      text: `${t("caseChat.answerImpact", {
        disruption: exceptionText(caseFile, t),
        impact: disruptionScopeLines(plan, t).join(" "),
        product: caseFile.order.product,
        units: formatQuantity(caseFile.order.units),
        orders: orders ?? t("v9.unknown"),
        due: dueWindow(caseFile, t),
        documents: docs,
      })}${demoLine}`,
      citations,
      local: true,
    };
  }

  return {
    text: `${t("caseChat.answerSummary", {
      caseId: shortId(caseFile.case_id),
      disruption: exceptionText(caseFile, t),
      impact: disruptionScopeLines(plan, t).join(" "),
      product: caseFile.order.product,
      units: formatQuantity(caseFile.order.units),
      due: dueWindow(caseFile, t),
      plan: shortId(plan.plan_id),
      actions: plan.actions.length,
      state: stateLabel(caseFile.state, t),
    })}${demoLine}`,
    citations,
    local: true,
  };
}

export function stepStatusIcon(status: StepState): string {
  if (status === "done") return "✓";
  if (status === "failed") return "!";
  if (status === "active") return "●";
  return "○";
}

export function uniqueActionDocuments(caseFile: CaseFile): number {
  if (!caseFile.plan) return 0;
  return new Set(caseFile.plan.actions.map((action) => action.document)).size;
}
