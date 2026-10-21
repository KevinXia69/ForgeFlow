import type { CaseFile, ProductPlan } from "./types";

/** Use recorded calendar deadlines, never count down from the browser clock. */
export function recordedDueRange(caseFile: CaseFile): { earliest: string; latest: string } | null {
  const plans: ProductPlan[] = caseFile.plan ? [caseFile.plan]
    : caseFile.comparison?.status === "ready" ? caseFile.comparison.plans : [];
  if (!plans.length) return null;
  const deadlines = plans.map(plan => {
    const value = plan.order_readiness;
    if (!value || value.basis.plan_id !== plan.plan_id || value.basis.snapshot_sha256 !== plan.snapshot_sha256
      || !value.orders.length) return null;
    const rows = value.orders.map(row => `${row.order_ref}:${row.customer_due_date}`).sort();
    if (value.orders.some(row => !/^\d{4}-\d{2}-\d{2}$/.test(row.customer_due_date))) return null;
    return rows;
  });
  if (deadlines.some(rows => rows === null || JSON.stringify(rows) !== JSON.stringify(deadlines[0]))) return null;
  const dates = plans[0].order_readiness!.orders.map(row => row.customer_due_date).sort();
  return { earliest: dates[0], latest: dates[dates.length - 1] };
}
