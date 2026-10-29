import type { ProductPlan } from "./types";
import type { Lang, Translate, MessageKey } from "./i18n";
import { costMoney } from "./costAmounts";

/** Historical metrics retain their original values, but cannot establish current compliance. */
export function hasNewSpendBasis(plan: ProductPlan | null | undefined): boolean {
  return plan?.margin_basis === "new_spend_coverage_v2";
}
export function boundDecisionMetrics(plan: ProductPlan | null | undefined) {
  const data = plan?.decision_metrics;
  return plan && data && data.basis?.plan_id === plan.plan_id
    && data.basis.snapshot_sha256 === plan.snapshot_sha256 ? data : null;
}
export function currentDecisionMetrics(plan: ProductPlan | null | undefined) {
  const data = boundDecisionMetrics(plan);
  return hasNewSpendBasis(plan) && data?.schema_version === 2
    && data.basis.margin_basis === "new_spend_coverage_v2" ? data : null;
}
export function marginLabel(plan: ProductPlan | null | undefined, t: Translate): string {
  return t(hasNewSpendBasis(plan) ? "decision.margin" : "decision.marginLegacy");
}
export function marginValue(plan: ProductPlan | null | undefined, t: Translate): string {
  const data = currentDecisionMetrics(plan);
  if (data?.margin.new_spend_revenue === "0" && data.margin.planned_cost === "0") return t("decision.marginNotApplicable");
  return typeof plan?.margin === "number" && Number.isFinite(plan.margin) ? `${plan.margin.toFixed(2)}%` : t("v9.unknown");
}

export function retentionFactLines(plan: ProductPlan, t: Translate): string[] {
  const data = plan.retention_refinement;
  if (!data || data.profile !== "fixed-totals-retention-v1"
    || !["improved", "unchanged", "fallback", "skipped"].includes(data.status)
    || !Number.isInteger(data.retained_before) || !Number.isInteger(data.retained_after)
    || data.retained_before < 0 || data.retained_after < 0) return [];
  const reasons: Record<string, MessageKey> = {
    non_repair_objective: "retention.nonRepair", original_optima_not_proven: "retention.unproven",
    budget_exhausted: "retention.timeout", refinement_optimum_not_proven: "retention.unproven",
    unsupported_numeric_precision: "retention.unsupported", unsupported_manufacturing_scope: "retention.unsupported",
    ambiguous_component_offer: "retention.unsupported", fixed_objectives_changed: "retention.validation",
    retention_mapping_not_proven: "retention.unproven", refinement_failed_validation_or_solver: "retention.validation",
  };
  return [t(`retention.${data.status}` as MessageKey, { before: data.retained_before, after: data.retained_after }),
    ...(reasons[data.reason] ? [t(reasons[data.reason])] : []), t("retention.limit")];
}

export type PlanningAttention = "high" | "medium" | "low" | "unknown";
const number = (value: string | null | undefined) => value == null || !Number.isFinite(Number(value)) ? null : Number(value);

export function planningAttention(plan: ProductPlan | null | undefined): PlanningAttention {
  if (!plan) return "unknown";
  if (plan.on_time === false || plan.checks?.some(check => check.status === "block")) return "high";
  const facts = currentDecisionMetrics(plan);
  if (!facts) return "unknown";
  const dates = facts.readiness;
  const amounts = [facts.margin.cost_headroom, ...facts.order_budgets.map(row => row.headroom),
    ...facts.workcenters.map(row => row.headroom_minutes)].map(number);
  if (dates.late_orders || dates.uncovered_orders || amounts.some(value => value !== null && value < 0)) return "high";
  if (dates.zero_buffer_orders || amounts.some(value => value === 0)) return "medium";
  if (dates.unknown_ready_orders || amounts.some(value => value === null) || plan.checks?.some(check => check.status === "unknown")) return "unknown";
  return "low";
}

export function decisionFactLines(plan: ProductPlan, t: Translate, lang: Lang): string[] {
  const data = boundDecisionMetrics(plan);
  if (!data) return [t("decisionFacts.unknown")];
  const unknown = t("v9.unknown"), margin = data.margin, dates = data.readiness;
  const current = currentDecisionMetrics(plan);
  const lines = current ? [
    ...(margin.new_spend_revenue === "0" && margin.planned_cost === "0" ? [t("decision.marginNotApplicable")] : [t("decisionFacts.margin", { margin: margin.planned_margin_pct ?? unknown, floor: margin.effective_floor_pct,
      headroom: margin.headroom_pp ?? unknown })]),
    t("decisionFacts.coverage", { units: margin.new_spend_units ?? unknown, stock: margin.stock_units ?? unknown, revenue: costMoney(margin.new_spend_revenue, lang, unknown) }),
    t("decisionFacts.cost", { amount: costMoney(margin.cost_headroom, lang, unknown).replace(/(?:US)?\$/, "USD ") }),
  ] : [t("decision.marginLegacyNote")];
  lines.push(t("decisionFacts.dates", { total: dates.order_count, zero: dates.zero_buffer_orders, late: dates.late_orders,
      uncovered: dates.uncovered_orders, unknown: dates.unknown_ready_orders }));
  for (const row of data.workcenters.slice(0, 12)) lines.push(row.code + ": " + t("decisionFacts.used") + " " + (row.used_standard_minutes ?? unknown)
    + "; " + t("decisionFacts.limit") + " " + (row.horizon_limit_minutes ?? unknown)
    + "; " + t("decisionFacts.remaining") + " " + (row.headroom_minutes ?? unknown) + ".");
  return lines;
}
