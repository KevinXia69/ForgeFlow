/**
 * Executable acceptance for v14 stage B (E05, E06, E08, E09).
 *
 * E05  readback states are plain words; an unknown token is shown and marked
 * E06  the 51/51 rule count is stated once, not twice
 * E08  when the workspace cannot approve AND a required check is missing, both
 *      reasons are shown; with only the deployment cause, only that one is
 *
 * E07 (demo seed), E10 (port), E11 (decision) and E12 (wording) are checked
 * outside the browser; see the repository documentation.
 */
import { spawn } from "node:child_process";
import { chromium } from "playwright-core";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const port = 4198;
const root = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const approved = JSON.parse(await readFile(new URL("../../web/e2e/fixtures/approved-case.json", import.meta.url), "utf8"));

const missingCheck = {
  ...structuredClone(approved),
  case_id: "b".repeat(32),
  state: "awaiting_approval",
  execution: null,
  approval: null,
  result: { ...approved.result, execution_status: null, verified: false, strict_success: null, evaluation_valid: null, outcome: "awaiting_approval" },
  decision: { ...approved.decision, can_approve: false, reason: "product_execution_unavailable", unknown_checks: ["on_time"] },
  plan: { ...approved.plan, checks: approved.plan.checks.filter((check) => check.code !== "on_time") },
};

const unknownState = {
  ...structuredClone(approved),
  case_id: "c".repeat(32),
  execution: {
    ...approved.execution,
    readback: {
      ...approved.execution.readback,
      records: approved.execution.readback.records.map((record) => ({ ...record, state: "archived_v2" })),
    },
  },
};

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise((resolve) => setTimeout(resolve, 600));

const assertions = [];
const check = (ok, label, detail = "") => {
  assertions.push({ ok, label });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
};

const browser = await chromium.launch({ channel: "chrome", headless: true });

/** approvalReady flips when the page re-reads capabilities. */
let approvalReady = false;
let capabilityReads = 0;

