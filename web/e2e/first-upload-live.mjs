/** Chrome -> actual FastAPI -> actual PostgreSQL first-generation acceptance. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { chromium } from "playwright-core";

const databaseEnv = process.env.FORGEFLOW_TEST_DATABASE_ENV;
if (!databaseEnv) throw new Error("Provide FORGEFLOW_TEST_DATABASE_ENV pointing to a private control database env file");
const port = Number(process.env.FORGEFLOW_BROWSER_PORT || 4198);
const output = process.env.FORGEFLOW_SCREENSHOT_DIR || "/tmp/forgeflow-v386-first-upload";
const staticRoot = process.env.FORGEFLOW_STATIC_DIR || "/tmp/forgeflow-polish-private";
const repo = fileURLToPath(new URL("../../", import.meta.url));
await mkdir(output, { recursive: true, mode: 0o700 });
const server = spawn(resolve(repo, ".venv/bin/python"), ["web/e2e/fixtures/first-upload-api.py"], {
  cwd: repo, env: { ...process.env, FIRST_UPLOAD_DATABASE_ENV: databaseEnv,
    FIRST_UPLOAD_OUTPUT: output, FIRST_UPLOAD_PORT: String(port), FIRST_UPLOAD_PROVIDER_PORT: String(port + 1),
    FIRST_UPLOAD_STATIC_DIR: staticRoot }, stdio: ["ignore", "pipe", "pipe"],
});
let serverError = "";
server.stderr.on("data", chunk => { serverError += chunk.toString(); });
const base = `http://127.0.0.1:${port}`;
const report = { status: "running", api: "actual local FastAPI", database: "actual PostgreSQL",
  semantic_services: "local HTTPS fixtures for embedding and Qdrant (certificate verification retained)", ui_status_mocked: false, checks: [] };
let browser;
try {
  for (let attempt = 0; ; attempt++) {
    try { const response = await fetch(`${base}/fixture/report`); if (!response.ok) throw new Error("API not ready"); break; }
    catch (error) { if (attempt >= 120 || server.exitCode !== null) throw new Error(`API startup failed: ${serverError.slice(-1500)}`); await new Promise(resolve => setTimeout(resolve, 250)); }
  }
  const initial = await (await fetch(`${base}/fixture/report`)).json();
  assert.equal(initial.published_generation, null);
  assert.deepEqual(initial.documents, []);
  report.checks.push("new tenant has no documents or published generation");
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies(JSON.parse(await readFile(resolve(output, "cookie.json"), "utf8")));
  await context.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
  const page = await context.newPage();
  await page.goto(`${base}/app/`);
  await page.locator("[data-home-view]").waitFor({ timeout: 30000 });
  const responseWait = page.waitForResponse(response => new URL(response.url()).pathname === "/api/knowledge/documents" && response.request().method() === "POST");
  await page.locator('[data-home-view] > input[type="file"]').setInputFiles({ name: "first-upload.txt", mimeType: "text/plain", buffer: Buffer.from("First document for isolated browser acceptance.\nThe test verifies catalog publication and UI readiness.\n") });
  const uploaded = await responseWait;
  assert.ok(uploaded.ok(), `upload returned ${uploaded.status()}`);
  const source = (await uploaded.json()).source_path;
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const row = page.locator(".settings-documents li").filter({ hasText: /first-upload|\.txt/ }).first();
  await row.getByRole("button", { name: "Index", exact: true }).waitFor({ timeout: 30000 });
  const indexWait = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/index") && response.request().method() === "POST");
  await row.getByRole("button", { name: "Index", exact: true }).click();
  const indexed = await indexWait;
  assert.ok(indexed.ok(), `first index returned ${indexed.status()}: ${await indexed.text()}`);
  await page.getByRole("button", { name: "Close settings", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[data-home-task-panel="upload"] [role="status"]')?.textContent === "Document ready. Ask a question and inspect its sources.", undefined, { timeout: 30000 });
  await page.screenshot({ path: resolve(output, "first-upload-ready.png"), fullPage: true, animations: "disabled" });
  const final = await (await fetch(`${base}/fixture/report`)).json();
  const document = final.documents.find(row => row.source_path === source);
  assert.ok(final.published_generation);
  assert.equal(document.indexed_generation, final.published_generation);
  assert.equal(document.published_generation, final.published_generation);
  assert.equal(document.lifecycle.searchable, true);
  assert.ok(final.provider_calls.some(call => call === "POST /embeddings"));
  assert.ok(final.indexed_points.length > 0);
  assert.ok(final.indexed_points.every(point => point.generation === final.published_generation && point.status === "published"));
  report.checks.push("real upload created catalog document", "real Index API bootstrapped first generation",
    "real PostgreSQL catalog indexed/published generations match", "UI observed searchable=true through real API", "semantic HTTP points published");
  report.document = { source_path: source, version: document.version, indexed_generation: document.indexed_generation,
    published_generation: document.published_generation, lifecycle: document.lifecycle };
  report.provider_calls = final.provider_calls;
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  report.failure = error instanceof Error ? error.message : String(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await Promise.race([new Promise(resolve => server.once("exit", resolve)), new Promise(resolve => setTimeout(resolve, 5000))]);
  await writeFile(resolve(output, "first-upload-validation.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report));
}
