/**
 * Executable acceptance for v13 T09–T13 (success narrative).
 *
 * T09 one plain conclusion sentence on the verified first screen
 * T10 the execution batch is one list with tabs, not stacked copies
 * T11 step names follow their own status; no bare step numbers
 * T12 no unexplained hash / internal document id / repeated score on screen
 * T13 rehearsal wording from the approval note stays out of the visible receipt
 *
 * Uses the real approved-case payload captured from the T05 loop. No ERP writes.
 */
import { spawn } from "node:child_process";
import { chromium } from "playwright-core";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const port = 4189;
const root = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const approved = JSON.parse(await readFile(new URL("../../web/e2e/fixtures/approved-case.json", import.meta.url), "utf8"));

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise((resolve) => setTimeout(resolve, 600));

const assertions = [];
const check = (ok, label, detail = "") => {
  assertions.push({ ok, label });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
};

/** Text a reader can see: collapsed <details> and aria-hidden nodes do not count. */
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

const browser = await chromium.launch({ channel: "chrome", headless: true });

async function open(casePayload, locale = "zh-CN") {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale });
  const page = await context.newPage();
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") return route.fulfill({ json: { scope_id: "owner-a", display_name: "Alex Chen", deployment: "local" } });
    if (path === "/api/config") return route.fulfill({ json: { deployment: "local", runner_enabled: false, dsh_web_url: "http://example.invalid/" } });
    if (path === "/api/capabilities") return route.fulfill({ json: {
      product_scenarios: [], case_read: true, case_create: false, case_prepare: false, case_approve: false, product_approval: false,
      environment: { execution: "unavailable", scenario: null, ready: false },
    } });
    if (path === "/api/product/cases") return route.fulfill({ json: [casePayload] });
    if (path === `/api/product/cases/${casePayload.case_id}`) return route.fulfill({ json: casePayload });
    if (path === "/api/runs") return route.fulfill({ json: [] });
    return route.fulfill({ json: [] });
  });
  await page.goto(`http://127.0.0.1:${port}/app/#case=${casePayload.case_id}`, { waitUntil: "networkidle" });
  await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
  await page.waitForTimeout(700);
  return { context, page };
}

try {
  // ---- verified case ------------------------------------------------------
  const { context, page } = await open(approved);

  const visible = await page.evaluate(VISIBLE_TEXT);

  // T09
  const conclusions = await page.locator("[data-case-conclusion]").allInnerTexts();
  check(conclusions.length === 1, "T09 conclusion: exactly one conclusion on the screen", `${conclusions.length}`);
  const sentence = (conclusions[0] ?? "").replace(/\s+/g, " ");
  check(/5/.test(sentence), "T09 conclusion: states the order count", sentence);
  check(/50/.test(sentence), "T09 conclusion: states the unit count");
  check(/8/.test(sentence), "T09 conclusion: states the due window");
  check(/51\/51/.test(sentence), "T09 conclusion: states the verifier result");

  // T10
  check(await page.locator(".case-readback").count() === 1, "T10 records: exactly one execution list is rendered");
  const tabs = await page.locator("[data-record-view-button]").count();
  check(tabs === 2, "T10 records: readback and writes are tabs", `${tabs}`);
  const readbackRows = await page.locator(".case-readback li").count();
  await page.locator('[data-record-view-button="writes"]').click();
  const writeRows = await page.locator(".case-readback li").count();
  check(await page.locator(".case-readback").count() === 1, "T10 records: switching the tab does not stack a second list");
  check(readbackRows === writeRows, "T10 records: both views cover the same batch", `readback=${readbackRows} writes=${writeRows}`);
  await page.locator('[data-record-view-button="readback"]').click();

  // T11
  const labels = await page.locator("[data-case-stepper] .status-step-label").allInnerTexts();
  check(!labels.includes("待你决定"), "T11 stepper: a finished case does not say it waits for a decision", labels.join("|"));
  check(labels.includes("已批准") && labels.includes("已执行"), "T11 stepper: finished steps are named after what happened", labels.join("|"));
  const digits = (await page.locator("[data-case-stepper] .status-step-dot").allInnerTexts()).filter((value) => /^[0-9]+$/.test(value.trim()));
  check(digits.length === 0, "T11 stepper: no bare step numbers", digits.join("|"));

  // T12
  const hashes = visible.match(/\b[0-9a-f]{8,}\b/g) ?? [];
  check(hashes.length === 0, "T12 first screen: no unexplained hex identifier", hashes.slice(0, 3).join("|"));
  const scores = visible.match(/100\.0+\/100/g) ?? [];
  check(scores.length === 1, "T12 first screen: the score is stated once", `${scores.length}`);
  const internal = visible.match(/original_verifier|whm\/MO\/|Operator [0-9a-f]{8}/g) ?? [];
  check(internal.length === 0, "T12 first screen: no internal identifier or snake_case kind", internal.slice(0, 3).join("|"));
  check(await page.locator("details[data-receipt-technical]:not([open])").count() === 1, "T12 technical detail: collapsed by default");
  await page.locator("details[data-receipt-technical] > summary").first().click();
  const planIdShown = (await page.locator("[data-receipt-plan]").first().innerText()).trim();
  check(planIdShown.length > 0, "T12 technical detail: the plan id is still available when expanded", planIdShown);
  await page.locator("details[data-receipt-technical] > summary").first().click();

  // T13
  const rehearsal = visible.match(/dedicated-sandbox|Authorized |sandbox revision/gi) ?? [];
  check(rehearsal.length === 0, "T13 receipt: rehearsal wording is not visible on the business receipt", rehearsal.join("|"));
  const rawNote = (approved.approval?.comment ?? "").trim();
  if (rawNote) {
    const inDom = await page.getByText(rawNote, { exact: true }).count();
    check(inDom >= 1, "T13 receipt: the approval note is retained, not deleted");
    const visibleNote = (visible.match(new RegExp(rawNote.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")) ?? []).length;
    check(visibleNote === 0, "T13 receipt: the note is behind the technical detail", `${visibleNote}`);
  }
  await context.close();

  // ---- awaiting case: the decision step must still say it waits -----------
  const awaiting = structuredClone(approved);
  awaiting.state = "awaiting_approval";
  awaiting.execution = null;
  awaiting.approval = null;
  awaiting.result = { ...awaiting.result, execution_status: null, verified: false, strict_success: null, evaluation_valid: null, outcome: "awaiting_approval" };
  awaiting.decision = { ...awaiting.decision, can_approve: false, reason: "product_execution_unavailable" };
  const second = await open(awaiting);
  const awaitingLabels = await second.page.locator("[data-case-stepper] .status-step-label").allInnerTexts();
  check(awaitingLabels.includes("待你决定"), "T11 stepper: an undecided case still says it waits for a decision", awaitingLabels.join("|"));
  check(await second.page.locator("[data-case-conclusion]").count() === 0, "T09 conclusion: not claimed for a case that is not verified");
  await second.context.close();
} catch (error) {
  check(false, "harness completed", error instanceof Error ? error.message : String(error));
} finally {
  await browser.close();
  server.kill();
  const failed = assertions.filter((item) => !item.ok);
  console.log(`\n${assertions.length - failed.length}/${assertions.length} assertions passed`);
  process.exitCode = failed.length ? 1 : 0;
}
