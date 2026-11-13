/**
 * Settles one question: on a case that CAN be approved (capabilities allow it),
 * is a visible approve control on the first screen, or only inside the rail?
 *
 * Mock API only; no ERP. Mirrors the shapes used by hermetic.mjs.
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = 4183;
const root = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise(resolve => setTimeout(resolve, 600));

const id = "a".repeat(32), planId = "c".repeat(64);
const base = {
  case_id: id, scenario: "A", state: "awaiting_approval", synthetic: true,
  order: { ref: "SO-1..5", product: "Clean Room Sealed Fixture", units: 50, due_days: 8, customer: "Northwind Manufacturing" },
  disruption: { kind: "supplier_cancellation", vendor: "Apex Components" },
  plan: {
    plan_id: planId,
    actions: [{ kind: "cancel_po", document: "PO-8841", quantity: 50, summary: "Cancel disrupted supply" }],
    margin: null, on_time: null, new_spend: null, cost_delta: null,
    policy_sha256: "d".repeat(64), snapshot_sha256: "e".repeat(64),
    prepared_at: "2026-10-22T12:00:00Z",
    checks: [{ code: "budget", label: "Budget", status: "pass", actual: 1, threshold: 2, source: "local solver plan" }],
  },
  revision_limits: { original_margin_pct: 27.5, effective_margin_pct: 27.5, max_exclusive_pct: 100, decimal_places: 2 },
  approval: null, execution: null, created_at: "2026-10-22T12:00:00Z", updated_at: "2026-10-22T12:00:00Z", reason: null,
  decision: { can_approve: true, reason: null, blocked_checks: [], unknown_checks: [], warnings: [], can_reject: true, can_request_changes: true, can_reconcile: false, revision_reason: null },
  result: { execution_status: null, readback_status: "unknown", verifier_status: "unknown", strict_success: null, evaluation_valid: null, verified: false, outcome: "awaiting_approval" },
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const findings = [];
try {
  for (const [label, approveCapability] of [["approval-allowed", true], ["approval-blocked(read-only)", false]]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await page.route("**/api/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/auth/me") return route.fulfill({ json: { scope_id: "owner-a", display_name: "Alex Chen", deployment: "local" } });
      if (path === "/api/config") return route.fulfill({ json: { deployment: "local", runner_enabled: false, dsh_web_url: "http://example.invalid/" } });
      if (path === "/api/capabilities") return route.fulfill({ json: {
        product_scenarios: [{ id: "A", task: "2280_medium_repair_plan_medium", can_create: approveCapability }],
        case_read: true, case_create: approveCapability, case_prepare: approveCapability,
        case_approve: approveCapability, product_approval: approveCapability,
        environment: { execution: "synthetic_executor", scenario: "A", ready: approveCapability },
      } });
      if (path === "/api/product/cases") return route.fulfill({ json: [base] });
      if (path === `/api/product/cases/${id}`) return route.fulfill({ json: base });
      if (path === "/api/runs") return route.fulfill({ json: [] });
      return route.fulfill({ json: [] });
    });
    await page.goto(`http://127.0.0.1:${port}/app/#case=${id}`, { waitUntil: "networkidle" });
    await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
    await page.waitForTimeout(900);

    const facts = await page.evaluate(() => {
      const sticky = document.querySelector("[data-case-approve-sticky]");
      const railApprove = document.querySelector("[data-case-approve]");
      const box = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { top: Math.round(r.top), bottom: Math.round(r.bottom), visibleInViewport: r.top < innerHeight && r.bottom > 0 };
      };
      return {
        viewportHeight: innerHeight,
        stickyPresent: !!sticky,
        stickyBox: box(sticky),
        stickyEnabled: sticky ? sticky.disabled === false : null,
        railApproveBox: box(railApprove),
        railApproveEnabled: railApprove ? railApprove.disabled === false : null,
        railScrollable: (() => {
          const rail = document.querySelector(".case-detail-rail");
          return rail ? { clientH: rail.clientHeight, scrollH: rail.scrollHeight } : null;
        })(),
        disabledReason: document.querySelector("[data-decision-disabled]")?.textContent?.trim() ?? null,
      };
    });
    findings.push(`\n=== ${label} ===\n${JSON.stringify(facts, null, 1)}`);
    await page.screenshot({ path: `/tmp/approve-${label}.png`, fullPage: false });
    await page.close();
  }
} finally {
  await browser.close();
  server.kill();
  console.log(findings.join("\n"));
}
