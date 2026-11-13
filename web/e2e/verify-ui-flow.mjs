/**
 * Drive the real UI through one exception: prepare -> approve -> apply -> verify.
 *
 * Prints a JSON report with each step's visible status and saves screenshots so
 * the run can be reviewed after the fact. Exits non-zero if any step does not
 * reach its expected state.
 */
import { chromium } from "playwright-core";
import { mkdir, writeFile } from "node:fs/promises";

const BASE = process.env.FORGEFLOW_URL ?? "http://127.0.0.1:8000";
const INVITE = process.env.FORGEFLOW_INVITE ?? "";
const OUT = process.env.FORGEFLOW_SCREENSHOT_DIR ?? "/tmp/forgeflow-ui-flow";
const PO = process.env.FORGEFLOW_PO ?? "51";
const REASON = process.env.FORGEFLOW_REASON ?? "supplier withdrew the confirmed commitment";

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: false });
const report = { base: BASE, purchase_order: PO, steps: [], errors: [] };
let failed = false;
const step = (name, ok, extra = {}) => {
  report.steps.push({ step: name, ok, ...extra });
  if (!ok) failed = true;
};
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  const page = await context.newPage();
  page.on("pageerror", error => report.errors.push(String(error)));

  await page.goto(`${BASE}/app/`, { waitUntil: "domcontentloaded" });
  await page.locator("#invite-code").waitFor({ timeout: 15000 });
  await page.locator("#invite-code").fill(INVITE);
  const submit = page.locator("form button[type=submit]");
  if (await submit.count()) await submit.first().click();
  else await page.locator("#invite-code").press("Enter");
  await page.locator("[data-nav]").first().waitFor({ timeout: 20000 });
  const me = await page.evaluate(async () => {
    const response = await fetch("/api/auth/me", { credentials: "same-origin" });
    return response.ok ? response.json() : { status: response.status };
  });
  step("signed_in", Boolean(me && me.scope_id), { scope: me?.scope_id?.slice(0, 8) });

  await page.goto(`${BASE}/app/#cases`, { waitUntil: "domcontentloaded" });
  const panel = page.locator("[data-odoo20-plan]");
  await panel.waitFor({ timeout: 15000 });
  step("task_panel_visible", true);

  const fields = panel.locator("input");
  await fields.nth(0).fill(PO);
  await fields.nth(1).fill(REASON);
  await panel.getByRole("button", { name: /prepare/i }).click();
  await panel.locator("p", { hasText: /awaiting approval/i }).first()
    .waitFor({ timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/1-prepared.png`, animations: "disabled" });
  const prepared = await panel.innerText();
  step("prepared", /awaiting approval/i.test(prepared), { panel: prepared.slice(0, 200) });

  await panel.getByRole("button", { name: /approve/i }).first().click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${OUT}/2-approved.png`, animations: "disabled" });
  const approved = await panel.innerText();
  step("approved", /apply/i.test(approved), { panel: approved.slice(0, 200) });

  await panel.getByRole("button", { name: /apply/i }).first().click();
  await page.waitForTimeout(4000);
  await page.screenshot({ path: `${OUT}/3-applied.png`, animations: "disabled" });
  const applied = await page.locator(".odoo20-panel").innerText();
  step("applied_and_verified", /verified|已执行并验证/i.test(applied),
       { panel: applied.slice(0, 300) });

  // The queue must reflect the finished matter without a manual review gate.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  const queue = await page.locator("[data-cases-view]").innerText();
  report.queue = queue.split("\n").map(line => line.trim()).filter(Boolean).slice(0, 25);
  await page.screenshot({ path: `${OUT}/4-queue.png`, animations: "disabled" });

  await page.goto(`${BASE}/app/#runs`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/5-evidence.png`, animations: "disabled" });

  await writeFile(`${OUT}/report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} finally {
  await browser.close();
}
if (failed) process.exitCode = 1;
