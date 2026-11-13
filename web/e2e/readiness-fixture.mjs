/** Invented order readiness for manual UI checks; never ERP or verifier evidence. */
export function addReadinessPreview(value) {
  value.order = { ...value.order, ref: "SO-1..2", units: 10, customer: "2 invented sales orders" };
  for (const [index, plan] of value.comparison.plans.entries()) {
    if (index === 0) continue; // Legacy plan stays unknown.
    plan.order_readiness = { schema_version: 1,
      basis: { plan_id: plan.plan_id, snapshot_sha256: plan.snapshot_sha256,
        anchor_date: "2026-01-01", time_basis: "planned_supply_availability_days", quantity_scope: "target_product" },
      orders: Array.from({ length: 2 }, (_, i) => ({ order_ref: `SO-${i + 1}`, demand_units: 5,
        covered_units: 5, customer_due_day: 8, customer_due_date: "2026-01-09",
        planned_ready_day: i === 0 ? 8 : 7, planned_ready_date: i === 0 ? "2026-01-09" : "2026-01-08",
        buffer_days: i === 0 ? 0 : 1, stock_units: 0, manufactured_units: 5, purchased_units: 0, status: "on_time" })) };
  }
  return value;
}
