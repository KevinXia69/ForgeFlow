/** Home search and material lifecycle contract. Mock APIs; no ERP or model calls. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = Number(process.env.FORGEFLOW_BROWSER_PORT || 4191);
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const deployment = process.env.FORGEFLOW_TEST_DEPLOYMENT || "local";
const base = `http://127.0.0.1:${port}`;
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
let browser;
try {
  for (let attempt = 0; ; attempt++) {
    try { await fetch(`${base}/app/`); break; }
    catch (error) { if (attempt >= 50) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
  }
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
  const pageErrors = [], searches = [], writes = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  let jobState = "queued", searchable = false, needsReview = false, failUpload = false;
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const json = (value, status = 200) => route.fulfill({ status, json: value });
    if (request.method() === "POST") writes.push(path);
    if (path === "/api/auth/me") return json({ scope_id: "materials-reviewer", display_name: "Test reviewer", deployment });
    if (path === "/api/config") return json({ deployment, runner_enabled: false });
    if (path === "/api/settings/model") return json({ configured: false });
    if (["/api/runs", "/api/workspaces", "/api/odoo20/tasks"].includes(path)) return json([]);
    if (path === "/api/odoo20/business-data") {
      searches.push({ kind: url.searchParams.get("kind"), q: url.searchParams.get("q") });
      // The product match comes from an order line; its name is absent from the summary.
      return json({ records: url.searchParams.get("kind") === "purchase_order"
        ? [{ id: 7, name: "PO-0042", partner_id: [2, "Example supply"], state: "purchase" }] : [] });
    }
    if (path === "/api/knowledge/documents") {
      if (request.method() === "POST") {
        assert.equal(request.headers()["x-forgeflow-workspace"], "materials-reviewer");
        return failUpload ? json({ detail: "forbidden" }, 403)
          : json({ source_path: "uploads/brief.txt", job_id: "parse-brief", state: "queued" }, 202);
      }
      return json([{ source_path: "uploads/brief.txt", parse_status: "succeeded",
        lifecycle: { searchable, needs_review: needsReview, withdrawn: false } }]);
    }
    if (path === "/api/knowledge/parse-jobs/parse-brief") return json({ state: jobState });
    return json({ detail: "not_found" }, 404);
  });
  await page.goto(`${base}/app/`);
  await page.locator("[data-home-view]").waitFor();
  await page.locator(".chat-preset").first().click();
  await page.locator(".home-task-search input").fill("SKU-RELAY");
  await page.locator(".home-task-search button").click();
  await page.getByText("PO-0042", { exact: true }).waitFor();
  assert.deepEqual(searches.sort((a, b) => a.kind.localeCompare(b.kind)), [
    { kind: "purchase_order", q: "SKU-RELAY" }, { kind: "stock", q: "SKU-RELAY" },
  ]);
  const input = page.locator('[data-home-view] > input[type="file"]');
  const upload = () => input.setInputFiles({ name: "brief.txt", mimeType: "text/plain", buffer: Buffer.from("Material lifecycle example") });
  const status = page.locator('[data-home-task-panel="upload"] [role="status"]');
  const expectStatus = async text => {
    await page.waitForFunction(expected => document.querySelector('[data-home-task-panel="upload"] [role="status"]')?.textContent === expected, text);
  };

  await upload();
  await expectStatus("Saved to company materials. Processing.");
  assert.ok(!(await status.innerText()).includes("Document ready"));
  jobState = "succeeded";
  await expectStatus("Document parsed. Preparing it for search.");
  searchable = true;
  await expectStatus("Document ready. Ask a question and inspect its sources.");

  needsReview = true; searchable = false;
  await upload();
  await expectStatus("Document saved. Review its parsed content before using it in answers.");
  failUpload = true;
  await upload();
  await expectStatus("Upload failed. Try again.");
  assert.deepEqual(pageErrors, []);
  assert.ok(writes.length === 3 && writes.every(path => path === "/api/knowledge/documents"));
  console.log(JSON.stringify({ ok: true, deployment, checks: ["order-line search", "workspace-scoped upload", "parse then index", "review-required", "upload error", "no ERP mutations"] }));
} finally {
  if (browser) await browser.close();
  server.kill();
}
