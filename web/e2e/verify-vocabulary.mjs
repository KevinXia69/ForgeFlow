/**
 * Executable acceptance for v13 T25–T27 (vocabulary and shape).
 *
 * T25  no cross-reference to something that does not say that
 * T26  when this page read the case is page metadata, not a case fact
 * T27  no research vocabulary on the start page or a case screen; the evidence
 *      surface says in plain words what its records are
 *
 * Private bundle against a mock API. No ERP writes.
 */
import { spawn } from "node:child_process";
import { chromium } from "playwright-core";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const port = 4193;
const root = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const approved = JSON.parse(await readFile(new URL("../../web/e2e/fixtures/approved-case.json", import.meta.url), "utf8"));
const awaiting = { ...structuredClone(approved), state: "awaiting_approval", execution: null, approval: null,
  result: { ...approved.result, execution_status: null, verified: false, strict_success: null, evaluation_valid: null, outcome: "awaiting_approval" },
  decision: { ...approved.decision, can_approve: false, reason: "product_execution_unavailable" } };

const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise((resolve) => setTimeout(resolve, 600));

const assertions = [];
const check = (ok, label, detail = "") => {
  assertions.push({ ok, label });
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
};

const VISIBLE = `(() => {
  const parts = [];
  const walk = (node) => {
    if (node.nodeType === 3) {
      const parent = node.parentElement;
      if (!parent || parent.closest('details:not([open])') || parent.closest('[hidden], [aria-hidden="true"]')) return;
      parts.push(node.nodeValue ?? ''); return;
    }
    if (node.nodeType !== 1 || node.tagName === 'SCRIPT' || node.tagName === 'STYLE') return;
    for (const child of node.childNodes) walk(child);
  };
  walk(document.body);
  return parts.join(' ');
})()`;

const RESEARCH_TERMS = /solver|求解器|verifier|校验器|constraint headroom|约束余量|run id|evaluation validity|评测有效性|experiment category|实验类别|fact snapshot|事实快照/i;

const browser = await chromium.launch({ channel: "chrome", headless: true });

async function open(caseFile, hash, locale) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale });
  const page = await context.newPage();
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const reply = (json, status = 200) => route.fulfill({ status, json });
    if (path === "/api/auth/me") return reply({ scope_id: "owner-a", display_name: "Reviewer", deployment: "local" });
    if (path === "/api/config") return reply({ deployment: "local", runner_enabled: false });
    if (path === "/api/capabilities") return reply({
      case_read: true, case_qa: true, case_create: false, case_prepare: false, case_approve: false, product_approval: false,
      environment: { execution: "unavailable", scenario: null, ready: false },
    });
    if (path === "/api/product/cases") return reply([caseFile]);
    if (path.startsWith("/api/product/cases/")) return reply(caseFile);
    if (path === "/api/runs") return reply([]);
    return reply({});
  });
  await page.goto(`http://127.0.0.1:${port}/app/${hash}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(700);
  return { context, page };
}

try {
  // ---- T25 / T26 / T27 on a case screen, in both languages ---------------
  for (const [locale, pageFact] of [["zh-CN", /本页/], ["en-US", /This page/]]) {
    const { context, page } = await open(awaiting, `#case=${awaiting.case_id}`, locale);
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });

    const badge = await page.locator(".case-data-type").first().innerText();
    check(!/见环境状态|shown above/.test(badge), `T25 badge [${locale}]: no reference to a banner that does not say that`, badge);

    const readNote = page.locator("[data-case-last-confirmed]");
    check(await readNote.count() === 1, `T26 read time [${locale}]: it is still recorded`);
    check(await readNote.isVisible() === false, `T26 read time [${locale}]: it is not on the first screen`);
    const readText = (await readNote.textContent() ?? "").trim();
    check(pageFact.test(readText), `T26 read time [${locale}]: it is worded as a page fact`, readText);
    await page.locator("[data-case-meta-details] > summary").click();
    await page.waitForTimeout(200);
    check(await readNote.isVisible(), `T26 read time [${locale}]: available once the details are opened`);

    const visible = await page.evaluate(VISIBLE);
    const terms = visible.match(RESEARCH_TERMS) ?? [];
    check(terms.length === 0, `T27 case screen [${locale}]: no research vocabulary`, [...new Set(terms)].join("|"));
    await context.close();
  }

  // ---- T27 on the start page ----------------------------------------------
  {
    const { context, page } = await open(awaiting, "", "zh-CN");
    await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
    const visible = await page.evaluate(VISIBLE);
    const terms = visible.match(RESEARCH_TERMS) ?? [];
    check(terms.length === 0, "T27 start page: no research vocabulary", [...new Set(terms)].join("|"));
    await context.close();
  }

  // ---- T27 on the success screen ------------------------------------------
  {
    const { context, page } = await open(approved, `#case=${approved.case_id}`, "en-US");
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    const visible = await page.evaluate(VISIBLE);
    const terms = visible.match(RESEARCH_TERMS) ?? [];
    check(terms.length === 0, "T27 success screen: no research vocabulary", [...new Set(terms)].join("|"));
    await context.close();
  }

  // ---- T27: the evidence surface explains itself --------------------------
  {
    const { context, page } = await open(awaiting, "#runs", "zh-CN");
    await page.locator("[data-runs-list]").waitFor({ timeout: 20000 });
    const intro = await page.locator(".view-head p").first().innerText();
    check(/research run|研究运行/.test(intro), "T27 evidence: it says the records are research runs", intro.slice(0, 60));
    check(/evaluation program|评测程序/.test(intro), "T27 evidence: it says where the scores come from");
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
