import type { ProductPlan } from "./types";
import type { Lang, Translate } from "./i18n";
import { costMoney } from "./costAmounts";

export type PlanningAttention = "high" | "medium" | "low" | "unknown";
const number = (value: string | null | undefined) => value == null || !Number.isFinite(Number(value)) ? null : Number(value);

export function planningAttention(plan: ProductPlan | null | undefined): PlanningAttention {
  if (!plan) return "unknown";
  if (plan.on_time === false || plan.checks?.some(check => check.status === "block")) return "high";
  const facts = plan.decision_metrics;
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
  const data = plan.decision_metrics;
  if (!data) return [t("decisionFacts.unknown")];
  const unknown = t("v9.unknown"), margin = data.margin, dates = data.readiness;
  const lines = [
    t("decisionFacts.margin", { margin: margin.planned_margin_pct ?? unknown, floor: margin.effective_floor_pct,
      headroom: margin.headroom_pp ?? unknown }),
    t("decisionFacts.cost", { amount: costMoney(margin.cost_headroom, lang, unknown).replace(/(?:US)?\$/, "USD ") }),
    t("decisionFacts.dates", { total: dates.order_count, zero: dates.zero_buffer_orders, late: dates.late_orders,
      uncovered: dates.uncovered_orders, unknown: dates.unknown_ready_orders }),
  ];
  for (const row of data.workcenters.slice(0, 12)) lines.push(row.code + ": " + t("decisionFacts.used") + " " + (row.used_standard_minutes ?? unknown)
    + "; " + t("decisionFacts.limit") + " " + (row.horizon_limit_minutes ?? unknown)
    + "; " + t("decisionFacts.remaining") + " " + (row.headroom_minutes ?? unknown) + ".");
  return lines;
}
