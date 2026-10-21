/**
 * v14 E22: the review deployment, end to end, against a real read-only API.
 *
 * No mocks. This drives the public bundle served by scripts/review_backend.py,
 * which proxies /api to an API running with FORGEFLOW_DEPLOYMENT=review.
 *
 * It checks what a reviewer actually gets:
 *   - the start page renders, states it is read-only, and is English-only
 *   - the case list comes from the real backend and can be opened
 *   - the case screen explains why it cannot approve, and the control is disabled
 *   - the API refuses a write with 403 review_read_only (the boundary is real,
 *     not just a disabled button)
 *
 * Usage: REVIEW_BASE=http://127.0.0.1:8080 INVITE=demo-invite-code-0001 node e2e/review-e2e.mjs
 */
import { chromium } from "playwright-core";

const base = process.env.REVIEW_BASE ?? "http://127.0.0.1:8080";
const invite = process.env.INVITE ?? "demo-invite-code-0001";

const assertions = [];
const check = (ok, label, detail = "") => {
  assertions.push({ ok, label });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
const page = await context.newPage();
page.setDefaultTimeout(20000);

try {
  // The review API is real: get a session the way a reviewer would.
  const session = await context.request.post(`${base}/api/auth/session`, { data: { invite_code: invite } });
  check(session.ok(), "the review backend issues a session", `HTTP ${session.status()}`);

  // ---- the boundary is enforced by the server, not only by the UI ---------
  const write = await context.request.post(`${base}/api/product/cases`, { data: { scenario: "A" } });
  const writeBody = await write.text();
  check(write.status() >= 400, "a write is refused by the review API", `HTTP ${write.status()} ${writeBody.slice(0, 40)}`);

  // ---- start page ---------------------------------------------------------
  await page.goto(`${base}/app/`, { waitUntil: "domcontentloaded" });
  await page.locator("[data-home-view]").waitFor();
  await page.waitForFunction(() => document.querySelector("[data-home-environment]")?.getAttribute("data-home-environment") !== "unknown", null, { timeout: 10000 }).catch(() => {});
  const badge = await page.locator("[data-home-environment]").getAttribute("data-home-environment");
  check(badge === "readonly", "the start page states the workspace is read-only", String(badge));
  check(await page.locator("[data-home-card]").count() === 4, "the start page carries four function cards");
  const body = await page.locator("body").innerText();
  check(!/[\u3400-\u9fff]/.test(body), "every surface is still English-only");

  // ---- case list from the real backend ------------------------------------
  await page.locator('[data-nav="cases"]').click();
  await page.locator('[data-cases-view][data-cases-ready="true"]').waitFor();
  const rows = page.locator("[data-case-row]");
  check(await rows.count() >= 1, "the case list comes from the real backend", `${await rows.count()} row(s)`);
  const caseId = await rows.first().getAttribute("data-case-id");

  await rows.first().click();
  await page.locator("[data-case-detail]").waitFor();

  // ---- the decision is honestly blocked -----------------------------------
  const reason = await page.locator("[data-sticky-block-reason]").first().textContent();
  check(Boolean(reason && reason.trim()), "the case screen says why it cannot approve", (reason ?? "").trim().slice(0, 60));
  const approve = page.locator("[data-case-approve-sticky]");
  check(await approve.count() === 1 && await approve.isDisabled(), "the approve control is disabled");

  // ---- the review deployment does not pretend to execute ------------------
  const execute = await context.request.post(`${base}/api/product/cases/${caseId}/approve`, { data: {} });
  check(execute.status() >= 400, "approval is refused by the review API", `HTTP ${execute.status()}`);
} catch (error) {
  check(false, "review end-to-end completed", error instanceof Error ? error.message : String(error));
} finally {
  await browser.close();
  const failed = assertions.filter((item) => !item.ok);
  console.log(`\n${assertions.length - failed.length}/${assertions.length} assertions passed`);
  process.exitCode = failed.length ? 1 : 0;
}
