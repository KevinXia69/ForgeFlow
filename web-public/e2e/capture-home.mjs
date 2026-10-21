/**
 * Capture the public review start page for the v13 T30 comparison.
 * Serves the built public bundle and mocks the read-only review API.
 */
import { spawn } from "node:child_process";
import { access, mkdir, mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const tag = process.env.CAPTURE_TAG || "after";
const out = "/tmp/forgeflow-shots/";
await mkdir(`${out}screens/`, { recursive: true });

const temporary = await mkdtemp(join(tmpdir(), "forgeflow-public-capture-"));
await symlink(fileURLToPath(new URL("../dist", import.meta.url)), join(temporary, "app"), "dir");
await access(join(temporary, "app", "index.html"));
const server = spawn("python3", ["-m", "http.server", "4181", "--bind", "127.0.0.1", "--directory", temporary], { stdio: "ignore" });
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
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
const page = await context.newPage();
await page.route("**/api/**", (route) => {
  const path = new URL(route.request().url()).pathname;
  const reply = (json, status = 200) => route.fulfill({ status, json });
  if (path === "/api/auth/me") return reply({ scope_id: "public-reviewer", display_name: "Reviewer public", deployment: "review" });
  if (path === "/api/config") return reply({ deployment: "review", runner_enabled: false });
  if (path === "/api/capabilities") return reply({ case_read: true, case_qa: true, case_create: false, case_prepare: false, case_approve: false, product_approval: false, environment: { execution: "unavailable", ready: false } });
  if (path === "/api/product/cases") return reply([caseFile]);
  if (path.startsWith("/api/product/cases/")) return reply(caseFile);
  if (path === "/api/runs") return reply([]);
  return reply({});
});

await page.goto("http://127.0.0.1:4181/app/", { waitUntil: "networkidle" });
await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
await page.locator("[data-home-card]").first().waitFor();
await page.waitForTimeout(700);
await page.screenshot({ path: `${out}screens/public-${tag}-home.png`, fullPage: true });

const facts = await page.evaluate(() => ({
  hash: location.hash,
  activeNav: document.querySelector('[data-nav][aria-current="page"]')?.getAttribute("data-nav"),
  env: document.querySelector("[data-home-environment]")?.getAttribute("data-home-environment"),
  envText: document.querySelector("[data-home-environment]")?.textContent?.trim(),
  presets: [...document.querySelectorAll("[data-chat-presets] .chat-preset")].map((el) => el.textContent?.trim()),
  cards: [...document.querySelectorAll("[data-home-card]")].map((el) => ({
    id: el.getAttribute("data-home-card"),
    title: el.querySelector(".home-card-title")?.textContent?.trim(),
    count: el.querySelector(".home-card-count")?.textContent?.trim(),
  })),
  reviewBanner: document.querySelector(".review-banner")?.textContent?.trim(),
}));
console.log(JSON.stringify(facts, null, 1));

// The card must carry the view explicitly, like the private workbench.
await page.locator('[data-home-card="verified"]').click();
await page.waitForTimeout(800);
console.log("after verified card:", JSON.stringify(await page.evaluate(() => ({
  hash: location.hash,
  active: document.querySelector('[data-saved-view][aria-pressed="true"]')?.getAttribute("data-saved-view"),
}))));

await browser.close();
server.kill();
await rm(temporary, { recursive: true, force: true });