async function open(caseFile, locale = "zh-CN", view = "case") {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const reply = (json, status = 200) => route.fulfill({ status, json });
    if (path === "/api/auth/me") return reply({ scope_id: "owner-a", display_name: "Operator", deployment: "local" });
    if (path === "/api/config") return reply({ deployment: "local", runner_enabled: false });
    if (path === "/api/capabilities") {
      capabilityReads += 1;
      return reply({
        case_read: true, case_qa: true, case_create: false, case_prepare: false, case_approve: approvalReady, product_approval: approvalReady,
        environment: { execution: approvalReady ? "local_executor" : "unavailable", scenario: null, ready: approvalReady },
      });
    }
    if (path === "/api/product/cases") return reply([caseFile]);
    if (path.startsWith("/api/product/cases/")) return reply(caseFile);
    if (path === "/api/runs") return reply([]);
    return reply({});
  });
  const hash = view === "case" ? `#case=${caseFile.case_id}` : view === "runs" ? "#runs" : "";
  await page.goto(`http://127.0.0.1:${port}/app/${hash}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  return { context, page };
}

try {
  // ---- E05: known states become words, unknown tokens are marked ----------
  for (const [locale, expected] of [["zh-CN", "已核对"], ["en-US", "checked"]]) {
    const { context, page } = await open(approved, locale);
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    await page.waitForTimeout(600);
    const states = await page.locator("[data-readback-state]").allTextContents();
    check(states.length > 0 && states.every((text) => text.trim() === expected), `E05 [${locale}]: known readback states use plain words`, states.join(", "));
    check(!states.includes("reflected"), `E05 [${locale}]: the server token is not shown verbatim`, states[0]);
    await context.close();
  }
  {
    const { context, page } = await open(unknownState, "zh-CN");
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    await page.waitForTimeout(600);
    const marked = await page.locator('[data-readback-untranslated="true"]').count();
    const text = (await page.locator('[data-readback-untranslated="true"]').first().textContent() ?? "").trim();
    check(marked > 0, "E05: an unknown readback state is marked");
    check(text === "archived_v2", "E05: the unknown state is shown as the server token", text);
    await context.close();
  }

  // ---- E06: the rule count appears once -----------------------------------
  {
    const { context, page } = await open(approved, "zh-CN");
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    await page.waitForTimeout(700);
    const counts = await page.evaluate(() => ({
      section: (document.querySelector("[data-case-verification]")?.innerText.match(/51\s*\/\s*51/g) ?? []).length,
      occurrences: (document.body.innerText.match(/51\s*\/\s*51/g) ?? []).length,
      headline: document.querySelector("[data-verification-verifier]")?.textContent?.trim() ?? "",
      ruleCounts: document.querySelector("[data-rule-counts]")?.textContent?.trim() ?? "",
    }));
    check(counts.section === 1, "E06: 51/51 is stated once inside the verification section", `${counts.section}×`);
    check(counts.occurrences <= 2, "E06: the page no longer repeats it a third time", `${counts.occurrences}× on the page`);
    check(!/51/.test(counts.headline), "E06: the verification headline carries no counts", counts.headline);
    check(/51/.test(counts.ruleCounts), "E06: the counts live in the recorded-rules block", counts.ruleCounts);
    await context.close();
  }

  // ---- E08: both causes when both apply, one when only one applies --------
  {
    const { context, page } = await open(missingCheck, "zh-CN");
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    await page.waitForTimeout(700);
    const both = await page.evaluate(() => ({
      reason: document.querySelector("[data-sticky-block-reason]")?.textContent?.trim() ?? "",
      also: document.querySelector("[data-sticky-block-also]")?.textContent?.trim() ?? "",
    }));
    check(both.reason.length > 0 && both.also.length > 0, "E08: both causes are stated", `${both.reason} / ${both.also}`);
    check(/即使连上|not enough/i.test(both.also), "E08: the second cause says connecting is not enough", both.also);
    await context.close();
  }
  {
    const { context, page } = await open(approved, "zh-CN");
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    await page.waitForTimeout(600);
    check(await page.locator("[data-sticky-block-also]").count() === 0, "E08: no second cause when only the deployment blocks");
    await context.close();
  }

  // ---- E11: the evidence page explains itself and keeps its filters usable
  {
    const { context, page } = await open(approved, "zh-CN", "runs");
    await page.locator("[data-runs-list]").waitFor({ timeout: 20000 });
    const intro = await page.locator(".view-head p").first().innerText();
    check(/研究运行/.test(intro), "E11: the evidence page states what its records are", intro.slice(0, 40));
    // The filters are five listbox triggers plus the search and two date inputs.
    const filters = page.locator('[data-runs-list] [aria-haspopup="listbox"], [data-runs-list] input');
    const count = await filters.count();
    let usable = 0;
    for (let index = 0; index < count; index += 1) {
      if (await filters.nth(index).isVisible()) usable += 1;
    }
    check(usable === count && count >= 8, "E11: every filter is visible without opening a panel", `${usable}/${count}`);
    await context.close();
  }

  // ---- E09: the start page can re-read the workspace state ---------------
  {
    approvalReady = false;
    const { context, page } = await open(approved, "zh-CN", "home");
    await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
    const before = await page.locator("[data-home-environment]").getAttribute("data-home-environment");
    const readsBefore = capabilityReads;
    approvalReady = true;
    await page.locator("[data-home-env-refresh]").click();
    await page.waitForFunction(() => document.querySelector("[data-home-environment]")?.getAttribute("data-home-environment") === "ready", null, { timeout: 8000 });
    const after = await page.locator("[data-home-environment]").getAttribute("data-home-environment");
    check(before === "readonly" && after === "ready", "E09: re-checking flips the badge without a reload", `${before} → ${after}`);
    check(capabilityReads > readsBefore, "E09: re-checking really re-read the capabilities", `${readsBefore} → ${capabilityReads}`);
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
