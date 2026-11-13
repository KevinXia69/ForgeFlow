/** Four operator operations in the plan panel: stop, cancel, revoke, reconcile. */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = 4184;
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise(resolve => setTimeout(resolve, 500));
const BASE = `http://127.0.0.1:${port}`;
const browser = await chromium.launch({ channel: "chrome", headless: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
const calls = [];
let cancelled = false;
let prepared = false;
let revoked = false;
const plan = { plan_id: 77, plan_version: 1, action_digest: "actions-77", plan_digest: "plan-77", snapshot_hash: "facts-77",
  plan_state: "awaiting_approval", expires_at: "2099-01-01T00:00:00Z", action_count: 1, line_count: 1,
  actions: [{ action_id: "buy-1", type: "create_purchase_order", target_model: "purchase.order", target_id: 0,
    quantity: 5, unit: "units", currency: "USD", params: { lines: [{ product_id: 1, product_qty: 5, price_unit: 10 }] } }],
  lines: [{ product_id: [1, "Synthetic part"], product_qty: 5, price_unit: 10 }], facts: { assumptions: ["Synthetic test"] },
  order: { name: "P00216", state: "cancel" }, task: { attention: "open", need: "approval" } };
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.addInitScript(() => { if (!localStorage.getItem("forgeflow.lang")) localStorage.setItem("forgeflow.lang", "zh"); });
await page.route("**/api/**", async route => {
  const path = new URL(route.request().url()).pathname;
  const method = route.request().method();
  const json = (value, status = 200) => route.fulfill({ status, json: value });
  if (path === "/api/auth/me") return json({ scope_id: "owner-a", display_name: "Alex", deployment: "local" });
  if (path === "/api/config") return json({ deployment: "local", runner_enabled: true, accounts_enabled: false, workbenches: { investigation: false, recovery_review: false } });
  if (path === "/api/runs") return json([]);
  if (path === "/api/capabilities") return json({ case_read: true, environment: { ready: true } });
  if (path === "/api/product/cases") return json([]);
  if (path === "/api/odoo20/tasks") return json([{ plan_id: 77, ticket_id: "t-1", goal: "恢复断供", attention: prepared ? "open" : "answered", need: "approval", state: cancelled ? "cancelled" : "investigating", user_state: cancelled ? "needs_action" : undefined }]);
  if (path === "/api/tasks") return json([]);
  if (path === "/api/tasks/failures") return json([]);
  if (path === "/api/tasks/attentions") return json(revoked ? [{ attention_id: "unknown-77", ticket_id: "t-1", plan_id: 77,
    need: "result_unknown", goal: "恢复断供", question: "确认执行结果" }] : []);
  if (path === "/api/rules") return json([]);
  if (path === "/api/odoo20/plans" && method === "POST") { prepared = true; cancelled = false; return json(plan); }
  if (path === "/api/odoo20/plans/77") return json(plan);
  if (path === "/api/odoo20/plans/77/approve") { calls.push("approve"); return json({}); }
  if (path === "/api/odoo20/plans/77/revoke") { calls.push("revoke"); revoked = true; return json({ plan_id: 77, revoked: true }); }
  if (path === "/api/odoo20/plans/77/reconcile") { calls.push("reconcile"); return json({ state: "unknown", next: "needs_investigation" }); }
  if (path === "/api/tasks/t-1/cancel") { calls.push("cancel"); cancelled = true; return json({ ticket_id: "t-1", state: "cancelled" }); }
  return json([]);
});
try {
  await page.goto(`${BASE}/app/?load=1#cases`, { waitUntil: "domcontentloaded" });
  const panel = page.locator("[data-odoo20-plan]");
  await panel.waitFor({ timeout: 20000 });
  await panel.getByRole("button", { name: "取消调查" }).waitFor({ timeout: 10000 });
  assert(await panel.getByRole("button", { name: "取消调查" }).count() === 1, "stop/cancel button missing");
  await panel.getByRole("button", { name: "取消调查" }).click();
  await page.waitForFunction(() => document.body.innerText.includes("需要处理"), null, { timeout: 10000 });
  assert(calls.includes("cancel"), "cancel API was not called");

  await panel.locator("input").nth(0).fill("216");
  await panel.locator("input").nth(1).fill("供应商取消");
  await panel.getByRole("button", { name: "准备" }).click();
  await panel.locator("[data-plan-approve]").waitFor({ timeout: 10000 });
  await panel.locator("[data-review-ack]").check();
  await panel.locator("[data-plan-approve]").click();
  await panel.getByRole("button", { name: "撤销批准" }).waitFor({ timeout: 10000 });
  await panel.getByRole("button", { name: "撤销批准" }).click();
  await page.waitForFunction(() => document.body.innerText.includes("批准已撤销"));
  assert(calls.includes("revoke"), "revoke API was not called");

  await panel.getByRole("button", { name: "再次核对" }).click();
  await new Promise(resolve => setTimeout(resolve, 150));
  assert(calls.includes("reconcile"), "reconcile API was not called");
  console.log(JSON.stringify({ ok: true, calls }));
} finally {
  await browser.close();
  server.kill();
}
