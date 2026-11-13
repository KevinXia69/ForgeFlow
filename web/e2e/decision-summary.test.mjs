import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
const path = process.env.FORGEFLOW_DECISION_MODULE ?? fileURLToPath(new URL("../src/decisionSummary.ts", import.meta.url));
const code = stripTypeScriptTypes(readFileSync(path, "utf8")).replace('"./costAmounts"', JSON.stringify(pathToFileURL(join(dirname(path), "costAmounts.ts")).href));
const { planningAttention, decisionFactLines, currentDecisionMetrics, marginValue, marginLabel, retentionFactLines } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
const plan = () => ({ plan_id: "p", snapshot_sha256: "s", margin_basis: "new_spend_coverage_v2", on_time: true, checks: [{ status: "pass" }], decision_metrics: { schema_version: 2, basis: { plan_id: "p", snapshot_sha256: "s", margin_basis: "new_spend_coverage_v2" },
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


test("legacy metrics never acquire corrected compliance or cost headroom", () => {
  const record = plan(); record.margin = 80; record.decision_metrics.schema_version = 1;
  delete record.margin_basis; delete record.decision_metrics.basis.margin_basis;
  const before = structuredClone(record), t = (key) => key;
  assert.equal(currentDecisionMetrics(record), null);
  assert.equal(planningAttention(record), "unknown");
  assert.equal(marginLabel(record, t), "decision.marginLegacy");
  assert.equal(marginValue(record, t), "80.00%");
  assert.ok(decisionFactLines(record, t, "en").includes("decision.marginLegacyNote"));
  assert.ok(!decisionFactLines(record, t, "en").includes("decisionFacts.cost"));
  assert.deepEqual(record, before);
});

test("v2 coverage preserves denominator, all-stock N/A and plan binding", () => {
  const record = plan(), t = (key, values = {}) => key + " " + JSON.stringify(values);
  record.margin = 76.74981077163956;
  Object.assign(record.decision_metrics.margin, { sales_value: "23980.5", new_spend_revenue: "20623.23",
    new_spend_units: 43, stock_units: 7, planned_cost: "4794.94", planned_margin_pct: "76.74981077163956" });
  assert.equal(marginValue(record, t), "76.75%");
  const coverage = decisionFactLines(record, t, "en").find(line => line.startsWith("decisionFacts.coverage"));
  assert.match(coverage, /20,623.23/); assert.doesNotMatch(coverage, /23,980.50/);
  record.decision_metrics.margin.new_spend_revenue = "0"; record.decision_metrics.margin.planned_cost = "0";
  assert.match(marginValue(record, t), /marginNotApplicable/);
  record.decision_metrics.basis.plan_id = "other";
  assert.equal(currentDecisionMetrics(record), null);
  assert.deepEqual(decisionFactLines(record, key => key, "en"), ["decisionFacts.unknown"]);
});


test("optional retention metadata distinguishes completed, skipped and incomplete experiments", () => {
  const record = plan(), t = (key, values = {}) => key + " " + JSON.stringify(values);
  record.retention_refinement = { profile: "fixed-totals-retention-v1", status: "improved", retained_before: 4, retained_after: 6, reason: "validated_improvement" };
  assert.ok(retentionFactLines(record, t)[0].includes('"after":6'));
  assert.match(retentionFactLines(record, t).at(-1), /retention.limit/);
  record.retention_refinement = { ...record.retention_refinement, status: "fallback", retained_after: 4, reason: "budget_exhausted" };
  assert.ok(retentionFactLines(record, t).some(line => line.startsWith("retention.timeout")));
  assert.ok(!retentionFactLines(record, t).some(line => line.startsWith("retention.improved")));
  record.retention_refinement.status = "skipped"; record.retention_refinement.reason = "non_repair_objective";
  assert.ok(retentionFactLines(record, t).some(line => line.startsWith("retention.nonRepair")));
  record.retention_refinement.profile = "unknown-profile";
  assert.deepEqual(retentionFactLines(record, t), []);
});
