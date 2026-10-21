import { chromium } from "playwright-core";
import { gotoView, assertStartPage } from "./lib/nav.mjs";
import { mkdir } from "node:fs/promises";
if (process.env.FORGEFLOW_ALLOW_LIVE_TEST !== "1") throw new Error("This test writes the designated Odoo sandbox; set FORGEFLOW_ALLOW_LIVE_TEST=1 only for the authorized product regression.");
const OUT = process.env.FORGEFLOW_SCREENSHOT_DIR ?? "/tmp/forgeflow-v9-live";
await mkdir(OUT, { recursive: true });

const BASE = process.env.FORGEFLOW_URL ?? "http://127.0.0.1:8000";
const INVITE = process.env.FORGEFLOW_INVITE ?? "demo-invite-code-0001";

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  locale: "zh-CN",
});
const page = await context.newPage();
try {
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  await page.locator("#language-select").click();
  await page.getByRole("option", { name: "简体中文", exact: true }).click();
  await page.locator("#invite-code").fill(INVITE);
  await page.getByRole("button", { name: "进入工作台", exact: true }).click();
  await page.locator(".sidebar").waitFor({ timeout: 15000 });

  // The start page is the ask surface; the case queue has its own route.
  await gotoView(page, "cases");
  const casesReady = page.locator('[data-cases-view][data-cases-ready="true"]');
  await casesReady.waitFor({ timeout: 20000 });
  const mode = await page
    .locator("[data-cases-view]")
    .getAttribute("data-cases-mode");
  if (mode !== "live") {
    throw new Error(`live case requires live mode, got ${mode}`);
  }
  const caps = await (await page.request.get(BASE + "/api/capabilities")).json();
  if (caps.environment?.execution !== "live_sandbox" || caps.live_execute !== true) throw new Error("real Odoo product regression requires a live sandbox executor");
  await page.locator("[data-create-case]").click();
  await page.locator("[data-case-detail]").waitFor({ timeout: 300000 });
  await page.waitForFunction(
    () => {
      const button = document.querySelector("[data-case-approve]");
      return button && !button.hasAttribute("disabled");
    },
    null,
    { timeout: 300000 },
  );
  console.log(
    "READY",
    await page.locator("[data-case-state]").first().innerText(),
  );
  await page.screenshot({
    path: `${OUT}/before-approval.png`,
    fullPage: true,
  });

  await page.locator("[data-case-approve]").click();
  await page.getByRole("dialog").waitFor();
  await page.getByRole("checkbox").check();
  await page.getByRole("dialog").getByRole("button", { name: "批准并执行", exact: true }).click();
  await page.locator("[data-execution-receipt]").waitFor({ timeout: 300000 });
  if (await page.locator("[data-execution-receipt]").getAttribute("data-receipt-state") !== "success") throw new Error("execution completed without strict verified evidence");
  const receipt = await page.locator("[data-execution-receipt]").innerText();
  console.log("RECEIPT", receipt.slice(0, 1200));
  await page.screenshot({
    path: `${OUT}/after-execution.png`,
    fullPage: true,
  });
} finally {
  await context.close();
  await browser.close();
}
