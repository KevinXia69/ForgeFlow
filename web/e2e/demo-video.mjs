import { chromium } from "playwright-core";
import { gotoView, assertStartPage } from "./lib/nav.mjs";
import fs from "node:fs";

const BASE = process.env.FORGEFLOW_URL || "http://127.0.0.1:8000";
const INVITE = process.env.FORGEFLOW_INVITE || "demo-invite-code-0001";
const outDir = "../.runtime/video";
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  recordVideo: { dir: outDir, size: { width: 1440, height: 1000 } },
});
const page = await context.newPage();
try {
  await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  console.log("login");
  await page.locator("#language-select").click();
  await page.getByRole("option", { name: "简体中文" }).click();
  await page.locator("#invite-code").fill(INVITE);
  await page.getByRole("button", { name: "进入工作台", exact: true }).click();
  await page.locator(".sidebar").waitFor({ timeout: 15000 });
  await gotoView(page, "cases");
  await page.waitForTimeout(1000);

  console.log("case");
  await page.screenshot({ path: "../.runtime/react-cases.png", fullPage: true });
  await page.locator("[data-case-row]").first().click();
  await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
  await page.waitForTimeout(2200);
  await page.screenshot({ path: "../.runtime/react-case-detail.png", fullPage: true });

  console.log("case chat");
  await page.locator("[data-ask-case]").click();
  await page.locator("[data-case-chat]").waitFor({ timeout: 5000 });
  await page.locator(".chat-preset").first().click();
  await page.waitForTimeout(2500);
  await page.screenshot({ path: "../.runtime/react-case-chat.png" });
  await page.getByRole("button", { name: "关闭案例问答", exact: true }).click();

  console.log("knowledge");
  await gotoView(page, "home");
  const composer = page.locator(".composer-input").first();
  await composer.fill("只读 key 为什么能阻止直接写入？");
  await composer.press("Enter");
  await page.waitForTimeout(9000);

  console.log("evidence");
  await gotoView(page, "runs");
  await page.locator("table tbody tr").first().waitFor({ timeout: 10000 });
  await page.locator("table tbody tr").first().click();
  await page.waitForTimeout(1200);
  const evidence = page.getByRole("region", { name: /证据|Evidence/ }).first();
  if (await evidence.count()) await evidence.scrollIntoViewIfNeeded();
  await page.waitForTimeout(2500);

  console.log("settings");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page.waitForTimeout(1200);
  await page.locator("#settings-language").click();
  await page.getByRole("option", { name: "English" }).click();
  await page.waitForTimeout(1200);
  await page.locator("#settings-language").click();
  await page.getByRole("option", { name: "简体中文" }).click();
  await page.waitForTimeout(800);
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await page.waitForTimeout(1500);
} finally {
  await context.close();
  await browser.close();
}
const files = fs.readdirSync(outDir).filter((name) => name.endsWith(".webm"));
console.log(JSON.stringify({ videos: files }));
