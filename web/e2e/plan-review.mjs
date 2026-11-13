/** Candidate-build A4 regression. All ERP responses are synthetic and local. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright-core";

const port = Number(process.env.FORGEFLOW_TEST_PORT || 4187);
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
const base = `http://127.0.0.1:${port}`;
let browser;
try {
  for (let attempt = 0; attempt < 40; attempt++) {
    try { if ((await fetch(`${base}/app/`)).ok) break; } catch {}
    if (attempt === 39) throw new Error("candidate web server did not start");
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ headless: true,
    ...(process.env.CHROME_EXECUTABLE_PATH ? { executablePath: process.env.CHROME_EXECUTABLE_PATH }
      : { channel: process.env.CHROME_CHANNEL || "chrome" }) });
  const plan = (id, version = 1) => ({
    plan_id: id, plan_version: version, plan_digest: `plan-${id}-${version}`, action_digest: `actions-${id}-${version}`,
    snapshot_hash: `facts-${id}-${version}`, plan_state: "awaiting_approval", expires_at: "2099-01-01T00:00:00Z", action_count: 8, line_count: 7,
    order: { name: `PO-${id}`, state: "cancel", currency_id: [1, "USD"] }, task: { attention: "open", need: "approval" },
    actions: Array.from({ length: 8 }, (_, index) => ({ action_id: `action-${index}`, type: "create_purchase_order",
      target_model: "purchase.order", target_id: 0, quantity: index + 1, unit: "units", currency: "USD",
      summary: `Buy component ${index + 1}`, params: { partner_id: 100 + index, date_planned: "2026-11-20",
        lines: [{ product_id: index + 1, product_qty: index + 1, price_unit: 10 + index, date_planned: "2026-11-20" }] }, preconditions: [] })),
    lines: Array.from({ length: 7 }, (_, index) => ({ id: index + 1, product_id: [index + 1, `Original component ${index + 1}`],
      product_qty: 10 + index, price_unit: 4, product_uom_id: [1, "units"], date_planned: "2026-11-15" })),
    facts: { replacements: [{ assumptions: ["Supplier capacity is unconfirmed"], binding_constraints: ["Arrival before due date"] }],
      custom_fact: { unknown_margin: null, contract_note: "Complete snapshot evidence" } },
  });
  const plans = new Map([77, 78, 79, 80, 81, 82, 83, 84, 85].map(id => [id, plan(id)]));
  const failed = new Set();
  const held = new Map();
  const requests = [];
  const approvals = [];
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", error => errors.push(String(error)));
  await page.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const json = (value, status = 200) => route.fulfill({ status, json: value });
    if (path === "/api/auth/me") return json({ scope_id: "review-test", display_name: "Reviewer", deployment: "local" });
    if (path === "/api/config") return json({ deployment: "local", runner_enabled: true, accounts_enabled: false });
    if (path === "/api/capabilities") return json({ case_read: true, environment: { ready: true } });
    if (path === "/api/odoo20/tasks") return json([...plans.keys()].map(id => ({ plan_id: id, ticket_id: `ticket-${id}`,
      goal: `Review PO-${id}`, attention: "open", need: "approval", state: "awaiting_approval" })));
    const match = path.match(/^\/api\/odoo20\/plans\/(\d+)(\/approve)?$/);
    if (match) {
      const id = Number(match[1]);
      if (method === "POST") { approvals.push({ id, body: route.request().postDataJSON() }); return json({ state: "approved" }); }
      requests.push(id);
      if (held.has(id)) await held.get(id).promise;
      if (failed.has(id)) return json({ detail: "unavailable" }, 503);
      return json(plans.get(id));
    }
    return json([]);
  });
  const navigate = async id => {
    await page.goto(`${base}/app/#cases&plan=${id}`, { waitUntil: "domcontentloaded" });
    await page.locator("[data-plan-review]").waitFor();
    await page.locator("[data-plan-approve]").waitFor();
  };
  const ack = page.locator("[data-review-ack]");
  const approve = page.locator("[data-plan-approve]");
  await navigate(77);
  assert.equal(await page.locator("[data-review-action]").count(), 8, "every action must be visible");
  assert.equal(await page.locator("[data-review-line]").count(), 7, "every original line must be visible");
  assert.equal(await page.locator("[data-review-action-diff]").count(), 8, "every action needs a complete diff");
  assert.equal(await approve.isDisabled(), true, "approval needs explicit review acknowledgement");
  await page.locator("[data-review-action-diff]").last().locator("summary").first().click();
  assert.match(await page.locator("[data-review-action-diff]").last().innerText(), /107/);
  await page.locator("[data-review-facts] summary").click();
  assert.match(await page.locator("[data-review-facts]").innerText(), /Complete snapshot evidence/);
  assert.match(await page.locator("[data-plan-review]").innerText(), /Supplier capacity is unconfirmed/);
  await ack.check();
  plans.set(77, plan(77, 2));
  await approve.click();
  await page.waitForFunction(() => document.querySelector("[data-review-version]")?.textContent?.includes("2"));
  assert.equal(approvals.length, 0, "changed version must never be approved by stale click");
  assert.equal(await ack.isChecked(), false);
  assert.equal(await approve.isDisabled(), true);
  await ack.check(); await approve.click();
  await page.locator("[data-execution-pending]").waitFor();
  assert.deepEqual(approvals, [{ id: 77, body: { comment: "", expected_plan_version: 2,
    expected_action_digest: "actions-77-2", expected_plan_digest: "plan-77-2" } }]);

  // Plan switches suppress the previous plan immediately and retire late fetches.
  let release;
  held.set(78, { promise: new Promise(resolve => { release = resolve; }) });
  await page.evaluate(() => { location.hash = "cases&plan=78"; });
  for (let i = 0; i < 100 && !requests.includes(78); i++) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(requests.includes(78));
  assert.equal(await page.locator("[data-plan-review]").count(), 0);
  assert.equal(await approve.count(), 0);
  await page.evaluate(() => { location.hash = "cases&plan=79"; });
  await page.waitForFunction(() => document.querySelector("[data-odoo20-summary] strong")?.textContent?.includes("PO-79"));
  release(); held.delete(78);
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.match(await page.locator("[data-odoo20-summary] strong").innerText(), /PO-79/);

  failed.add(80);
  await page.evaluate(() => { location.hash = "cases&plan=80"; });
  await page.getByText("The current plan could not be confirmed.", { exact: false }).first().waitFor();
  assert.equal(await approve.count(), 0, "load failure must hide old approval");
  failed.delete(80);
  await page.locator("[data-odoo20-plan]").getByRole("button", { name: "Retry", exact: true }).first().click();
  await ack.waitFor(); await ack.check();
  failed.add(80);
  await approve.click();
  await page.getByText("The current plan could not be confirmed.", { exact: false }).first().waitFor();
  assert.equal(await approve.isDisabled(), true, "failed preflight must block approval");
  assert.equal(approvals.length, 1);

  const incomplete = plan(81); incomplete.action_count = 9;
  plans.set(81, incomplete);
  await navigate(81);
  assert.equal(await approve.isDisabled(), true, "truncated details must not be approved");
  assert.equal(await ack.isDisabled(), true);
  await navigate(82); await ack.check();
  // Even an incorrectly unchanged digest cannot hide a changed displayed business fact.
  plans.get(82).lines[0].price_unit = 999;
  await approve.click();
  await page.getByText("The plan or its approval state changed.", { exact: false }).first().waitFor();
  assert.equal(await ack.isChecked(), false);
  assert.equal(approvals.length, 1);
  const expired = plan(83); expired.expires_at = "2000-01-01T00:00:00Z"; plans.set(83, expired);
  await navigate(83);
  assert.equal(await approve.isDisabled(), true, "expired plans cannot be approved");
  await navigate(84); await ack.check();
  plans.set(84, plan(84, 2));
  await page.waitForFunction(() => document.querySelector("[data-review-version]")?.textContent?.includes("2"), null, { timeout: 8000 });
  assert.equal(await ack.isChecked(), false, "background version refresh must retire review acknowledgement");
  assert.equal(await approve.isDisabled(), true);
  const partial = plan(85);
  partial.facts.completeness = { complete: false, truncated: true, lines_complete: false, line_limit: 500,
    reason: "purchase_order_line_limit_exceeded" };
  plans.set(85, partial);
  await navigate(85);
  assert.equal(await approve.isDisabled(), true, "partial source facts must not be approved");
  assert.equal(await ack.isDisabled(), true);
  assert.equal(await page.locator("[data-review-action]").count(), 8, "partial plans remain inspectable");
  await page.locator("[data-review-facts] summary").click();
  assert.match(await page.locator("[data-review-facts]").innerText(), /purchase_order_line_limit_exceeded/);
  if (process.env.FORGEFLOW_SCREENSHOT_DIR) {
    await mkdir(process.env.FORGEFLOW_SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: `${process.env.FORGEFLOW_SCREENSHOT_DIR}/plan-review.png`, fullPage: true });
  }
  assert.deepEqual(errors, [], "the review must not crash");
  console.log(JSON.stringify({ ok: true, actions: 8, original_lines: 7, assertions: ["complete-details", "review-binding", "version-drift", "switch-race", "load-failure", "preflight-failure", "truncated-payload", "changed-facts", "expired-plan", "background-version-refresh", "partial-source-facts"] }));
} finally { if (browser) await browser.close(); server.kill(); }
