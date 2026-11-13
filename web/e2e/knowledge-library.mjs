/** Hosted knowledge library and immutable citation versions. Mock API, real UI. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = Number(process.env.FORGEFLOW_BROWSER_PORT || 4193);
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const base = `http://127.0.0.1:${port}`;
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
const sourcePath = "reference/industry/K01.md", linePath = "reference/industry/K10.md", legacyPath = "uploads/legacy.md";
let browser;
try {
  for (let attempt = 0; ; attempt++) {
    try { await fetch(`${base}/app/`); break; }
    catch (error) { if (attempt >= 50) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
  }
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
  const errors = [], blockRequests = [], writes = [];
  let blockMode = "ok";
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    const json = (value, status = 200) => route.fulfill({ status, json: value });
    if (request.method() === "POST") writes.push(path);
    if (path === "/api/auth/me") return json({ scope_id: "knowledge-reviewer", display_name: "Test reviewer", deployment: "hosted" });
    if (path === "/api/config") return json({ deployment: "hosted", runner_enabled: false });
    if (path === "/api/settings/model") return json({ configured: true, can_override: false, profile: "deepseek_flash", model: "mock" });
    if (["/api/runs", "/api/workspaces", "/api/odoo20/tasks"].includes(path)) return json([]);
    if (path === "/api/knowledge/documents") return json([
      { source_path: sourcePath, filename: "Procurement reference.md", version: 2, lifecycle: { searchable: true } },
      { source_path: linePath, filename: "Odoo operations.md", version: 2, lifecycle: { searchable: true } },
    ]);
    if (path.endsWith("/versions")) return json([{ version: 2 }, { version: 1 }]);
    if (path.endsWith("/blocks")) {
      assert.equal(request.headers()["x-forgeflow-workspace"], "knowledge-reviewer");
      const requestedPath = decodeURIComponent(path.slice("/api/knowledge/documents/".length, -"/blocks".length));
      const version = Number(url.searchParams.get("version"));
      blockRequests.push({ sourcePath: requestedPath, version });
      assert.ok(version > 0, "document request silently selected latest version");
      if (blockMode === "missing") return json({ detail: "document_version_not_found" }, 404);
      const actualVersion = blockMode === "mismatch" ? 2 : version;
      return json({ source_path: requestedPath, version: actualVersion,
        blocks: [{ ord: 1, text: `Original document body v${actualVersion}.`, locator: "page 28", version: actualVersion }] });
    }
    if (path === "/api/assistant/stream") {
      const answer = { found: true, mode: "harness", answer: "Evidence is ready for review.",
        citations: [`document://${sourcePath}#page 28`, `document://${linePath}:14-20`, `document://${legacyPath}#saved`],
        passages: [
          { path: `document://${sourcePath}`, locator: "page 28", start_line: 1, end_line: 6, text: "Captured procurement excerpt v1.", document_version: 1 },
          { path: `document://${linePath}`, start_line: 14, end_line: 20, text: "Captured operational excerpt v1.", document_version: 1 },
          { path: `document://${legacyPath}`, locator: "saved", start_line: 1, end_line: 2, text: "Captured legacy excerpt." },
        ] };
      return route.fulfill({ contentType: "text/event-stream", body: `id: 1\ndata: ${JSON.stringify({ type: "final", ...answer })}\n\n` });
    }
    return json({ detail: "not_found" }, 404);
  });
  await page.goto(`${base}/app/`);
  await page.locator("[data-home-view]").waitFor();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const library = page.locator('section[aria-labelledby="knowledge-library-heading"]');
  await library.getByText("Procurement reference.md · v2 · Industry reference · Indexed", { exact: true }).waitFor();
  assert.equal(await library.getByRole("button", { name: /Delete|Restore|Upload/i }).count(), 0, "hosted library exposed write controls");
  await library.getByRole("button", { name: "Versions", exact: true }).first().click();
  await library.getByRole("button", { name: "v1 · View document", exact: true }).click();
  const content = page.locator("[data-knowledge-document]");
  await content.getByText("Original document body v1.", { exact: true }).waitFor();
  assert.deepEqual(blockRequests.at(-1), { sourcePath, version: 1 });
  await page.keyboard.press("Escape");
  await content.waitFor({ state: "hidden", timeout: 5000 });
  await library.waitFor({ state: "visible", timeout: 5000 });
  await page.getByRole("button", { name: "Close settings", exact: true }).click();

  await page.locator('.composer [contenteditable="true"]').fill("Review the source evidence.");
  await page.locator("button.send").click();
  await page.getByText("Evidence is ready for review.", { exact: true }).waitFor();
  const citations = page.locator("[data-answer-citations] button");
  assert.equal(await citations.count(), 3);
  for (const [index, requestedPath, excerpt] of [[0, sourcePath, "Captured procurement excerpt v1."], [1, linePath, "Captured operational excerpt v1."]]) {
    await citations.nth(index).click();
    await content.getByText(excerpt, { exact: true }).waitFor();
    await content.locator("details summary").click();
    await content.getByText("Original document body v1.", { exact: true }).waitFor();
    assert.deepEqual(blockRequests.at(-1), { sourcePath: requestedPath, version: 1 });
    assert.equal(await content.getByText("Original document body v2.", { exact: true }).count(), 0);
    await page.getByRole("button", { name: "Close settings", exact: true }).click();
  }

  // An old saved answer keeps its captured passage without fetching today's file.
  await page.reload();
  await page.getByText("Evidence is ready for review.", { exact: true }).waitFor();
  const requestCount = blockRequests.length;
  await citations.nth(2).click();
  await content.getByText("Captured legacy excerpt.", { exact: true }).waitFor();
  assert.equal(blockRequests.length, requestCount);
  assert.equal(await content.locator("details").count(), 0);
  await page.getByRole("button", { name: "Close settings", exact: true }).click();

  for (const mode of ["missing", "mismatch"]) {
    blockMode = mode;
    await citations.first().click();
    await content.getByRole("alert").filter({ hasText: "The requested version is unavailable" }).waitFor();
    await content.getByText("Captured procurement excerpt v1.", { exact: true }).waitFor();
    assert.equal(await content.getByText("Original document body v2.", { exact: true }).count(), 0);
    await page.getByRole("button", { name: "Close settings", exact: true }).click();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await library.getByText("Procurement reference.md · v2 · Industry reference · Indexed", { exact: true }).waitFor();
  const overflow = await page.locator('[role="dialog"]').evaluate(element => element.scrollWidth - element.clientWidth);
  assert.ok(overflow <= 1, `narrow knowledge library overflows by ${overflow}px`);
  assert.deepEqual(errors, []);
  assert.ok(writes.every(path => path === "/api/assistant/stream"), "knowledge browsing mutated documents");
  console.log(JSON.stringify({ ok: true, checks: ["hosted library", "version history", "hash locator", "line locator", "saved excerpt", "missing version", "mismatched version", "narrow layout"] }));
} finally {
  if (browser) await browser.close();
  server.kill();
}
