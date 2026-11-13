/**
 * W0–W9 UI verification: log in, walk the four pages, capture what is on screen.
 *
 * Headed by default so a person can watch; screenshots go to OUT.  The script
 * prints the visible chrome text so wording changes can be reviewed without
 * reading pixels.
 */
import { chromium } from "playwright-core";
import { mkdir, writeFile } from "node:fs/promises";

const BASE = process.env.FORGEFLOW_URL ?? "http://127.0.0.1:8000";
const INVITE = process.env.FORGEFLOW_INVITE ?? "";
const OUT = process.env.FORGEFLOW_SCREENSHOT_DIR ?? "/tmp/forgeflow-verify";
const HEADLESS = process.env.FORGEFLOW_HEADLESS === "1";

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: HEADLESS });
const report = { base: BASE, pages: {}, errors: [] };
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await context.newPage();
  page.on("pageerror", error => report.errors.push(String(error)));
  page.on("console", message => {
    if (message.type() === "error") report.errors.push(`console: ${message.text()}`);
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.locator("#invite-code").waitFor({ timeout: 15000 });
  await page.locator("#invite-code").fill(INVITE);
  const submit = page.locator("form button[type=submit]");
  if (await submit.count()) await submit.first().click();
  else await page.locator("#invite-code").press("Enter");
  await page.locator("[data-nav]").first().waitFor({ timeout: 20000 });

  const views = [
    ["home", ""],
    ["setup", "#setup"],
    ["cases", "#cases"],
    ["runs", "#runs"],
  ];
  for (const [name, hash] of views) {
    await page.goto(`${BASE}/app/${hash}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1200);
    const shot = `${OUT}/${name}.png`;
    await page.screenshot({ path: shot, animations: "disabled", fullPage: false });
    const text = await page.locator("body").innerText();
    report.pages[name] = {
      shot,
      url: page.url(),
      title: await page.title(),
      headings: await page.locator("h1, h2, h3").allInnerTexts(),
      nav: await page.locator("[data-nav]").allInnerTexts(),
      text: text.split("\n").map(line => line.trim()).filter(Boolean).slice(0, 80),
    };
  }

  // The task panel is the only surface that shows the seven user states.
  await page.goto(`${BASE}/app/#cases`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  if (await page.locator("[data-odoo20-plan]").count()) {
    report.taskPanel = await page.locator("[data-odoo20-plan]").innerText();
    await page.locator("[data-odoo20-plan]").screenshot({
      path: `${OUT}/task-panel.png`, animations: "disabled" });
  }
  await writeFile(`${OUT}/report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser.close();
}
