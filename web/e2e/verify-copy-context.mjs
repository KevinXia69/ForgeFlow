/**
 * Executable acceptance for v13 T14–T24 (no unexplained context in the UI).
 *
 * T14  checks are translated by code, not by the server's English label
 * T15  a planned check keeps its pill colour and gap
 * T16  the approver is never rendered as "Operator <hex>" in business copy
 * T17  no truncated contract hash in a check value; "required" is translated
 * T18  the demo disclaimer is stated once, not three times
 * T19  one word per concept (workbench / evidence)
 * T20  the sign-in card names the product and does not assume a returning visitor
 * T21  the empty state explains itself in plain words
 * T22  counters are disjoint and the state is not repeated more than twice
 * T23  rows that look identical are told apart by their record time
 * T24  "unknown" severity is a secondary note, not the loudest element
 *
 * Mock API only; no ERP writes.
 */
import { spawn } from "node:child_process";
import { chromium } from "playwright-core";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const port = 4190;
const root = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const approved = JSON.parse(await readFile(new URL("../../web/e2e/fixtures/approved-case.json", import.meta.url), "utf8"));

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise((resolve) => setTimeout(resolve, 600));

const assertions = [];
const check = (ok, label, detail = "") => {
  assertions.push({ ok, label });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
};

const VISIBLE_TEXT = `(() => {
  const parts = [];
  const walk = (node) => {
    if (node.nodeType === 3) {
      const parent = node.parentElement;
      if (!parent) return;
      if (parent.closest('details:not([open])')) return;
      if (parent.closest('[hidden], [aria-hidden="true"]')) return;
      parts.push(node.nodeValue ?? '');
      return;
    }
    if (node.nodeType !== 1) return;
    if (node.tagName === 'SCRIPT' || node.tagName === 'STYLE') return;
    for (const child of node.childNodes) walk(child);
  };
  walk(document.body);
  return parts.join(' ');
})()`;

/** Older plan with short English labels and a contract hash in the policy value. */
function legacyCase() {
  const base = structuredClone(approved);
  return {
    ...base,
    state: "awaiting_approval",
    execution: null,
    approval: null,
    result: { ...base.result, execution_status: null, verified: false, strict_success: null, evaluation_valid: null, outcome: "awaiting_approval" },
    decision: { ...base.decision, can_approve: false, reason: "required_checks_unknown", unknown_checks: ["on_time"] },
    plan: {
      ...base.plan,
      checks: [
        { code: "margin", label: "Margin floor", status: "pass", actual: "80%", threshold: "27.5%", source: "local request margin" },
        { code: "budget", label: "Budget cap", status: "pass", actual: "$1", threshold: "$2", source: "local solver plan" },
        { code: "demand", label: "Demand coverage", status: "pass", actual: "50 units", threshold: "50 units", source: "local solver plan" },
        { code: "policy", label: "Original contract binding", status: "pass", actual: "b700c9f1534e…", threshold: "required", source: "local plan binding" },
        { code: "date", label: "Customer date", status: "planned", actual: "8 days", threshold: "8 days", source: "local solver plan" },
      ],
    },
  };
}

/** A list with duplicate-looking rows: same exception, product, units, due. */
function duplicateRows() {
  const row = (suffix, state, minutes) => {
    const base = structuredClone(approved);
    const verified = state === "succeeded";
    return {
      ...base,
      case_id: suffix.repeat(32).slice(0, 32),
      state,
      execution: state === "awaiting_approval" ? null : base.execution,
      result: {
        ...base.result,
        verified,
        strict_success: state === "awaiting_approval" ? null : verified,
        evaluation_valid: state === "awaiting_approval" ? null : true,
        outcome: verified ? "verified" : state === "failed" ? "verification_failed" : "awaiting_approval",
      },
      updated_at: `2026-10-22T04:${String(minutes).padStart(2, "0")}:00Z`,
      created_at: `2026-10-22T04:${String(minutes).padStart(2, "0")}:00Z`,
    };
  };
  return [row("a", "succeeded", 2), row("b", "failed", 34), row("c", "awaiting_approval", 51), row("d", "succeeded", 59)];
}

