/** UI-only arithmetic/scope regression. Mock API; never ERP or acceptance evidence. */
import assert from "node:assert/strict";
import { chromium } from "playwright-core";
import { readFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { CASE_ID, startRevisionFixture } from "./revision-fixture.mjs";

const source = JSON.parse(readFileSync(new URL("./fixtures/impact-a-reprojection.json", import.meta.url), "utf8"));
const current = structuredClone(source.cases[0].replay_case);
current.case_id = CASE_ID;
current.order.product = "UI test fixture — Clean Room Sealed Fixture";
current.order.due_days_max = 10;
current.state = "awaiting_approval";
current.approval = current.execution = null;
current.history = []; current.audit = []; current.comparison = null;
current.result = { execution_status: null, readback_status: "unknown", verifier_status: "unknown", strict_success: null,
  evaluation_valid: null, verified: false, simulation_passed: false, outcome: "awaiting_approval" };
current.decision = { can_approve: false, can_reject: false, can_request_changes: false, reason: "ui_test_only" };
const plan = current.plan;
plan.margin_basis = "new_spend_coverage_v2";
plan.margin = 100 * (1 - 4794.94 / 20623.23);
plan.decision_metrics.schema_version = 2;
plan.decision_metrics.basis.margin_basis = plan.margin_basis;
Object.assign(plan.decision_metrics.margin, { new_spend_revenue: "20623.23", new_spend_units: 43, stock_units: 7,
  planned_margin_pct: String(plan.margin), original_floor_pct: "27.5", effective_floor_pct: "27.5",
  headroom_pp: String(plan.margin - 27.5), max_planning_cost: "14951.84175", cost_headroom: "10156.90175" });
for (const check of plan.checks ?? []) if (check.code.includes("margin")) {
  check.actual = `${plan.margin}%`; check.threshold = 27.5;
  check.source = "independent new-spend coverage check; original floor 27.5%; effective floor 27.5%; excludes finished stock revenue";
}
const publicBundle = process.env.FORGEFLOW_UI_PUBLIC === "1";
const directory = publicBundle ? fileURLToPath(new URL("../../web-public/dist", import.meta.url)) : undefined;
const instance = await startRevisionFixture({ directory });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const out = process.env.FORGEFLOW_UI_OUT ?? "/tmp/forgeflow-v2-ui-only";
mkdirSync(out, { recursive: true });
try {
  for (const lang of publicBundle ? ["en"] : ["zh", "en"]) {
    instance.fixture.current = structuredClone(current);
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: lang === "zh" ? "zh-CN" : "en-US" });
    const page = await context.newPage();
    const errors = []; page.on("pageerror", error => errors.push(String(error)));
    await page.addInitScript(value => localStorage.setItem("forgeflow.lang", value), lang);
    await page.goto(`${instance.url}/app/#case=${CASE_ID}`);
    await page.locator("[data-case-detail]").waitFor();
    const margin = page.locator('[data-kpi="margin"]');
    assert.match(await margin.innerText(), /76\.75%/);
    assert.ok((await margin.textContent()).includes(lang === "zh" ? "新增支出利润率" : "New-spend margin"), await margin.innerText());
    const scope = (await page.locator("[data-disruption-scope]").allInnerTexts()).join(" ");
    assert.ok(scope.includes(lang === "zh" ? "1 张，共 13 件" : "1, totaling 13 units"), scope);
    assert.ok(scope.includes(lang === "zh" ? "3 件" : "3 units"), scope);
    assert.ok(!(await page.locator("body").innerText()).includes("50 sales orders are affected"));
    await margin.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${out}/${publicBundle ? "public" : "workbench"}-${lang}-new-margin.png` });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "narrow layout overflow");
    await page.screenshot({ path: `${out}/${publicBundle ? "public" : "workbench"}-${lang}-390.png` });

    // The same old numeric value remains visible, without acquiring a new floor pass.
    instance.fixture.current.plan.margin = 80;
    delete instance.fixture.current.plan.margin_basis;
    instance.fixture.current.plan.decision_metrics.schema_version = 1;
    delete instance.fixture.current.plan.decision_metrics.basis.margin_basis;
    await page.reload(); await margin.waitFor();
    assert.ok((await margin.textContent()).includes(lang === "zh" ? "旧口径" : "legacy basis"), await margin.innerText());
    assert.equal(await margin.getAttribute("data-margin-verified"), "false");
    assert.ok(!(await margin.innerText()).includes("27.5"));
    instance.fixture.current.plan = null;
    await page.reload(); await page.locator("[data-case-detail]").waitFor();
    const missing = (await page.locator("[data-disruption-scope]").allInnerTexts()).join(" ");
    assert.ok(missing.includes(lang === "zh" ? "尚未记录" : "not recorded"));
    assert.deepEqual(errors, []);
    await context.close();
  }
  assert.equal(instance.fixture.calls.length, 0, "UI check attempted a product mutation");
  console.log(`margin-scope=ok (${publicBundle ? "public en" : "workbench zh/en"}; mock API, no ERP/model/acceptance; screenshots ${out})`);
} finally { await browser.close(); await instance.close(); }
