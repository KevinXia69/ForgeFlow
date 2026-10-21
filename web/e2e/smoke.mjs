/** Read-only smoke against a configured workbench; product writes belong to live-case.mjs. */
import { chromium } from "playwright-core";
import { gotoView, assertStartPage } from "./lib/nav.mjs";
import { mkdir } from "node:fs/promises";
const BASE = process.env.FORGEFLOW_URL ?? "http://127.0.0.1:8000";
const INVITE = process.env.FORGEFLOW_INVITE ?? "demo-invite-code-0001";
const OUT = process.env.FORGEFLOW_SCREENSHOT_DIR ?? "/tmp/forgeflow-v9-smoke";
await mkdir(OUT, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  for (const lang of ["zh", "en"]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await page.locator("#language-select").click();
    await page.getByRole("option", { name: lang === "zh" ? "简体中文" : "English", exact: true }).click();
    await page.locator("#invite-code").fill(INVITE);
    await page.getByRole("button", { name: lang === "zh" ? "进入工作台" : "Enter workbench", exact: true }).click();
    await assertStartPage(page, (condition, message) => { if (!condition) throw new Error(message); });
    await gotoView(page, "cases");
    if (await page.locator("iframe").count()) throw new Error("business home contains native shell");
    for (const view of ["decision", "risk", "executing", "verified", "all"]) await page.locator(`[data-saved-view="${view}"]`).click();
    await page.screenshot({ path: `${OUT}/${lang}-cases.png`, animations: "disabled" });
    // A clearly labelled fixture is always available without creating an ERP case.
    await page.locator("[data-open-demo]").click();
    await page.locator('[data-case-mode="fixture"]').first().waitFor();
    await page.locator('[data-plan-view="diff"]').click();
    await page.locator('[data-plan-view="table"]').click();
    await page.locator('[data-rail-tab="inbox"]').click();
    await page.locator('[data-case-evidence]').waitFor();
    await page.locator('[data-ask-case]').click();
    await page.locator('[data-case-chat]').waitFor();
    await page.locator('[data-case-chat] .chat-preset').first().click();
    await page.locator('.msg.assistant').first().waitFor({ timeout: 15000 });
    await page.reload();
    await page.locator('[data-case-chat] .msg.assistant').first().waitFor();
    await page.locator('.case-chat-back').click();
    await gotoView(page, "runs");
    if (await page.locator('.run-link').count()) {
      await page.locator('.run-link').first().click();
      await page.locator('[data-evidence="panel"]').waitFor({ timeout: 15000 });
      if (await page.locator('[data-runs-list]').count()) throw new Error('selected run is still below the list');
    }
    await gotoView(page, "home");
    await page.getByRole('button', { name: lang === 'zh' ? '设置' : 'Settings', exact: true }).click();
    const dialog = page.getByRole('dialog'); await dialog.waitFor();
    await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'detached' });
    if (errors.length) throw new Error(errors.join('\n'));
    await context.close();
    console.log(`smoke=${lang}:ok (read-only; fixture Q&A; no ERP execution)`);
  }
} finally { await browser.close(); }
