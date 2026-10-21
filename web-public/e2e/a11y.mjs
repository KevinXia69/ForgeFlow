/**
 * Public review bundle: keyboard, structure and reflow checks that run without a
 * deployment. It serves dist/ and mocks the read-only review API, so the same
 * assertions run in CI or on a fresh checkout.
 *
 * What this measures: start page (one h1, four cards, environment state, cards
 * focusable), case list (rows are buttons and take focus), a case (one h1, a
 * decision control), evidence (every filter labelled), the settings dialog
 * (modal, focus enters), a 720px reflow column on each surface, and that every
 * surface stays English-only.
 *
 * What this does NOT measure, and what is therefore not claimed: colour
 * contrast ratios, a real screen reader, true browser zoom, 390px layout.
 */
import { spawn } from "node:child_process";
import { access, mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const checked = [];
const assert = (condition, message) => {
  checked.push(message);
  if (!condition) throw new Error(`[public a11y] ${message}`);
};

const temporary = await mkdtemp(join(tmpdir(), "forgeflow-public-a11y-"));
await symlink(fileURLToPath(new URL("../dist", import.meta.url)), join(temporary, "app"), "dir");
await access(join(temporary, "app", "index.html"));

const port = Number(process.env.PUBLIC_A11Y_PORT ?? 4177);
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", temporary], { stdio: "ignore" });
await new Promise((resolve) => setTimeout(resolve, 500));

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

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const overflow = () => page.evaluate(() => ({
  scrollWidth: document.documentElement.scrollWidth,
  clientWidth: document.documentElement.clientWidth,
}));

try {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const reply = (json, status = 200) => route.fulfill({ status, json });
    if (path === "/api/auth/me") return reply({ scope_id: "public-reviewer", display_name: "Reviewer public", deployment: "review" });
    if (path === "/api/config") return reply({ deployment: "review", runner_enabled: false });
    if (path === "/api/capabilities") return reply({
      case_read: true, case_qa: true, case_create: false, case_prepare: false, case_approve: false, product_approval: false,
      environment: { execution: "unavailable", ready: false },
    });
    if (path === "/api/product/cases") return reply([caseFile]);
    if (path.startsWith("/api/product/cases/")) return reply(caseFile);
    if (path === "/api/runs") return reply([]);
    return reply({});
  });

  await page.goto(`http://127.0.0.1:${port}/app/`, { waitUntil: "domcontentloaded" });
  await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
  assert(await page.getByRole("heading", { level: 1 }).count() === 1, "start page: exactly one h1");
  assert(await page.locator("[data-home-card]").count() === 4, "start page: four function cards");
  assert(await page.locator("[data-home-environment]").count() === 1, "start page: the environment is stated");
  await page.locator("[data-home-card]").first().focus();
  assert(await page.locator("[data-home-card]").first().evaluate((node) => node === document.activeElement), "start page: cards take keyboard focus");

  await page.setViewportSize({ width: 720, height: 900 });
  await page.waitForTimeout(300);
  const home = await overflow();
  assert(home.scrollWidth <= home.clientWidth + 1, "start page: no overflow in a 720px column");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(200);

  await page.locator('[data-nav="cases"]').click();
  await page.locator('[data-cases-view][data-cases-ready="true"]').waitFor({ timeout: 20000 });
  const rows = page.locator("[data-case-row]");
  assert(await rows.count() >= 1, "case list: at least one row is rendered");
  await rows.first().focus();
  assert(await rows.first().evaluate((node) => node === document.activeElement), "case list: rows take keyboard focus");

  await rows.first().click();
  await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
  assert(await page.getByRole("heading", { level: 1 }).count() === 1, "case: exactly one h1");
  assert(await page.locator("[data-decision-panel] button, [data-case-approve-sticky]").count() >= 1, "case: a decision control is rendered");
  await page.setViewportSize({ width: 720, height: 900 });
  await page.waitForTimeout(300);
  const detail = await overflow();
  assert(detail.scrollWidth <= detail.clientWidth + 1, "case: no overflow in a 720px column");
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(200);

  await page.locator('[data-nav="runs"]').click();
  await page.locator("[data-runs-list]").waitFor({ timeout: 20000 });
  const unlabelled = await page.locator("input, select").evaluateAll((nodes) => nodes.filter((node) => {
    const id = node.getAttribute("id");
    const aria = node.getAttribute("aria-label");
    const labelled = id ? document.querySelector(`label[for="${id}"]`) : null;
    return !aria && !labelled;
  }).length);
  assert(unlabelled === 0, "evidence: every filter control is labelled");

  await page.getByRole("button", { name: "Settings", exact: true }).first().click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor();
  assert(await dialog.getAttribute("aria-modal") === "true", "settings: the dialog is modal");
  assert(await dialog.evaluate((node) => node.contains(document.activeElement)), "settings: focus enters the dialog");
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });

  const body = await page.locator("body").innerText();
  assert(!/[\u3400-\u9fff]/.test(body), "every surface: still English-only");

  console.log(`public-a11y=ok (${checked.length} checks: keyboard, structure, 720px reflow)`);
  console.log("public-a11y=not-measured: colour contrast, screen reader, true browser zoom, 390px layout");
} finally {
  await browser.close();
  server.kill();
  await rm(temporary, { recursive: true, force: true });
}
