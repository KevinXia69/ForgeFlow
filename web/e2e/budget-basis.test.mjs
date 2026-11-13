/** Actual frontend helpers and dictionaries; no browser, ERP, provider, or network. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "../node_modules/typescript/lib/typescript.js";

const directory = resolve(process.env.FORGEFLOW_BUDGET_SRC ?? fileURLToPath(new URL("../src", import.meta.url)));
const cache = new Map();
function moduleUrl(path) {
  if (cache.has(path)) return cache.get(path);
  let code = stripTypeScriptTypes(readFileSync(path, "utf8"));
  code = code.replace(/from\s+(["'])(\.\.?\/[^"']+)\1/g, (_match, _quote, relative) =>
    "from " + JSON.stringify(moduleUrl(resolve(dirname(path), relative + (relative.endsWith(".ts") ? "" : ".ts")))));
  const url = "data:text/javascript;base64," + Buffer.from(code).toString("base64");
  cache.set(path, url);
  return url;
}
const { budgetCheckBasis, effectiveChecks, policyCheckValue, checkCounts, budgetAggregateNote, localCaseAnswer } =
  await import(moduleUrl(resolve(directory, "caseUi.ts")));
const i18n = ts.createSourceFile("i18n.tsx", readFileSync(resolve(directory, "i18n.tsx"), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const translations = {};
for (const statement of i18n.statements) {
  if (!ts.isVariableStatement(statement)) continue;
  for (const declaration of statement.declarationList.declarations) {
    const name = declaration.name.getText(i18n);
    if (!["zh", "en"].includes(name)) continue;
    const code = stripTypeScriptTypes("export default " + declaration.initializer.getText(i18n) + ";");
    translations[name] = (await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"))).default;
  }
}
const translator = lang => (key, values = {}) => {
  assert.ok(key in translations[lang], `Missing ${lang} translation: ${key}`);
  return translations[lang][key].replace(/\{(\w+)\}/g, (match, name) => values[name] === undefined ? match : String(values[name]));
};
const planId = "a".repeat(64), snapshot = "b".repeat(64);
const legacySource = "solver planned cost compared with summed sales-order customer budget caps; per-order caps remain policy checks, not purchase authorization";
const check = (changes = {}) => ({ code: "budget", label: "Planned cost versus customer budget caps", status: "pass",
  actual: "USD 50.00", threshold: "USD 1000.00", source: legacySource, ...changes });
const basis = () => ({ kind: "sales_order_value_per_customer_cap_v1", plan_id: planId, snapshot_sha256: snapshot });
function caseRecord(budget = check()) {
  return { case_id: "c".repeat(32), state: "awaiting_approval", scenario: "A", synthetic: false,
    order: { ref: "SO-1..5", units: 50, due_days: 8, product: "Product", customer: "Customer" },
    disruption: { kind: "supplier_cancellation", vendor: "Vendor" }, approval: null, execution: null,
    plan: { plan_id: planId, snapshot_sha256: snapshot, policy_sha256: "d".repeat(64), prepared_at: "2026-10-23T00:00:00Z",
      actions: [], checks: [budget], new_spend: 50, margin: 95, on_time: true, cost_delta: null,
      decision_metrics: { basis: { plan_id: planId, snapshot_sha256: snapshot },
        order_budgets: [{ order_ref: "S00001", sales_value: "200", customer_cap: "250", headroom: "50" }],
        margin: { planned_margin_pct: "95", effective_floor_pct: "20", headroom_pp: "75", cost_headroom: "750" },
        workcenters: [], readiness: { order_count: 5, zero_buffer_orders: 0, late_orders: 0, uncovered_orders: 0, unknown_ready_orders: 0 } } },
  };
}

for (const lang of Object.keys(translations)) {
  const t = translator(lang);
  test(`${lang}: only an exact same-plan basis identifies the per-order count`, () => {
    const record = caseRecord(check({ basis: basis(), label: "Any label", source: "Any description", actual: 5, threshold: 5 }));
    const before = structuredClone(record);
    const [shown] = effectiveChecks(record, t).checks;
    assert.equal(shown.budgetBasis, "per_order");
    assert.equal(shown.label, t("budgetCheck.perOrder"));
    assert.equal(policyCheckValue(shown, t), t("budgetCheck.count", { actual: 5, threshold: 5 }));
    assert.equal(shown.status, "pass");
    assert.equal(shown.actual, 5);
    assert.equal(shown.threshold, 5);
    assert.deepEqual(record, before);
    for (const bad of [{ ...basis(), plan_id: "e".repeat(64) }, { ...basis(), snapshot_sha256: "f".repeat(64) },
      { ...basis(), kind: "unknown" }, { ...basis(), extra: true }]) {
      assert.equal(budgetCheckBasis(check({ basis: bad, actual: 5, threshold: 5 }), record.plan), "unrecorded");
    }
  });
  test(`${lang}: unknown counts remain unknown and a recorded block remains blocked`, () => {
    for (const threshold of [5, null]) {
      const record = caseRecord(check({ basis: basis(), actual: null, threshold, status: "unknown" }));
      const [shown] = effectiveChecks(record, t).checks;
      assert.equal(shown.budgetBasis, "per_order");
      assert.equal(shown.actual, null);
      assert.equal(shown.status, "unknown");
      assert.equal(policyCheckValue(shown, t), t("budgetCheck.count", { actual: t("v9.unknownResults"), threshold: threshold ?? t("v9.unknownResults") }));
    }
    const [blocked] = effectiveChecks(caseRecord(check({ basis: basis(), actual: 3, threshold: 5, status: "block" })), t).checks;
    assert.equal(blocked.status, "block");
    assert.equal(policyCheckValue(blocked, t), t("budgetCheck.count", { actual: 3, threshold: 5 }));
  });
  test(`${lang}: explicit malformed basis or counts never acquire per-order or legacy semantics`, () => {
    const record = caseRecord();
    for (const changes of [{ basis: null }, { basis: undefined }, { basis: basis(), actual: "5", threshold: 5 },
      { basis: basis(), actual: 4, threshold: 5 }, { basis: basis(), actual: 0, threshold: 0 },
      { basis: basis(), actual: 129, threshold: 129 }, { basis: basis(), actual: true, threshold: 1 },
      { basis: basis(), actual: 5, threshold: 5, status: "block" },
      { basis: basis(), actual: 0, threshold: 5, status: "unknown" }]) {
      const source = check(changes), before = structuredClone(source);
      assert.equal(budgetCheckBasis(source, record.plan), "unrecorded");
      assert.deepEqual(source, before);
    }
  });
  test(`${lang}: legacy source, unknown source, and labels cannot upgrade from current metrics`, () => {
    const record = caseRecord();
    const before = structuredClone(record);
    const info = effectiveChecks(record, t);
    assert.equal(info.checks[0].budgetBasis, "legacy_total");
    assert.equal(info.checks[0].label, t("budgetCheck.legacy"));
    assert.equal(info.checks[0].source, t("budgetCheck.legacyNote"));
    assert.equal(policyCheckValue(info.checks[0], t), "USD 50.00 / USD 1000.00");
    assert.deepEqual(checkCounts(info.checks), { pass: 1, warn: 0, block: 0, planned: 0 });
    assert.equal(budgetAggregateNote(info.checks, t), t("budgetCheck.aggregateLegacy"));
    assert.deepEqual(record, before);
    for (const source of [null, "unrecognized", legacySource + " "]) {
      const unknown = effectiveChecks(caseRecord(check({ label: t("budgetCheck.perOrder"), source, actual: 5, threshold: 5 })), t);
      assert.equal(unknown.checks[0].budgetBasis, "unrecorded");
      assert.equal(unknown.checks[0].source, t("budgetCheck.unrecordedNote"));
      assert.equal(unknown.checks[0].status, "pass");
      assert.equal(budgetAggregateNote(unknown.checks, t), t("budgetCheck.aggregateUnknown"));
    }
  });
  test(`${lang}: ordinary, mixed, budget-only, and comparison answers preserve budget provenance`, () => {
    for (const question of ["plan", "cost and budget", "customer budget headroom"]) {
      const answer = localCaseAnswer(caseRecord(), question, t).text;
      assert.ok(answer.includes(t("budgetCheck.legacyNote")), question);
      assert.ok(!answer.includes(t("budgetCheck.perOrder")), question);
      if (question.includes("budget")) {
        assert.ok(answer.includes("S00001"));
        assert.ok(answer.includes("USD 200"));
        assert.ok(answer.includes(t("decisionFacts.budgetNote")));
      } else assert.ok(answer.includes(t("budgetCheck.aggregateLegacy")));
    }
    const known = caseRecord(check({ basis: basis(), actual: 5, threshold: 5 }));
    assert.ok(localCaseAnswer(known, "plan", t).text.includes(t("budgetCheck.count", { actual: 5, threshold: 5 })));
    const unknown = caseRecord(check({ source: "not recorded" }));
    assert.ok(localCaseAnswer(unknown, "plan", t).text.includes(t("budgetCheck.aggregateUnknown")));
    assert.ok(localCaseAnswer(unknown, "budget", t).text.includes(t("budgetCheck.unrecordedNote")));
    const comparison = caseRecord();
    comparison.comparison = { status: "ready", version: 1, selected_plan_id: planId, plans: [comparison.plan], attempts: [] };
    assert.ok(localCaseAnswer(comparison, "compare candidates", t).text.includes(t("budgetCheck.legacyNote")));
  });
  test(`${lang}: absent server checks do not substitute planned cost for a budget measurement`, () => {
    const record = caseRecord();
    delete record.plan.checks;
    const info = effectiveChecks(record, t);
    const budget = info.checks.find(row => row.code === "budget");
    assert.equal(info.derived, true);
    assert.equal(budget.actual, null);
    assert.equal(budget.status, "unknown");
    assert.equal(budget.budgetBasis, "unrecorded");
  });
}
