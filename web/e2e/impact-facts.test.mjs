/** Pure public-view helpers: no browser, ERP, provider, or private artifact reads. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "../node_modules/typescript/lib/typescript.js";
const directory = resolve(process.env.FORGEFLOW_IMPACT_SRC ?? fileURLToPath(new URL("../src", import.meta.url)));
const cache = new Map();
function moduleUrl(path) {
  if (cache.has(path)) return cache.get(path);
  let code = stripTypeScriptTypes(readFileSync(path, "utf8"));
  code = code.replace(/from\s+(["'])(\.\.?\/[^"']+)\1/g, (_match, _quote, relative) =>
    "from " + JSON.stringify(moduleUrl(resolve(dirname(path), relative + (relative.endsWith(".ts") ? "" : ".ts")))));
  const url = "data:text/javascript;base64," + Buffer.from(code).toString("base64");
  cache.set(path, url); return url;
}
const helpers = await import(moduleUrl(resolve(directory, "impactFacts.ts")));
const { localCaseAnswer } = await import(moduleUrl(resolve(directory, "caseUi.ts")));
const source = ts.createSourceFile("i18n.tsx", readFileSync(resolve(directory, "i18n.tsx"), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const translations = {};
for (const statement of source.statements) if (ts.isVariableStatement(statement)) for (const declaration of statement.declarationList.declarations) {
  const name = declaration.name.getText(source);
  if (!["zh", "en"].includes(name)) continue;
  translations[name] = (await import("data:text/javascript;base64," + Buffer.from(stripTypeScriptTypes("export default " + declaration.initializer.getText(source) + ";")).toString("base64"))).default;
}
const translator = lang => (key, values = {}) => {
  assert.ok(key in translations[lang], `Missing ${lang} translation: ${key}`);
  return translations[lang][key].replace(/\{(\w+)\}/g, (match, name) => values[name] === undefined ? match : String(values[name]));
};
const id = "a".repeat(64), snapshot = "b".repeat(64);
const sourceRef = (record, fields) => ({ projection: "source_facts", artifact: "change_set", record, fields });
function examplePlan() {
  const node = (key, kind, phase, label, product_code, quantity, facts = {}, action = null) =>
    ({ key, kind, phase, label, product_code, quantity, unit: product_code ? "units" : null, action, facts, source_refs: [] });
  const edge = (key, from, to, phase, relation, product_code, quantity) => ({ key, from, to, phase, relation, product_code, quantity,
    quantity_basis: phase === "baseline" ? "recorded_scope_quantity_unknown" : relation === "unused_supply" ? "unallocated_supply" : "exact_plan_allocation",
    available_day: phase === "planned" ? 4 : null, required_day: relation === "component_allocation" ? 4 : null, source_refs: [] });
  const ready = n => ({ order_ref: `S0000${n}`, demand_units: 5, covered_units: 5, customer_due_day: 8,
    customer_due_date: "2026-11-01", planned_ready_day: 4, planned_ready_date: "2026-10-28", buffer_days: 4,
    stock_units: 0, manufactured_units: 5, purchased_units: 0, status: "on_time" });
  const nodes = [node("disruption", "disruption", "baseline", "Vendor A", null, null),
    node("baseline:po", "purchase_order", "baseline", "P00001", "RAW", 12, {}, "cancel"),
    node("baseline:mo", "manufacturing_order", "baseline", "M00001", "FIN", 10),
    node("order:1", "sales_order", "shared", "S00001", "FIN", 5),
    node("order:2", "sales_order", "shared", "S00002", "FIN", 5),
    node("planned:new:purchase.order:2", "purchase_order", "planned", "Planned purchase 2", "RAW", 14, { document_ref: null }, "create"),
    node("planned:mo", "manufacturing_order", "planned", "M00001", "FIN", 10, {}, "keep"),
    node("unallocated:po", "unallocated_supply", "planned", "Unallocated RAW", "RAW", 2)];
  const edges = [edge("d", "disruption", "baseline:po", "baseline", "disruption_scope", "RAW", null),
    edge("o", "baseline:po", "baseline:mo", "baseline", "origin_scope", "RAW", null),
    edge("o1", "baseline:mo", "order:1", "baseline", "origin_scope", "FIN", null),
    edge("component", "planned:new:purchase.order:2", "planned:mo", "planned", "component_allocation", "RAW", 12),
    edge("f1", "planned:mo", "order:1", "planned", "sales_allocation", "FIN", 2),
    edge("f2", "planned:mo", "order:1", "planned", "sales_allocation", "FIN", 3),
    edge("f3", "planned:mo", "order:2", "planned", "sales_allocation", "FIN", 5),
    edge("unused", "planned:new:purchase.order:2", "unallocated:po", "planned", "unused_supply", "RAW", 2)];
  const orders = [1, 2].map(n => ({ order_ref: `S0000${n}`, node_key: `order:${n}`,
    baseline_node_keys: n === 1 ? ["disruption", "baseline:po", "baseline:mo", "order:1"] : [],
    baseline_edge_keys: n === 1 ? ["d", "o", "o1"] : [],
    planned_node_keys: ["planned:new:purchase.order:2", "planned:mo", `order:${n}`],
    planned_edge_keys: n === 1 ? ["component", "f1", "f2"] : ["component", "f3"], readiness: ready(n),
    missing: [n === 1 ? "baseline_allocation_quantity_unrecorded" : "baseline_relationship_unrecorded"] }));
  const chain = { schema_version: 1, basis: { plan_id: id, snapshot_sha256: snapshot, anchor_date: "2026-10-24", target_product_code: "FIN",
    quantity_basis: "product_units", time_basis: "planned_supply_availability_days", source_sha256: {} }, nodes, edges, orders,
    missing: ["actual_delivery_unmeasured", "component_order_allocation_unrecorded"],
    source_facts: { operations: [{ before: { product_code: "RAW", quantity: 12, origin_tokens: ["existing:mrp.production:1"] } }] } };
  return { plan_id: id, snapshot_sha256: snapshot, policy_sha256: "c".repeat(64), impact_chain: chain,
    order_readiness: { schema_version: 1, basis: { plan_id: id, snapshot_sha256: snapshot, anchor_date: "2026-10-24" }, orders: orders.map(o => o.readiness) },
    actions: [], margin: 20, on_time: true, cost_delta: null, new_spend: 10, checks: [], prepared_at: "2026-10-24T00:00:00Z" };
}
function caseRecord(plan = examplePlan()) { return { case_id: "d".repeat(32), state: "awaiting_approval", plan, approval: null, execution: null,
  order: { ref: "S00001..2", units: 10, due_days: 8, product: "FIN", customer: "Customer" }, disruption: { kind: "supplier_cancellation", vendor: "Vendor A" } }; }

test("bound chain refuses wrong plan, missing chain, dangling and duplicate public identities", () => {
  assert.equal(helpers.boundImpactChain({ plan_id: id }), null);
  const plan = examplePlan(); assert.equal(helpers.boundImpactChain(plan), plan.impact_chain);
  for (const mutate of [p => p.plan_id = "e".repeat(64), p => p.snapshot_sha256 = "f".repeat(64),
    p => p.impact_chain.edges[0].to = "missing", p => p.impact_chain.nodes.push(p.impact_chain.nodes[0]),
    p => p.impact_chain.orders[0].planned_edge_keys.push("not-recorded")]) {
    const altered = structuredClone(plan); mutate(altered); assert.equal(helpers.boundImpactChain(altered), null);
  }
});
test("SO scope preserves parallel allocations, whole-MO components and excludes unused quantity", () => {
  const chain = examplePlan().impact_chain, before = structuredClone(chain);
  const a = helpers.impactScope(chain, chain.orders[0], "planned"), b = helpers.impactScope(chain, chain.orders[1], "planned");
  assert.deepEqual(a.edges.map(e => [e.key, e.quantity]), [["component", 12], ["f1", 2], ["f2", 3]]);
  assert.deepEqual(b.edges.map(e => [e.key, e.quantity]), [["component", 12], ["f3", 5]]);
  assert.equal(a.nodes.some(n => n.kind === "unallocated_supply"), false);
  assert.equal(helpers.impactScope(chain, chain.orders[1], "baseline").edges.length, 0);
  assert.deepEqual(chain, before);
});
test("source details resolve only own published fields with a closed record grammar", () => {
  const chain = examplePlan().impact_chain;
  const ref = sourceRef("operations.0.before", ["quantity", "origin_tokens", "secret", "absent", "token"]);
  assert.ok(helpers.impactFactKeys.includes("entity_ref"));
  assert.ok(!helpers.impactFactKeys.includes("token"));
  assert.deepEqual(helpers.impactSourceFields(chain, ref), [["quantity", 12], ["origin_tokens", ["existing:mrp.production:1"]]]);
  for (const record of ["__proto__.x", "operations.0.__proto__", "https://example.com", "../private", "operations.constructor"]) {
    assert.deepEqual(helpers.impactSourceFields(chain, { ...ref, record }), []);
  }
  assert.deepEqual(helpers.impactSourceFields(chain, { ...ref, projection: "raw" }), []);
});
test("impact intent does not steal ordinary order budget, cost or readiness requests", () => {
  for (const q of ["budget for each order", "which purchase order costs the most", "per-order readiness", "客户订单预算", "采购成本是多少"]) assert.equal(helpers.impactIntent(q), false, q);
  for (const q of ["supply path for S00001", "why was S00001 affected", "which purchase order supplies this sales order", "why is this manufacturing order linked", ...(translations.zh ? ["为何 S00001 受影响", "哪个采购单供应该订单"] : [])]) assert.equal(helpers.impactIntent(q), true, q);
});
for (const lang of Object.keys(translations)) {
  const t = translator(lang);
  test(`${lang}: original quantity is unknown, planned units remain SKU-specific and new records do not acquire ERP names`, () => {
    const plan = examplePlan(), chain = plan.impact_chain;
    assert.equal(helpers.impactQuantity(chain.edges[0], t), t("impact.scopeQuantity"));
    assert.equal(helpers.impactQuantity(chain.edges[3], t), t("impact.exactQuantity", { quantity: 12, product: "RAW" }));
    assert.equal(helpers.impactQuantity(chain.edges.at(-1), t), t("impact.unusedQuantity", { quantity: 2, product: "RAW" }));
    const text = helpers.impactAnswer(plan, "supply path S00001", t);
    assert.ok(text.includes(t("impact.componentNote"))); assert.ok(text.includes(t("impact.actualNote")));
    assert.ok(text.includes("S00001")); assert.ok(!text.includes("S00002"));
    assert.ok(text.includes(t("impact.newDocument", { number: 2, product: "RAW" })));
    assert.ok(!text.includes(t("impact.unusedQuantity", { quantity: 2, product: "RAW" })));
  });
  test(`${lang}: missing order, missing baseline and legacy chain are honest`, () => {
    const plan = examplePlan();
    for (const ref of ["S000010", "SO9", "SO-A", "SO_99", "S0009"]) assert.equal(helpers.impactAnswer(plan, "supply path " + ref, t), t("impact.orderNotFound"));
    assert.ok(helpers.impactAnswer(plan, "supply path S00002", t).includes(t("impact.noBaseline")));
    assert.equal(helpers.impactAnswer(plan, "supply path Unallocated RAW", t), t("impact.nodeNoOrder"));
    assert.equal(helpers.impactAnswer({ ...plan, impact_chain: null }, "supply path", t), t("impact.unknown"));
    assert.equal(helpers.impactAnswer({ ...plan, plan_id: "different" }, "supply path", t), t("impact.invalid"));
  });
  test(`${lang}: mixed fallback preserves actual result, pending/unknown take precedence and inputs do not mutate`, () => {
    const record = caseRecord(), before = structuredClone(record);
    const text = localCaseAnswer(record, "supply path S00001 and actual execution", t).text;
    assert.ok(text.includes(t("impact.qaTitle"))); assert.ok(text.includes(t("impact.actualNote")));
    assert.ok(text.includes(t("caseChat.answerNoExecution", { plan: id.slice(0, 8) + "…" })));
    assert.deepEqual(record, before);
    const pending = { ...record, decision: { selection_pending: true } };
    assert.equal(localCaseAnswer(pending, "supply path and actual execution", t).text, t("candidate.pendingBody"));
    const unknown = { ...record, state: "execution_unknown" };
    assert.ok(!localCaseAnswer(unknown, "supply path and actual execution", t).text.includes(t("impact.qaTitle")));
    const actual = { ...record, state: "failed", execution: { status: "failed", writes: [], readback: null,
      verifier: { original_passed: false, score: 95.18, strict_success: false, evaluation_valid: true } } };
    const mixed = localCaseAnswer(actual, "supply path S00001 and verifier", t).text;
    assert.ok(mixed.includes(t("impact.qaTitle"))); assert.ok(mixed.includes("95.18"));
  });
}


test("disruption scope follows baseline links and keeps failed SKU quantity separate", () => {
  const plan = examplePlan();
  assert.deepEqual(helpers.disruptionScope(plan), { orders: 1, units: 5, demands: [{ product: "FIN", orders: 1, units: 5 }],
    supplies: [{ product: "RAW", documents: 1, units: 12 }] });
  // Planned replenishment and a second unrelated sales order do not expand direct baseline scope.
  const before = structuredClone(plan);
  for (const lang of Object.keys(translations)) {
    const t = translator(lang), answer = localCaseAnswer(caseRecord(plan), "delivery risk", t).text;
    assert.ok(answer.includes(lang === "zh" ? "全案规划" : "Total planning scope"));
    assert.ok(answer.includes(lang === "zh" ? "直接关联订单：1 张，共 5" : "directly linked orders: 1, totaling 5"));
    assert.ok(answer.includes("12") && answer.includes("RAW"));
    assert.ok(!answer.includes("10 units of FIN across 2 sales orders are affected"));
    const missing = localCaseAnswer(caseRecord(null), "delivery risk", t).text;
    assert.ok(missing.includes(lang === "zh" ? "尚未记录" : "not recorded"));
  }
  assert.deepEqual(plan, before);
  const missing = examplePlan(); missing.impact_chain.edges = missing.impact_chain.edges.filter(edge => edge.relation !== "disruption_scope");
  // A chain with dangling index references is rejected, rather than yielding invented zero impact.
  assert.equal(helpers.disruptionScope(missing), null);
  assert.equal(helpers.disruptionScope(null), null);
});

test("bilingual candidate descriptions disclose policy priority and unchanged original scoring", () => {
  for (const lang of Object.keys(translations)) {
    const t = translator(lang);
    assert.ok(t("candidate.basis", { count: 3 }).includes(lang === "zh" ? "不同优化优先级" : "different optimization priorities"));
    for (const key of ["candidate.costBody", "candidate.capacityBody"]) {
      assert.ok(lang === "zh" ? t(key).includes("改变原题") : /chang(?:es|ing) the original/.test(t(key)));
    }
    assert.ok(t("candidate.preferenceNote").includes(lang === "zh" ? "原始验收" : "Original verification"));
  }
});
