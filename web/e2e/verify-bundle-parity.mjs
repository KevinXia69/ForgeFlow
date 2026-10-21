/**
 * Cross-bundle parity: the same case must tell the same story in the private
 * workbench and in the public review bundle.
 *
 * It serves both built bundles in turn, replays the same mocked API, and
 * extracts a language-independent fact set from each: which structural markers
 * are present, how many execution lists are rendered, how many hex identifiers
 * and scores reach the reader, whether the step names follow the case state,
 * whether the rows can be told apart. The two fact sets must be identical.
 *
 * Text is deliberately not compared: one bundle is English-only and the other
 * is bilingual. What must not differ is the structure and the counts.
 *
 * Read-only; no ERP writes. Exits non-zero on the first difference.
 */
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { chromium } from "playwright-core";
import { mkdtemp, readFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const approved = JSON.parse(await readFile(new URL("../../web/e2e/fixtures/approved-case.json", import.meta.url), "utf8"));
const awaiting = {
  ...structuredClone(approved),
  case_id: "b".repeat(32),
  state: "awaiting_approval",
  execution: null,
  approval: null,
  result: { ...approved.result, execution_status: null, verified: false, strict_success: null, evaluation_valid: null, outcome: "awaiting_approval" },
  decision: { ...approved.decision, can_approve: false, reason: "required_checks_unknown", unknown_checks: ["on_time"] },
  plan: {
    ...approved.plan,
    checks: [
      { code: "margin", label: "Margin floor", status: "pass", actual: "80%", threshold: "27.5%", source: "local request margin" },
      { code: "policy", label: "Original contract binding", status: "pass", actual: "b700c9f1534e…", threshold: "required", source: "local plan binding" },
    ],
  },
};

/** Duplicate-looking rows: same exception, product, units and due date. */
function listRows() {
  const row = (suffix, state, minutes) => {
    const base = structuredClone(approved);
    const verified = state === "succeeded";
    return {
      ...base,
      case_id: suffix.repeat(32).slice(0, 32),
      state,
      execution: state === "awaiting_approval" ? null : base.execution,
      result: { ...base.result, verified, strict_success: verified, evaluation_valid: verified, outcome: verified ? "verified" : "awaiting_approval" },
      updated_at: `2026-10-22T04:${String(minutes).padStart(2, "0")}:00Z`,
      created_at: `2026-10-22T04:${String(minutes).padStart(2, "0")}:00Z`,
    };
  };
  return [row("a", "succeeded", 2), row("b", "failed", 34), row("c", "awaiting_approval", 51), row("d", "succeeded", 59)];
}

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

const browser = await chromium.launch({ channel: "chrome", headless: true });
const results = {};

/** An ephemeral free port, so a leftover server can never be mistaken for ours. */
function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

async function examine({ label, directory, port, locale }) {
  const step = (name) => console.log(`  [${label}] ${name}`);
  const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", directory], { stdio: "ignore" });
  await new Promise((resolve) => setTimeout(resolve, 600));
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale });
  const page = await context.newPage();
  page.on("pageerror", (error) => console.log(`  [${label}] pageerror: ${error.message}`));
  page.on("console", (message) => { if (message.type() === "error") console.log(`  [${label}] console.error: ${message.text().slice(0, 120)}`); });
  const facts = {};
  page.setDefaultTimeout(15000);

  const route = (cases) => page.route("**/api/**", (r) => {
    const path = new URL(r.request().url()).pathname;
    const reply = (json, status = 200) => r.fulfill({ status, json });
    if (path === "/api/auth/me") return reply({ scope_id: "owner-a", display_name: "Operator 5cfc86f2", deployment: "local" });
    if (path === "/api/config") return reply({ deployment: "local", runner_enabled: false });
    if (path === "/api/capabilities") return reply({
      product_scenarios: [], case_read: true, case_qa: true, case_create: false, case_prepare: false, case_approve: false, product_approval: false,
      environment: { execution: "unavailable", scenario: null, ready: false },
    });
    if (path === "/api/product/cases") return reply(cases);
    if (path.startsWith("/api/product/cases/")) return reply(cases[0]);
    if (path === "/api/runs") return reply([]);
    return reply({});
  });

  // --- verified case -------------------- --------------------------------------------------------
  await route([approved]);
  await page.goto(`http://127.0.0.1:${port}/app/#case=${approved.case_id}`, { waitUntil: "domcontentloaded" });
  await page.locator("[data-case-detail]").waitFor({ timeout: 20000 }).catch(async (error) => {
    console.log(`  [${label}] detail did not render; body: ${(await page.locator("body").innerText()).slice(0, 200).replace(/\n/g, " | ")}`);
    throw error;
  });
  await page.waitForTimeout(700);
  facts.verified = await page.evaluate(VISIBLE_TEXT);
  facts.verifiedMarkers = await page.evaluate(() => ({
    conclusion: document.querySelectorAll("[data-case-conclusion]").length,
    executionLists: document.querySelectorAll(".case-readback").length,
    recordTabs: document.querySelectorAll("[data-record-view-button]").length,
    technicalCollapsed: document.querySelectorAll("details[data-receipt-technical]:not([open])").length,
    scores: ((document.body.innerText.match(/100\.0+\/100/g) ?? []).length),
    stepNames: [...document.querySelectorAll("[data-case-stepper] .status-step-label")].map((el) => (el.textContent ?? "").trim().length > 0),
    stepDigits: [...document.querySelectorAll("[data-case-stepper] .status-step-dot")].filter((el) => /^[0-9]+$/.test((el.textContent ?? "").trim())).length,
  }));
  await page.unroute("**/api/**");

  // --- awaiting case -------------------- with a missing required check ---------------------------
  await route([awaiting]);
  await page.goto(`http://127.0.0.1:${port}/app/#case=${awaiting.case_id}`, { waitUntil: "networkidle" });
  await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
  await page.waitForTimeout(700);
  facts.awaiting = await page.evaluate(VISIBLE_TEXT);
  facts.awaitingMarkers = await page.evaluate(() => {
    const sticky = document.querySelector("[data-case-approve-sticky]");
    const box = sticky?.getBoundingClientRect();
    return {
      missingRows: document.querySelectorAll('[data-check-missing="true"]').length,
      stickyPresent: !!sticky,
      stickyDisabled: sticky ? sticky.disabled : null,
      stickyInView: box ? box.top >= 0 && box.bottom <= innerHeight : null,
      blockReason: document.querySelectorAll("[data-sticky-block-reason]").length,
      blockRecovery: document.querySelectorAll("[data-sticky-block-recovery]").length,
      untranslatedLabels: [...document.querySelectorAll("[data-policy-checks] .policy-check-label")]
        .filter((el) => /^[A-Za-z]/.test((el.textContent ?? "").trim())).length,
    };
  });
  await page.unroute("**/api/**");

  // --- case list -------------------- ------------------------------------------------------------
  const rows = listRows();
  await route(rows);
  await page.goto(`http://127.0.0.1:${port}/app/#cases`, { waitUntil: "networkidle" });
  await page.locator("[data-case-row]").first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(700);
  facts.list = await page.evaluate(() => {
    const when = [...document.querySelectorAll("[data-case-when]")].map((el) => (el.textContent ?? "").trim());
    const counters = [...document.querySelectorAll("[data-saved-view]")].map((el) => Number((el.textContent ?? "").match(/[0-9]+/g)?.pop() ?? -1));
    return {
      rows: document.querySelectorAll("[data-case-row]").length,
      distinctWhen: new Set(when).size,
      counters,
      counterSum: counters.slice(0, 4).reduce((a, b) => a + b, 0),
      counterTotal: counters[4],
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      severityWeight: (() => {
        const el = document.querySelector(".case-severity");
        return el ? Number(getComputedStyle(el).fontWeight) : null;
      })(),
      demoEntries: document.querySelectorAll("[data-open-demo]").length,
    };
  });
  await page.unroute("**/api/**");

  // --- start page -------------------- -----------------------------------------------------------
  await route([approved]);
  await page.goto(`http://127.0.0.1:${port}/app/`, { waitUntil: "networkidle" });
  await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
  await page.waitForTimeout(500);
  facts.start = await page.evaluate(() => ({
    cards: document.querySelectorAll("[data-home-card]").length,
    environment: document.querySelector("[data-home-environment]")?.getAttribute("data-home-environment") ?? null,
    presets: document.querySelectorAll("[data-chat-presets] .chat-preset").length,
    cardsNavigable: [...document.querySelectorAll("[data-home-card]")].every((el) => el.tagName === "BUTTON"),
  }));

  await context.close();
  server.kill();

  results[label] = facts;
}

