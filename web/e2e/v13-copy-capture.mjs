/**
 * Capture the surfaces affected by v13 T14–T24 (copy and context).
 *
 * login · case list · stale awaiting case (untranslated checks, policy hash)
 * · labelled demo case (stacked disclaimers). Read-only; no ERP writes.
 */
import { chromium } from "playwright-core";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const tag = process.env.CAPTURE_TAG || "before";
const base = process.env.FORGEFLOW_BASE || "http://127.0.0.1:8123";
const invite = process.env.FORGEFLOW_INVITE || "firstlook-local-invite-code";
const out = process.env.FORGEFLOW_OUT
  ? `${process.env.FORGEFLOW_OUT.replace(/\/$/, "")}/`
  : "/tmp/forgeflow-shots/";
await mkdir(`${out}screens/`, { recursive: true });

const browser = await chromium.launch({ channel: "chrome", headless: true });
const log = [];

for (const [lang, locale] of [["zh", "zh-CN"], ["en", "en-US"]]) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto(base, { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${out}screens/${lang}-${tag}-login.png`, fullPage: false });
  const login = await page.evaluate(() => ({
    title: document.querySelector(".login-card h1")?.textContent?.trim(),
    copy: document.querySelector(".login-card p")?.textContent?.trim(),
    hasLogo: !!document.querySelector(".login-card img, .login-card svg, .login-card .brand"),
    inviteLabel: document.querySelector('label[for="invite-code"]')?.textContent?.trim(),
    hint: [...document.querySelectorAll(".login-card .muted, .login-card small")].map((el) => el.textContent?.trim()).filter(Boolean),
  }));

  await page.locator("#invite-code").fill(invite);
  await page.locator('button[type="submit"]').first().click();
  await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
  await page.goto(`${base}/#cases`, { waitUntil: "networkidle" });
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${out}screens/${lang}-${tag}-cases.png`, fullPage: true });

  const list = await page.evaluate(() => {
    const rows = [...document.querySelectorAll("[data-case-row]")].map((row) => {
      const severity = row.querySelector(".case-severity");
      return {
        title: row.querySelector(".case-card-title")?.textContent?.trim(),
        meta: row.querySelector(".case-card-meta")?.textContent?.trim(),
        when: row.querySelector("[data-case-when]")?.textContent?.trim() ?? null,
        severity: severity?.textContent?.trim(),
        severitySize: severity ? getComputedStyle(severity).fontSize : null,
        severityColor: severity ? getComputedStyle(severity).color : null,
        severityWeight: severity ? getComputedStyle(severity).fontWeight : null,
        state: row.querySelector(".case-state-pill")?.textContent?.trim(),
      };
    });
    const counters = [...document.querySelectorAll("[data-saved-view]")].map((el) => el.textContent?.trim());
    return {
      rows,
      distinctTitles: new Set(rows.map((row) => `${row.title}|${row.meta}`)).size,
      distinctRows: new Set(rows.map((row) => `${row.title}|${row.meta}|${row.when}`)).size,
      decisionMentions: (document.body.innerText.match(/待你决定|Needs your decision/g) || []).length,
      counters,
      disclaimer: document.querySelector(".cases-disclaimer")?.textContent?.trim(),
      navigate: [...document.querySelectorAll(".nav-item")].map((el) => el.textContent?.trim()),
      topbarStatus: document.querySelector(".status-pill")?.textContent?.trim(),
      sidebarFooter: document.querySelector(".side-who strong")?.textContent?.trim(),
    };
  });

  // A case that is otherwise unrecorded in its checks (untranslated labels, hash value).
  const awaiting = page.locator('[data-case-row="true"][data-case-state="awaiting_approval"]').first();
  const checksSource = await awaiting.count() ? "recorded-awaiting-case" : "labelled-demo-no-recorded-case";
  if (checksSource === "recorded-awaiting-case") await awaiting.click();
  else await page.locator('[data-open-demo]').click();
  await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${out}screens/${lang}-${tag}-case-checks.png`, fullPage: true });
  const checks = await page.evaluate(() => ({
    rows: [...document.querySelectorAll("[data-policy-checks] .policy-check")].map((el) => ({
      status: el.getAttribute("data-check-status"),
      statusPill: el.querySelector(".policy-check-status")?.textContent?.trim(),
      pillFontSize: el.querySelector(".policy-check-status") ? getComputedStyle(el.querySelector(".policy-check-status")).fontSize : null,
      label: el.querySelector(".policy-check-label")?.textContent?.trim(),
      value: el.querySelector(".policy-check-value")?.textContent?.trim(),
      source: el.querySelector(".policy-check-source")?.textContent?.trim(),
    })),
    counts: document.querySelector("[data-check-counts]")?.textContent?.trim(),
    hashes: (document.body.innerText.match(/\b[0-9a-f]{8,}\b/g) || []).slice(0, 6),
    approver: document.querySelector("[data-receipt-approver]")?.textContent?.trim(),
    latinWords: (document.body.innerText.match(/\b[A-Z][a-z]+ [a-z]+[a-z ]*/g) || []).slice(0, 8),
    decisionMentions: (document.body.innerText.match(/待你决定|Needs your decision/g) || []).length,
  }));

  // The labelled demo case, which previously stacked three disclaimers.
  await page.goto(`${base}/#case=274c4b9089cc74cc1c01401732d91202`, { waitUntil: "networkidle" });
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${out}screens/${lang}-${tag}-demo.png`, fullPage: true });
  const demo = await page.evaluate(() => ({
    banners: [...document.querySelectorAll("[data-demo-banner], .cases-demo-banner, .environment-banner")].map((el) => el.textContent?.trim()),
    visibleDisclaimerCount: (document.body.innerText.match(/不写 ERP|no ERP writes/gi) || []).length,
    plannedPill: (() => {
      const pill = [...document.querySelectorAll(".policy-check")].find((el) => el.getAttribute("data-check-status") === "planned");
      if (!pill) return null;
      const status = pill.querySelector(".policy-check-status");
      const label = pill.querySelector(".policy-check-label");
      return status && label ? { status: status.textContent?.trim(), label: label.textContent?.trim(), statusBg: getComputedStyle(status).backgroundColor, gap: Math.round(label.getBoundingClientRect().left - status.getBoundingClientRect().right) } : null;
    })(),
  }));

  log.push(`\n===== ${lang} (${tag}) =====\n${JSON.stringify({ login, list, checksSource, checks, demo }, null, 1)}`);
  if (errors.length) log.push(`pageerrors: ${errors.join(" | ")}`);
  await context.close();
}

await writeFile(`${out}${tag}-facts.txt`, log.join("\n"), "utf8");
await browser.close();
console.log(log.join("\n"));
