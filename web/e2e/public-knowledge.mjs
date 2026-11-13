/** Actual deployed knowledge UI acceptance. Explicit URL and private cookie file required. */
import assert from "node:assert/strict";
import { readFile, stat, mkdir, writeFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { chromium } from "playwright-core";

const base = process.env.FORGEFLOW_URL;
const cookieFile = process.env.FORGEFLOW_BROWSER_COOKIE_FILE;
const out = process.env.FORGEFLOW_SCREENSHOT_DIR;
if (!base || !cookieFile || !out) throw new Error("Provide FORGEFLOW_URL, FORGEFLOW_BROWSER_COOKIE_FILE, and FORGEFLOW_SCREENSHOT_DIR.");
const origin = new URL(base);
assert.equal(origin.protocol, "https:", "Public acceptance requires HTTPS");
assert.ok(!origin.username && !origin.password && !origin.search && !origin.hash, "Use an app URL without credentials or query parameters");
const cookieInfo = await stat(cookieFile);
assert.equal(cookieInfo.mode & 0o077, 0, "Cookie file must be private (0600)");
const saved = JSON.parse(await readFile(cookieFile, "utf8"));
const cookies = Array.isArray(saved) ? saved : saved.cookies;
assert.ok(Array.isArray(cookies) && cookies.length > 0, "Cookie file must contain a Playwright cookie array or storage-state cookies");
await mkdir(out, { recursive: true, mode: 0o700 });
const report = { started_at: new Date().toISOString(), environment: origin.origin,
  browser: "Google Chrome (Playwright, headless)", mocked_api: false,
  status: "running", checks: [], screenshots: [], document_requests: [], uploaded_test_document: null };
const browser = await chromium.launch({ channel: "chrome", headless: true });
let context, page;
try {
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addCookies(cookies);
  await context.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
  page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("response", response => {
    const url = new URL(response.url());
    if (url.origin === origin.origin && url.pathname.startsWith("/api/knowledge/documents/")
      && (url.pathname.endsWith("/blocks") || url.pathname.endsWith("/versions"))) {
      report.document_requests.push({ path: decodeURIComponent(url.pathname), version: url.searchParams.get("version"), status: response.status() });
    }
  });
  const screenshot = async name => {
    await page.screenshot({ path: resolve(out, name), fullPage: true, animations: "disabled" });
    report.screenshots.push(name);
    console.log(JSON.stringify({ stage: name }));
  };
  await page.goto(base, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.locator("[data-home-view]").waitFor({ timeout: 30000 });
  report.checks.push("authenticated home opened");
  await screenshot("01-home.png");

  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const library = page.locator('section[aria-labelledby="knowledge-library-heading"]');
  const k04 = library.locator(".settings-documents li").filter({ hasText: /^K04\s*·/ }).first();
  await k04.waitFor({ timeout: 30000 });
  assert.match(await k04.innerText(), /Industry reference/);
  await screenshot("02-reference-library.png");
  await k04.getByRole("button", { name: "Versions", exact: true }).click();
  const version = library.getByRole("button", { name: /^v\d+ · View document$/ }).first();
  await version.waitFor({ timeout: 30000 });
  const versionLabel = await version.innerText();
  await screenshot("03-reference-versions.png");
  await version.click();
  const content = page.locator("[data-knowledge-document]");
  await content.locator(".assistant-markdown").waitFor({ timeout: 30000 });
  assert.match(await content.innerText(), /MY2N-GS-R/);
  assert.match(await content.innerText(), /37\.7|636/);
  report.checks.push(`K04 ${versionLabel} knowledge document content opened`);
  await screenshot("04-reference-document.png");
  await page.getByRole("button", { name: "Close settings", exact: true }).last().click();
  await page.getByRole("button", { name: "Close settings", exact: true }).click();

  // This marked document is retained for the operator to withdraw by its exact returned path.
  const fixture = process.env.FORGEFLOW_UPLOAD_FIXTURE;
  const extension = fixture ? extname(fixture).toLowerCase() : ".txt";
  assert.ok([".txt", ".pdf", ".docx"].includes(extension), "Acceptance fixture must be TXT, PDF, or DOCX");
  const filename = `UI_ACCEPTANCE_${new Date().toISOString().slice(0, 10)}_${randomUUID().slice(0, 8)}${extension}`;
  const payload = fixture ? await readFile(fixture) : Buffer.from("ForgeFlow public UI acceptance document.\nPurpose: verify upload and indexing status.\nThis document contains no business orders, policies, product specifications, or customer data.\n");
  const uploadResponse = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.origin === origin.origin && url.pathname === "/api/knowledge/documents" && response.request().method() === "POST";
  }, { timeout: 60000 });
  await page.locator('[data-home-view] > input[type="file"]').setInputFiles({ name: filename, mimeType: extension === ".pdf" ? "application/pdf" : extension === ".docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "text/plain", buffer: payload });
  const response = await uploadResponse;
  const uploaded = await response.json();
  report.upload_response = { filename, status: response.status(), detail: uploaded.detail ?? null };
  assert.ok(response.ok(), `Upload returned HTTP ${response.status()}: ${uploaded.detail ?? "unknown"}`);
  report.uploaded_test_document = { filename, source_path: uploaded.source_path, job_id: uploaded.job_id ?? null };
  const uploadStatus = page.locator('[data-home-task-panel="upload"] [role="status"]');
  await uploadStatus.waitFor();
  await screenshot("05-upload-processing.png");
  await page.waitForFunction(() => document.querySelector('[data-home-task-panel="upload"] [role="status"]')?.textContent === "Document ready. Ask a question and inspect its sources.", undefined, { timeout: 180000 });
  report.checks.push("uploaded test document became searchable");
  await screenshot("06-upload-ready.png");
  await page.locator(".home-task-close").click();

  const question = "根据知识库中的原厂规格和渠道资料，MY2N-GS-R DC24 的线圈电流为什么出现 36.3 mA 和 37.7 mA 两种数值？请给出依据和资料引用。";
  await page.locator('.composer [contenteditable="true"]').fill(question);
  await page.locator("button.send").click();
  const citation = page.locator("[data-answer-citations] button").filter({ hasText: /K04\.md/ }).first();
  await citation.waitFor({ timeout: 180000 });
  report.checks.push("live model answer exposed K04 citation");
  const answer = await page.locator(".msg.assistant").last().innerText();
  await writeFile(resolve(out, "knowledge-answer.txt"), answer + "\n", { mode: 0o600 });
  await screenshot("07-cited-answer.png");
  const citationLabel = await citation.innerText();
  const citedVersion = / · v(\d+)/.exec(citationLabel)?.[1];
  assert.ok(citedVersion, "Live citation must include the captured document version");
  const citationRequestStart = report.document_requests.length;
  await citation.click();
  await content.waitFor();
  await content.locator("details summary").waitFor({ timeout: 30000 });
  await content.locator("details summary").click();
  assert.match(await content.innerText(), /MY2N-GS-R/);
  assert.ok(report.document_requests.slice(citationRequestStart).some(request => request.path.endsWith("K04.md/blocks")
    && request.version === citedVersion && request.status === 200), "Citation fetch must request the captured K04 version");
  report.checks.push(`citation opened captured passage and full text for v${citedVersion}`);
  await screenshot("08-citation-expanded.png");
  assert.deepEqual(errors, []);
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  report.failure = error instanceof Error ? error.message : String(error);
  if (page) {
    await page.screenshot({ path: resolve(out, "failure.png"), fullPage: true, animations: "disabled" }).catch(() => {});
  }
  process.exitCode = 1;
} finally {
  report.finished_at = new Date().toISOString();
  await writeFile(resolve(out, "public-ui-validation.json"), JSON.stringify(report, null, 2) + "\n", { mode: 0o600 });
  await context?.close();
  await browser.close();
  console.log(JSON.stringify({ status: report.status, report: resolve(out, "public-ui-validation.json"), checks: report.checks.length }));
}