async function examineText(label, directory, port, locale) {
  // Visible-text based facts need a separate pass with the listener attached.
  return { label, directory, port, locale };
}

const PRIVATE_DIR = fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const PUBLIC_DIR = fileURLToPath(new URL("../../web-public/dist/", import.meta.url));

const VISIBLE_TEXT = VISIBLE; // the page-side helper is injected as a string

let failed = 0;
let mount = null;
try {
  await examine({ label: "private", directory: PRIVATE_DIR, port: await freePort(), locale: "en-US" });
  // The public bundle is built to dist/ root; it is mounted at /app/ like the
  // public smoke test does.
  mount = await mkdtemp(join(tmpdir(), "forgeflow-parity-"));
  await symlink(PUBLIC_DIR, join(mount, "app"), "dir");
  await examine({ label: "public", directory: mount, port: await freePort(), locale: "en-US" });
} catch (error) {
  console.log(`FAIL  harness — ${error instanceof Error ? error.message : String(error)}`);
  failed += 1;
}

function compare(path, a, b) {
  const left = JSON.stringify(a);
  const right = JSON.stringify(b);
  if (left === right) {
    console.log(`PASS  ${path}: ${left}`);
    return;
  }
  console.log(`FAIL  ${path}: private=${left} public=${right}`);
  failed += 1;
}

