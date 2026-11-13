/**
 * Executable acceptance for v13 T28–T31 (start page and environment).
 *
 * T28  the four suggestions are business questions, each paired with its prompt
 * T29  the start page states whether approval is possible, and the card agrees
 * T30  the public review bundle carries the same start page
 * T31  the card carries the view in the route, not in sessionStorage
 *
 * Private bundle against a mock API, then the public bundle against its own mock.
 * No ERP writes.
 */
import { spawn } from "node:child_process";
import { access, mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const assertions = [];
const check = (ok, label, detail = "") => {
  assertions.push({ ok, label });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
};

const caseFile = {
  case_id: "a".repeat(32), scenario: "A", state: "awaiting_approval", synthetic: true,
  order: { ref: "SO-1..5", product: "Clean Room Sealed Fixture", units: 50, due_days: 8, customer: "Northwind Manufacturing" },
  disruption: { kind: "supplier_cancellation", vendor: "Apex Components" },
  plan: {
    plan_id: "c".repeat(64), actions: [{ kind: "cancel_po", document: "PO-8841", quantity: 50, summary: "Cancel disrupted supply" }],
    margin: null, on_time: null, new_spend: null, cost_delta: null,
    policy_sha256: "d".repeat(64), snapshot_sha256: "e".repeat(64), prepared_at: "2026-10-22T12:00:00Z",
    checks: [{ code: "budget", label: "Budget", status: "pass", actual: 1, threshold: 2, source: "local solver plan" }],
  },
  revision_limits: { original_margin_pct: 27.5, effective_margin_pct: 27.5, max_exclusive_pct: 100, decimal_places: 2 },
  approval: null, execution: null, created_at: "2026-10-22T12:00:00Z", updated_at: "2026-10-22T12:00:00Z", reason: null,
  decision: { can_approve: false, reason: "product_execution_unavailable", blocked_checks: [], unknown_checks: [], warnings: [], can_reject: false, can_request_changes: false, can_reconcile: false, revision_reason: null },
  result: { execution_status: null, readback_status: "unknown", verifier_status: "unknown", strict_success: null, evaluation_valid: null, verified: false, outcome: "awaiting_approval" },
};

async function serveStatic(dir, port) {
  const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", dir], { stdio: "ignore" });
  await new Promise((resolve) => setTimeout(resolve, 500));
  return server;
}

async function openStartPage({ port, locale, approve }) {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale });
  const page = await context.newPage();
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const reply = (json, status = 200) => route.fulfill({ status, json });
    if (path === "/api/auth/me") return reply({ scope_id: "owner-a", display_name: "Reviewer", deployment: "local" });
    if (path === "/api/config") return reply({ deployment: "local", runner_enabled: false });
    if (path === "/api/capabilities") return reply({
      case_read: true, case_qa: true, case_create: approve, case_prepare: approve, case_approve: approve, product_approval: approve,
      environment: { execution: approve ? "synthetic_executor" : "unavailable", ready: approve },
    });
    if (path === "/api/product/cases") return reply([caseFile]);
    if (path.startsWith("/api/product/cases/")) return reply(caseFile);
    if (path === "/api/runs") return reply([]);
    return reply({});
  });
  await page.goto(`http://127.0.0.1:${port}/app/`, { waitUntil: "networkidle" });
  await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
  await page.locator("[data-home-card]").first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(600);
  return { browser, context, page };
}

const PRIVATE_DIR = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
let publicTemp = null;
let privateServer = null;
let publicServer = null;

