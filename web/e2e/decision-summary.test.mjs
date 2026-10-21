import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const path = process.env.FORGEFLOW_DECISION_MODULE ?? fileURLToPath(new URL("../src/decisionSummary.ts", import.meta.url));
const code = stripTypeScriptTypes(readFileSync(path, "utf8")).replace('"./costAmounts"', JSON.stringify(pathToFileURL(join(dirname(path), "costAmounts.ts")).href));
const { planningAttention, decisionFactLines } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
const plan = () => ({ on_time: true, checks: [{ status: "pass" }], decision_metrics: {
  margin: { cost_headroom: "730" }, order_budgets: [{ headroom: "1000" }],
  workcenters: [{ headroom_minutes: "70" }], readiness: { late_orders: 0, uncovered_orders: 0,
    zero_buffer_orders: 0, unknown_ready_orders: 0 } } });
test("attention follows recorded constraints, without order-volume or due-day heuristics", () => {
  assert.equal(planningAttention(plan()), "low");
  assert.equal(planningAttention({ ...plan(), units: 100000, due_days: 0 }), "low");
  for (const field of ["late_orders", "uncovered_orders"]) {
    const value = plan(); value.decision_metrics.readiness[field] = 1;
    assert.equal(planningAttention(value), "high");
  }
  const negative = plan(); negative.decision_metrics.margin.cost_headroom = "-0.001";
  assert.equal(planningAttention(negative), "high");
  const zero = plan(); zero.decision_metrics.readiness.zero_buffer_orders = 1;
  assert.equal(planningAttention(zero), "medium");
});
test("missing measurements never become a low-risk conclusion", () => {
  assert.equal(planningAttention(null), "unknown");
  assert.equal(planningAttention({ on_time: true }), "unknown");
  const missing = plan(); missing.decision_metrics.workcenters[0].headroom_minutes = null;
  assert.equal(planningAttention(missing), "unknown");
  missing.decision_metrics.readiness.zero_buffer_orders = 1;
  assert.equal(planningAttention(missing), "medium"); // Known zero is still actionable; other facts remain unknown.
  missing.checks = [{ status: "block" }];
  assert.equal(planningAttention(missing), "high");
});

test("currency prose uses one currency code in both supported locales", () => {
  const t = (key, values) => key + " " + JSON.stringify(values ?? {});
  for (const locale of ["zh", "en"]) {
    const lines = decisionFactLines(plan(), t, locale);
    assert.ok(lines.some(line => line.includes("USD 730.00")));
    assert.ok(lines.every(line => !line.includes("USUSD") && !line.includes("$")));
  }
});
