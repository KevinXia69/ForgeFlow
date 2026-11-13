/** Setup operation panels: ready/empty, loading, failure, no permission, reconnect. */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = 4183;
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise(resolve => setTimeout(resolve, 500));
const BASE = `http://127.0.0.1:${port}`;
const browser = await chromium.launch({ channel: "chrome", headless: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
let parseMode = "ok", rulesMode = "ok", healthOk = true, releaseRules;
const workspace = {
  id: "w1", name: "Demo manufacturer", industry: "Manufacturing",
  modules: ["sales", "purchase"], status: "draft", states: {},
  sources: [], mappings: [],
};
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await page.addInitScript(() => { if (!localStorage.getItem("forgeflow.lang")) localStorage.setItem("forgeflow.lang", "zh"); });
await page.route("**/api/**", async route => {
  const url = new URL(route.request().url());
  const path = url.pathname;
  const json = (value, status = 200) => route.fulfill({ status, json: value });
  if (path === "/api/auth/me") return json({ scope_id: "owner-a", display_name: "Alex", deployment: "local" });
  if (path === "/api/config") return json({ deployment: "local", runner_enabled: true, accounts_enabled: false, workbenches: { investigation: false, recovery_review: false } });
  if (path === "/api/runs") return json([]);
  if (path === "/api/capabilities") return json({ case_read: true, environment: { ready: true } });
  if (path === "/api/workspaces") return json([workspace]);
  if (path === "/api/onboarding/targets") return json({ models: [] });
  if (path === "/api/sources/health") return healthOk ? json({ healthy: true, services: [] }) : json({ detail: "offline" }, 503);
  if (path === "/api/sources/subscriptions") return json([{ source_type: "odoo20.purchase_order", enabled: true, interval_seconds: 60 }]);
  if (path === "/api/knowledge/parse-jobs") return parseMode === "fail" ? json({ detail: "parse_failed" }, 500) : json([]);
  if (path === "/api/knowledge/documents") return json([]);
  if (path === "/api/odoo20/business-data") return json({ records: [] });
  if (path.includes("/blocks")) return json({ blocks: [] });
  if (path === "/api/rules") {
    if (rulesMode === "forbidden") return json({ detail: "forbidden" }, 403);
    if (rulesMode === "hold") await new Promise(resolve => { releaseRules = resolve; });
    return json([]);
  }
  if (path === "/api/tasks/failures") return json([]);
  if (path === "/api/restore/status") return json({ paused: false, reason: null });
  return json([]);
});

let loads = 0;
async function reload() {
  // A plain hash navigation is same-document: React would keep the previous
  // panel state and no request would be made.  The query tag forces a new load.
  loads += 1;
  await page.goto(`${BASE}/app/?load=${loads}#setup`, { waitUntil: "domcontentloaded" });
  await page.locator("[data-setup-ops]").waitFor({ timeout: 20000 });
}
const panel = (id) => page.locator(`[data-setup-panel="${id}"]`);
async function expectState(id, state) {
  await page.waitForFunction(([panelId, value]) =>
    document.querySelector(`[data-setup-panel="${panelId}"]`)?.getAttribute("data-panel-state") === value,
  [id, state], { timeout: 15000 });
}
try {
  // 1. Ready/empty state: five panels, each says what is empty.
  await reload();
  for (const id of ["connection", "parse", "index", "rules", "recovery", "business"]) {
    await expectState(id, "ready");
  }
  assert(await page.locator("[data-panel-empty]").count() >= 4, "empty panels do not say what is empty");
  assert(await page.locator('[data-setup-panel="connection"]').innerText().then(text => text.includes("1 个已启用来源")), "connection panel does not show sources");

  // 2. Loading is explicit while a request is held.
  rulesMode = "hold";
  await reload();
  await expectState("rules", "loading");
  releaseRules();
  await expectState("rules", "ready");

  // 3. A failing service does not hide the others.
  parseMode = "fail"; rulesMode = "ok";
  await reload();
  await expectState("parse", "error");
  await expectState("connection", "ready");
  assert(await panel("parse").locator('[data-panel-message="error"]').innerText().then(text => text.includes("加载失败")), "parse failure copy missing");

  // 4. No permission is distinct from a failure.
  rulesMode = "forbidden"; parseMode = "ok";
  await reload();
  await expectState("rules", "forbidden");
  assert(await panel("rules").locator('[data-panel-message="forbidden"]').innerText().then(text => text.includes("无权限")), "forbidden copy missing");

  // 5. Reconnect recovers a failed panel.
  healthOk = false; rulesMode = "ok";
  await reload();
  await expectState("connection", "error");
  healthOk = true;
  await page.getByRole("button", { name: "重新连接" }).click();
  await expectState("connection", "ready");
  console.log(JSON.stringify({ ok: true, panels: 6, states: ["ready", "loading", "error", "forbidden", "reconnect"] }));
} finally {
  await browser.close();
  server.kill();
}
