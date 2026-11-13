/**
 * Executable acceptance for v13 T01–T03 and T06.
 *
 * Drives the real built UI against controlled mock states and asserts what a
 * reader can actually see:
 *   T01  a required check the plan never recorded appears, named, as "not recorded"
 *   T02  the sticky decision bar exists and is visible when approval is blocked
 *   T03  a deployment-level block is described as deployment-level, with recovery
 *   T06  the create note stays visible while the create button is disabled
 *
 * Mock API only; no ERP writes. Exits non-zero on the first failed assertion.
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = 4184;
const root = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const server = spawn(process.env.FORGEFLOW_TEST_PYTHON || "python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise((resolve) => setTimeout(resolve, 600));

const id = "a".repeat(32);
const planId = "c".repeat(64);
const checks = [
  { code: "margin", label: "Margin floor", status: "pass", actual: "80%", threshold: "27.5%", source: "bound plan" },
  { code: "budget", label: "Budget cap", status: "pass", actual: "1", threshold: "2", source: "bound plan" },
  { code: "demand", label: "Demand coverage", status: "pass", actual: "50 units", threshold: "50 units", source: "bound plan" },
  { code: "policy", label: "Policy binding", status: "pass", actual: "required", threshold: "required", source: "bound plan" },
];

function caseFile({ unknownChecks = [], canApprove = true, state = "awaiting_approval" } = {}) {
  return {
    case_id: id, scenario: "A", state, synthetic: true,
    order: { ref: "SO-1..5", product: "Clean Room Sealed Fixture", units: 50, due_days: 8, customer: "Northwind Manufacturing" },
    disruption: { kind: "supplier_cancellation", vendor: "Apex Components" },
    plan: {
      plan_id: planId,
      actions: [{ kind: "cancel_po", document: "PO-8841", quantity: 3, summary: "Cancel disrupted supply" }],
      margin: null, on_time: null, new_spend: null, cost_delta: null,
      policy_sha256: "d".repeat(64), snapshot_sha256: "e".repeat(64),
      prepared_at: "2026-10-22T12:00:00Z",
      checks,
    },
    revision_limits: { original_margin_pct: 27.5, effective_margin_pct: 27.5, max_exclusive_pct: 100, decimal_places: 2 },
    approval: null, execution: null,
    created_at: "2026-10-22T12:00:00Z", updated_at: "2026-10-22T12:00:00Z", reason: null,
    decision: {
      can_approve: canApprove,
      reason: canApprove ? null : unknownChecks.length ? "required_checks_unknown" : "product_execution_unavailable",
      blocked_checks: [], unknown_checks: unknownChecks, warnings: [],
      can_reject: true, can_request_changes: true, can_reconcile: false, revision_reason: null,
    },
    result: { execution_status: null, readback_status: "unknown", verifier_status: "unknown", strict_success: null, evaluation_valid: null, verified: false, outcome: "awaiting_approval" },
  };
}

const assertions = [];
function check(ok, label, detail = "") {
  assertions.push({ ok, label, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
}

const browser = await chromium.launch({ channel: "chrome", headless: true });

async function open({ capabilities, cases, hash, lang = "en" }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: "en-US" });
  await context.addInitScript(value => localStorage.setItem("forgeflow.lang", value), lang);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") return route.fulfill({ json: { scope_id: "owner-a", display_name: "Alex Chen", deployment: capabilities.deployment || "local" } });
    if (path === "/api/config") return route.fulfill({ json: { deployment: capabilities.deployment || "local", runner_enabled: false, dsh_web_url: "http://example.invalid/" } });
    if (path === "/api/capabilities") return route.fulfill({ json: capabilities });
    if (path === "/api/product/cases") return route.fulfill({ json: cases });
    if (path === `/api/product/cases/${id}`) return route.fulfill({ json: cases[0] });
    if (path === "/api/runs") return route.fulfill({ json: [] });
    return route.fulfill({ json: [] });
  });
  await page.goto(`http://127.0.0.1:${port}/app/${hash}`, { waitUntil: "networkidle" });
  return { context, page, errors };
}

const liveCaps = (approve) => ({
  product_scenarios: [{ id: "A", task: "2280_medium_repair_plan_medium", can_create: approve }],
  case_read: true, case_create: approve, case_prepare: approve,
  case_approve: approve, product_approval: approve,
  environment: { execution: approve ? "synthetic_executor" : "unavailable", scenario: approve ? "A" : null, ready: approve },
});

try {
  // ---- T02 + baseline: approvable case keeps a visible sticky bar ----------
  {
    const { context, page } = await open({ capabilities: liveCaps(true), cases: [caseFile()], hash: `#case=${id}` });
    await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
    const sticky = page.locator("[data-case-approve-sticky]");
    check(await sticky.count() === 1, "T02 approvable: sticky approve bar exists");
    const box = await sticky.boundingBox();
    check(!!box && box.y + box.height <= 900 && box.y >= 0, "T02 approvable: sticky bar is inside the 1440x900 first screen", box ? `y=${Math.round(box.y)}` : "no box");
    check(await sticky.isEnabled(), "T02 approvable: sticky approve is enabled");
    check(await page.locator("[data-sticky-block-reason]").count() === 0, "T02 approvable: no block reason shown");
    await context.close();
  }

  // ---- T01: a required check the plan never recorded is shown and named -----
  {
    const { context, page } = await open({
      capabilities: liveCaps(true),
      cases: [caseFile({ unknownChecks: ["on_time"], canApprove: false })],
      hash: `#case=${id}`,
    });
    await page.locator("[data-policy-checks]").waitFor({ timeout: 15000 });
    const row = page.locator('[data-check-missing="true"]');
    check(await row.count() === 1, "T01 missing check: exactly one not-recorded row rendered");
    const text = (await row.first().innerText()).replace(/\s+/g, " ");
    check(/not recorded/i.test(text), "T01 missing check: row is labelled not recorded", text);
    check(/On-time delivery/i.test(text), "T01 missing check: the missing check is named", text);
    const counts = (await page.locator("[data-check-counts]").innerText()).replace(/\s+/g, " ");
    check(/1 not recorded/i.test(counts), "T08 counts: the summary counts the not-recorded check", counts);
    const note = await page.locator("[data-check-missing-note]").innerText();
    check(/On-time delivery/i.test(note), "T01 missing check: the note names it", note.split("\n")[0]);
    check(await page.locator("[data-legacy-plan]").count() === 1, "T04 legacy plan: historical-only notice shown");
    await context.close();
  }

  // ---- T02 + T03: blocked case still shows a bar, and says the real reason --
  {
    const { context, page } = await open({
      capabilities: liveCaps(true),
      cases: [caseFile({ unknownChecks: ["on_time"], canApprove: false })],
      hash: `#case=${id}`,
    });
    await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
    const sticky = page.locator("[data-case-approve-sticky]");
    check(await sticky.count() === 1, "T02 blocked: sticky bar still rendered");
    const box = await sticky.boundingBox();
    check(!!box && box.y + box.height <= 900 && box.y >= 0, "T02 blocked: sticky bar still inside the first screen", box ? `y=${Math.round(box.y)}` : "no box");
    check(!(await sticky.isEnabled()), "T02 blocked: approve is disabled");
    const reason = await page.locator("[data-sticky-block-reason]").innerText();
    check(/On-time delivery/i.test(reason), "T03 blocked: sticky bar names the missing check", reason);
    const recovery = await page.locator("[data-sticky-block-recovery]").innerText();
    check(recovery.trim().length > 10, "T03 blocked: sticky bar states how to unblock", recovery);
    await context.close();
  }

  // ---- T03: deployment-level block is not reported as a check problem -------
  {
    const { context, page } = await open({
      capabilities: liveCaps(false),
      cases: [caseFile({ canApprove: false })],
      hash: `#case=${id}`,
    });
    await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
    const stickyReason = await page.locator("[data-sticky-block-reason]").innerText();
    check(/no executor connected/i.test(stickyReason), "T03 deployment: reason is deployment-level", stickyReason);
    check(!/unknown\/not measured|conclusive result/i.test(stickyReason), "T03 deployment: reason is not the old vague fallback", stickyReason);
    const recovery = await page.locator("[data-sticky-block-recovery]").innerText();
    check(/check connection/i.test(recovery), "T03 deployment: recovery names the next step", recovery);
    const kind = await page.locator("[data-decision-disabled]").getAttribute("data-block-kind");
    check(kind === "deployment", "T03 deployment: the decision panel agrees on the cause", String(kind));
    await context.close();
  }

  // ---- T06: the create note stays while the button is disabled -------------
  {
    const { context, page } = await open({ capabilities: liveCaps(false), cases: [], hash: "#cases" });
    await page.locator("[data-cases-view]").waitFor({ timeout: 15000 });
    const create = page.locator("[data-create-case]");
    check(await create.count() === 1 && !(await create.isEnabled()), "T06 create: button is disabled in a read-only workspace");
    const note = await page.locator("[data-create-note]").innerText();
    check(note.trim().length > 20, "T06 create: the note is still visible while disabled", note);
    check(await page.locator("[data-create-note]").getAttribute("data-create-note") === "unavailable", "T06 create: the note states the unavailable case");
    await context.close();
  }
  for (const lang of ["en", "zh"]) {
    const busyCaps = { ...liveCaps(false), deployment: "hosted", reason: "environment_busy",
      environment: { execution: "live_sandbox", ready: true, scenario: "A", lease: { reason: "environment_busy", available: false } } };
    const busyPattern = lang === "zh" ? /另一位评委/ : /another reviewer/i;
    const nextPattern = lang === "zh" ? /管理员.*下一轮/ : /administrator.*next round/i;
    {
      const { context, page, errors } = await open({ capabilities: busyCaps, cases: [], hash: "#cases", lang });
      await page.locator('[data-cases-ready="true"]').waitFor({ timeout: 15000 });
      const note = await page.locator("[data-create-note]").innerText();
      check(busyPattern.test(note) && nextPattern.test(note), `${lang}: busy create explains the next reviewer round`, note);
      check(!(await page.locator("[data-create-case]").isEnabled()), `${lang}: busy create remains disabled`);
      check(!/executor|执行器|owner-a/i.test(note), `${lang}: busy create exposes no executor instructions or owner identity`);
      check(errors.length === 0, `${lang}: busy create has no page errors`, errors.join("; "));
      await context.close();
    }
    {
      const { context, page } = await open({ capabilities: busyCaps, cases: [caseFile()], hash: `#case=${id}`, lang });
      await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
      check(busyPattern.test(await page.locator("[data-sticky-block-reason]").innerText()), `${lang}: busy approval uses capabilities reason`);
      check(nextPattern.test(await page.locator("[data-sticky-block-recovery]").innerText()), `${lang}: busy approval names administrator recovery`);
      check(await page.locator("[data-decision-disabled]").getAttribute("data-block-kind") === "environment", `${lang}: approval panel agrees with busy sticky bar`);
      check(!(await page.locator("[data-case-approve-sticky]").isEnabled()), `${lang}: busy approval remains disabled`);
      await context.close();
    }
    {
      const oldCase = caseFile();
      oldCase.decision = { ...oldCase.decision, can_approve: false, can_reject: false,
        can_request_changes: false, reason: "environment_generation_mismatch", environment_current: false };
      const { context, page } = await open({ capabilities: { ...liveCaps(true), deployment: "hosted" }, cases: [oldCase], hash: `#case=${id}`, lang });
      await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
      const reason = await page.locator("[data-sticky-block-reason]").innerText();
      const recovery = await page.locator("[data-sticky-block-recovery]").innerText();
      check((lang === "zh" ? /上一轮.*仅供查看/ : /previous round.*viewing only/i).test(reason), `${lang}: old case is explicitly read-only`, reason);
      check((lang === "zh" ? /新.*案例/ : /new case/i).test(recovery), `${lang}: old case directs reviewer to create a new case`, recovery);
      check(await page.locator('[data-environment-restriction="historical"]').count() === 1, `${lang}: historical notice remains visible above case details`);
      check(!(await page.locator("[data-case-approve-sticky]").isEnabled()), `${lang}: old case cannot approve even when the current round is available`);
      check(!/executor|执行器|owner-a/i.test(reason + recovery), `${lang}: old case has no local setup instructions or identity`);
      await context.close();
    }
  }
} catch (error) {
  check(false, "harness completed", error instanceof Error ? error.message : String(error));
} finally {
  await browser.close();
  server.kill();
  const failed = assertions.filter((item) => !item.ok);
  console.log(`\n${assertions.length - failed.length}/${assertions.length} assertions passed`);
  process.exitCode = failed.length ? 1 : 0;
}