try {
  // ---- private bundle ----------------------------------------------------
  privateServer = await serveStatic(PRIVATE_DIR, 4191);
  {
    const { browser, page } = await openStartPage({ port: 4191, locale: "zh-CN", approve: false });

    // T28
    const presetLabels = await page.locator("[data-chat-presets] .chat-preset").allInnerTexts();
    check(presetLabels.length === 4, "T28 suggestions: four suggestions are shown", `${presetLabels.length}`);
    check(!presetLabels.some((label) => /边界|review|Agent|只读 Agent/i.test(label)), "T28 suggestions: none is framed as an internal concern", presetLabels.join("|"));
    check(await page.locator("[data-chat-presets] .chat-preset").first().getAttribute("aria-label") === null, "T28 suggestions: plain text, no hidden scope label");
    await page.locator("[data-chat-presets] .chat-preset").first().click();
    await page.waitForTimeout(400);
    const sent = await page.locator(".msg.user").first().innerText().catch(() => "");
    check(/案例|决定|案例需要我做决定/.test(sent), "T28 suggestions: clicking asks the question it shows", sent.slice(0, 40));

    // T29 read-only
    check(await page.locator("[data-home-environment]").count() === 1, "T29 environment: the start page states it");
    const readOnly = await page.locator("[data-home-environment]").getAttribute("data-home-environment");
    check(readOnly === "readonly", "T29 environment: a workspace that cannot approve says so", String(readOnly));
    const decisionHint = await page.locator('[data-home-card="decision"] .home-card-hint').innerText();
    check(/不能批准|cannot approve/.test(decisionHint), "T29 environment: the decision card agrees with it", decisionHint);
    await browser.close();
  }

  // T29 ready
  {
    const { browser, page } = await openStartPage({ port: 4191, locale: "zh-CN", approve: true });
    const ready = await page.locator("[data-home-environment]").getAttribute("data-home-environment");
    check(ready === "ready", "T29 environment: a connected executor is reported as ready", String(ready));
    const hint = await page.locator('[data-home-card="decision"] .home-card-hint').innerText();
    check(!/不能批准|cannot approve/.test(hint), "T29 environment: the card does not claim a read-only limit when ready", hint);
    await browser.close();
  }

  // T31 explicit route
  {
    const { browser, page } = await openStartPage({ port: 4191, locale: "zh-CN", approve: false });
    await page.evaluate(() => sessionStorage.clear());
    await page.locator('[data-home-card="verified"]').click();
    await page.waitForTimeout(700);
    const route = await page.evaluate(() => ({ hash: location.hash, active: document.querySelector('[data-saved-view][aria-pressed="true"]')?.getAttribute("data-saved-view") }));
    check(/view=verified/.test(route.hash), "T31 route: the card names the view in the hash", route.hash);
    check(route.active === "verified", "T31 route: the list opens on that view without stored state", String(route.active));
    await browser.close();
  }

  // ---- public bundle -----------------------------------------------------
  publicTemp = await mkdtemp(join(tmpdir(), "forgeflow-public-verify-"));
  await symlink(fileURLToPath(new URL("../../web-public/dist", import.meta.url)), join(publicTemp, "app"), "dir");
  await access(join(publicTemp, "app", "index.html"));
  publicServer = await serveStatic(publicTemp, 4192);
  {
    const { browser, page } = await openStartPage({ port: 4192, locale: "en-US", approve: false });
    check(await page.locator("[data-home-view]").count() === 1, "T30 public: the review bundle lands on the start page");
    check(await page.locator("[data-home-card]").count() === 4, "T30 public: it has the same four function cards");
    check(await page.locator("[data-home-environment]").count() === 1, "T30 public: it states the environment");
    const publicPresets = await page.locator("[data-chat-presets] .chat-preset").allInnerTexts();
    const privatePresetsEn = ["What is waiting on me", "How was it fixed last time", "Why approval first", "Is this real"];
    check(publicPresets.join("|") === privatePresetsEn.join("|"), "T30 public: the suggestions match the private ones", publicPresets.join("|"));
    // A new-chat control belongs at the top of the sidebar even when the
    // conversation is empty; what an empty conversation must not show is a
    // history list or a search box.
    check(await page.locator(".chat-threads").count() === 0, "T30 public: an empty conversation shows no history list");
    check(await page.locator('.chat-history-toolbar input[type="search"]').count() === 0, "T30 public: an empty conversation shows no search box");
    const body = await page.locator("body").innerText();
    check(!/[\u3400-\u9fff]/.test(body), "T30 public: still English-only");
    await browser.close();
  }
} catch (error) {
  check(false, "harness completed", error instanceof Error ? error.message : String(error));
} finally {
  privateServer?.kill();
  publicServer?.kill();
  if (publicTemp) await rm(publicTemp, { recursive: true, force: true });
  const failed = assertions.filter((item) => !item.ok);
  console.log(`\n${assertions.length - failed.length}/${assertions.length} assertions passed`);
  process.exitCode = failed.length ? 1 : 0;
}