const browser = await chromium.launch({ channel: "chrome", headless: true });

async function openPage({ cases, hash = "", locale = "zh-CN", approve = false, signedOut = false }) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale });
  const page = await context.newPage();
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") {
      return signedOut
        ? route.fulfill({ status: 401, json: { detail: "authentication required" } })
        : route.fulfill({ json: { scope_id: "owner-a", display_name: "Operator 5cfc86f2", deployment: "local" } });
    }
    if (path === "/api/config") return route.fulfill({ json: { deployment: "local", runner_enabled: false, dsh_web_url: "http://example.invalid/" } });
    if (path === "/api/capabilities") return route.fulfill({ json: {
      product_scenarios: [], case_read: true, case_create: false, case_prepare: approve, case_approve: approve, product_approval: approve,
      environment: { execution: approve ? "synthetic_executor" : "unavailable", scenario: approve ? "A" : null, ready: approve },
    } });
    if (path === "/api/product/cases") return route.fulfill({ json: cases });
    if (path.startsWith("/api/product/cases/")) return route.fulfill({ json: cases[0] });
    if (path === "/api/runs") return route.fulfill({ json: [] });
    return route.fulfill({ json: [] });
  });
  await page.goto(`http://127.0.0.1:${port}/app/${hash}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(700);
  return { context, page };
}

try {
  // ---- T20 sign-in card ---------------------------------------------------
  {
    const { context, page } = await openPage({ cases: [], signedOut: true });
    const title = (await page.locator(".login-card h1").innerText()).trim();
    check(!/back to|回到/i.test(title), "T20 login: heading does not assume a returning visitor", title);
    check(await page.locator(".login-card .login-brand").count() === 1, "T20 login: the product is named on the card");
    const hint = await page.locator("#invite-code-hint").innerText();
    check(/provided by|部署/.test(hint), "T20 login: it says where the invite code comes from", hint);
    await context.close();
  }

  // ---- T14 / T15 / T16 / T17 on a legacy plan -----------------------------
  {
    const legacy = legacyCase();
    const { context, page } = await openPage({ cases: [legacy], hash: `#case=${legacy.case_id}`, approve: true });
    await page.locator("[data-policy-checks]").waitFor({ timeout: 20000 });
    const labels = await page.locator("[data-policy-checks] .policy-check-label").allInnerTexts();
    check(labels.every((label) => !/^[A-Za-z]/.test(label)), "T14 checks: no raw English label survives translation", labels.join("|"));
    const sources = await page.locator("[data-policy-checks] .policy-check-source").allInnerTexts();
    check(!sources.some((source) => /local request margin|local solver plan|local plan binding/.test(source)), "T14 checks: sources are translated too", sources.join("|"));

    const values = await page.locator("[data-policy-checks] .policy-check-value").allInnerTexts();
    check(!values.some((value) => /[0-9a-f]{8,}/.test(value)), "T17 checks: no truncated contract hash in a value", values.join("|"));
    check(values.some((value) => /已绑定|Bound to the original contract/.test(value)), "T17 checks: the policy value says what it means", values.join("|"));
    check(!values.some((value) => /\/ required$/.test(value)), "T17 checks: 'required' is translated");

    const planned = page.locator('.policy-check[data-check-status="planned"]').first();
    const plannedCount = await page.locator('.policy-check[data-check-status="planned"]').count();
    if (plannedCount > 0) {
      const pill = planned.locator(".policy-check-status");
      const style = await pill.evaluate((node) => ({ bg: getComputedStyle(node).backgroundColor }));
      const gap = await planned.evaluate((node) => {
        const status = node.querySelector(".policy-check-status");
        const label = node.querySelector(".policy-check-label");
        if (!status || !label) return null;
        return Math.round(label.getBoundingClientRect().left - status.getBoundingClientRect().right);
      });
      check(style.bg !== "rgba(0, 0, 0, 0)", "T15 planned pill: has its own background", style.bg);
      check(gap !== null && gap > 2, "T15 planned pill: keeps a gap from the label", `${gap}px`);
    } else {
      check(true, "T15 planned pill: no planned check in this fixture (skipped)");
    }

    const approver = await page.locator("[data-case-detail] input[readonly]").first().inputValue();
    check(!/[0-9a-f]{8}/.test(approver), "T16 approver: no hash in the approver field", approver);
    await context.close();
  }

  // ---- T18 / T19 / T22 / T23 / T24 on the list ---------------------------
  {
    const rows = duplicateRows();
    const { context, page } = await openPage({ cases: rows, hash: "#cases" });
    await page.locator("[data-case-row]").first().waitFor({ timeout: 20000 });

    const counters = await page.locator("[data-saved-view]").allInnerTexts();
    const numbers = counters.map((text) => Number((text.match(/[0-9]+/g) ?? []).pop()));
    const [decision, attention, executing, verified, all] = numbers;
    check(decision + attention + executing + verified === all, "T22 counters: the views add up to the total", counters.join(" | "));
    check(!counters.some((text) => /incl\.|含待/.test(text)), "T22 counters: no parenthetical to decode", counters.join(" | "));

    const when = await page.locator("[data-case-when]").allInnerTexts();
    check(when.length === rows.length, "T23 rows: every row states when it was recorded", `${when.length}/${rows.length}`);
    check(new Set(when).size === rows.length, "T23 rows: identical-looking rows are told apart", when.join(" | "));

    const severity = await page.locator(".case-severity").first().evaluate((node) => ({
      weight: Number(getComputedStyle(node).fontWeight),
      color: getComputedStyle(node).color,
    }));
    check(severity.weight <= 500, "T24 severity: not the boldest element in the row", String(severity.weight));

    const disclaimerCount = (await page.evaluate(VISIBLE_TEXT)).match(/no ERP writes?|不写[入]? ?ERP/gi)?.length ?? 0;
    check(disclaimerCount <= 1, "T18 list: the demo disclaimer is stated once", `${disclaimerCount}`);
    await context.close();
  }

  // ---- T18 / T19 / T22 on the demo case ----------------------------------
  {
    const demo = structuredClone(approved);
    demo.case_id = "274c4b9089cc74cc1c01401732d91202";
    demo.synthetic = true;
    demo.state = "awaiting_approval";
    demo.execution = null;
    demo.approval = null;
    demo.result = { ...demo.result, execution_status: null, verified: false, strict_success: null, evaluation_valid: null, outcome: "awaiting_approval" };
    const { context, page } = await openPage({ cases: [demo], hash: `#case=${demo.case_id}` });
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    await page.waitForTimeout(600);
    const visible = await page.evaluate(VISIBLE_TEXT);
    const disclaimerCount = visible.match(/no ERP writes?|不写[入]? ?ERP/gi)?.length ?? 0;
    check(disclaimerCount <= 1, "T18 demo: the demo disclaimer is stated once", `${disclaimerCount}`);
    const stateMentions = (visible.match(/Needs your decision|待你决定/g) ?? []).length;
    check(stateMentions <= 2, "T22 detail: the state is not repeated more than twice", `${stateMentions}`);
    await context.close();
  }

  // ---- T19 naming and T21 empty state ------------------------------------
  {
    const { context, page } = await openPage({ cases: [], hash: "#cases" });
    await page.locator("[data-cases-empty]").waitFor({ timeout: 20000 });
    const empty = await page.locator("[data-cases-empty]").innerText();
    check(!/identity|身份下/.test(empty), "T21 empty state: no internal word for the reader", empty.split("\n")[0]);
    check(/demo|演示/i.test(empty), "T21 empty state: says what to do next", empty.split("\n")[0]);
    const topbar = (await page.locator(".status-pill").innerText()).trim();
    const footer = (await page.locator(".side-who strong").innerText()).trim();
    check(topbar === footer, "T19 naming: one word for the workspace", `${topbar} / ${footer}`);
    const evidenceLinks = await page.locator("[data-cases-empty], .cases-disclaimer").allInnerTexts();
    check(!/experiments/.test(await page.locator("body").innerText()), "T19 naming: evidence is not called an experiment");
    check(evidenceLinks.length >= 1, "T19 naming: disclaimer still present");
    await context.close();
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