/** The exact terms v13 T27 flagged. "verification" is plain language now. */
const RESEARCH = /\bsolver\b|\bverifier\b|constraint headroom|\brun id\b|evaluation validity|experiment category|\bsnapshot\b/gi;
function researchTerms(text) {
  return (text.match(RESEARCH) ?? []).length;
}

if (!failed) {
  compare("verified markers", results.private.verifiedMarkers, results.public.verifiedMarkers);
  compare("awaiting markers", results.private.awaitingMarkers, results.public.awaitingMarkers);
  compare("list facts", results.private.list, results.public.list);
  compare("start page", results.private.start, results.public.start);

  // Language-independent content rules, applied to both bundles.
  for (const side of ["private", "public"]) {
    const facts = results[side];
    const hex = (facts.verified.match(/\b[0-9a-f]{8,}\b/g) ?? []).length;
    console.log(`${hex === 0 ? "PASS" : "FAIL"}  ${side} verified screen: no hex identifier on screen — ${hex}`);
    if (hex !== 0) failed += 1;
    const terms = researchTerms(facts.verified) + researchTerms(facts.awaiting);
    if (terms) {
      const merged = `${facts.verified} ${facts.awaiting}`;
      for (const hit of merged.matchAll(new RegExp(RESEARCH.source, "gi"))) {
        console.log(`       ${side} …${merged.slice(Math.max(0, hit.index - 45), hit.index + 25).replace(/\s+/g, " ")}…`);
      }
    }
    console.log(`${terms === 0 ? "PASS" : "FAIL"}  ${side} case screens: no research vocabulary — ${terms}`);
    if (terms !== 0) failed += 1;
  }
}

await browser.close();
if (mount) await rm(mount, { recursive: true, force: true });
console.log(failed ? `\nbundle parity: ${failed} difference(s)` : "\nbundle parity: both bundles tell the same story");
process.exitCode = failed ? 1 : 0;
