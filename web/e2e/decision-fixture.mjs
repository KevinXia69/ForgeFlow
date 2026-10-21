/** Invented HTTP display records only; not solver, ERP or verifier evidence. */
import { candidateCase } from "./candidates-fixture.mjs";
export function decisionCase() {
  const value = candidateCase({ capacity: true });
  value.order.ref = "SO-FIXTURE";
  for (const [index, plan] of value.comparison.plans.entries()) {
    if (!index) continue; // Legacy values remain visibly unknown.
    const cost = Number(plan.costs.planned.total), buffer = index === 2 ? 1 : 0;
    plan.margin = 100 - cost / 10;
    plan.checks[0].actual = String(plan.margin) + "%";
    plan.order_readiness = { schema_version: 1, basis: { plan_id: plan.plan_id, snapshot_sha256: plan.snapshot_sha256,
      anchor_date: "2026-10-23", time_basis: "planned_supply_availability_days", quantity_scope: "target_product" },
      orders: [{ order_ref: "SO-FIXTURE", demand_units: 10, covered_units: 10, customer_due_day: 8,
        customer_due_date: "2026-10-31", planned_ready_day: 8 - buffer, planned_ready_date: buffer ? "2026-10-30" : "2026-10-31",
        buffer_days: buffer, stock_units: 0, manufactured_units: 10, purchased_units: 0, status: "on_time" }] };
    plan.decision_metrics = { schema_version: 1, basis: { plan_id: plan.plan_id, snapshot_sha256: plan.snapshot_sha256,
      currency: "USD", pricing_sha256: plan.costs.basis.pricing_sha256,
      cost_basis: "recorded_unit_price_and_manufacturing_standard", capacity_basis: "planning_horizon_standard_minutes" },
      margin: { sales_value: "1000", planned_cost: String(cost), original_floor_pct: "20", effective_floor_pct: "20",
        planned_margin_pct: String(plan.margin), headroom_pp: String(plan.margin - 20),
        max_planning_cost: "800", cost_headroom: String(800 - cost) },
      order_budgets: [{ order_ref: "SO-FIXTURE", sales_value: "1000", customer_cap: "2000", headroom: "1000" }],
      workcenters: [{ code: plan.capacity.workcenters[0].code, used_standard_minutes: String(plan.capacity.standard_minutes),
        horizon_limit_minutes: "100", headroom_minutes: String(100 - plan.capacity.standard_minutes) }],
      readiness: { order_count: 1, zero_buffer_orders: buffer ? 0 : 1, late_orders: 0,
        uncovered_orders: 0, unknown_ready_orders: 0, minimum_buffer_days: buffer } };
  }
  return value;
}
