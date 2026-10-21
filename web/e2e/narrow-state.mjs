/** Report 390px horizontal overflow and narrow stepper readability, zh and en. */
import { chromium } from "playwright-core";

const base = process.env.FORGEFLOW_BASE || "http://127.0.0.1:8123";
const invite = process.env.FORGEFLOW_INVITE || "firstlook-local-invite-code";
const baseOut = process.env.FORGEFLOW_OUT || "/tmp/forgeflow-shots";
const out = `${baseOut.replace(/\/$/, "")}/screens`;
import { mkdir } from "node:fs/promises";
await mkdir(out, { recursive: true });

const browser = await chromium.launch({ channel: "chrome", headless: true });
let failures = 0;

for (const locale of ["zh-CN", "en-US"]) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale });
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "networkidle" });
  await page.locator("#invite-code").fill(invite);
  await page.locator('button[type="submit"]').first().click();
  await page.locator("[data-home-view]").waitFor({ timeout: 20000 });

  const measure = () => page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const offenders = [...document.querySelectorAll("*")]
      .map((el) => { const r = el.getBoundingClientRect(); return { tag: el.tagName, cls: String(el.className).slice(0, 48), right: Math.round(r.right) }; })
      .filter((x) => x.right > vw + 1).slice(0, 8);
    return { vw, scrollWidth: document.documentElement.scrollWidth, offenders };
  });

  const report = async (label, facts) => {
    const bad = facts.scrollWidth > facts.vw + 1;
    if (bad) failures += 1;
    console.log(`${locale} ${label}: ${JSON.stringify(facts)}${bad ? "  <-- overflow" : ""}`);
  };

  await report("home", await measure());
  await page.goto(`${base}/#cases`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  await report("cases", await measure());

  const awaiting = page.locator('[data-case-row="true"][data-case-state="awaiting_approval"]').first();
  if (await awaiting.count()) {
    await awaiting.click();
    await page.locator("[data-case-detail]").waitFor({ timeout: 20000 });
    await page.waitForTimeout(800);
    await report("detail", await measure());
    const stepper = await page.evaluate(() => {
      const steps = [...document.querySelectorAll("[data-case-stepper] li")];
      const bar = document.querySelector("[data-case-sticky-decision]");
      const br = bar ? bar.getBoundingClientRect() : null;
      const last = steps.length ? steps[steps.length - 1].getBoundingClientRect() : null;
      return {
        steps: steps.length,
        stacked: steps.length > 1 && Math.round(steps[1].getBoundingClientRect().top) > Math.round(steps[0].getBoundingClientRect().top),
        coveredByBar: br && last ? (br.top < last.bottom && br.bottom > last.top) : null,
        labels: steps.map((li) => (li.querySelector(".status-step-label")?.textContent ?? "").trim()),
        labelWidths: steps.map((li) => Math.round(li.querySelector(".status-step-label")?.getBoundingClientRect().width ?? 0)),
      };
    });
    if (stepper.steps !== 5) failures += 1;
    if (stepper.labels.some((label) => label.length === 0)) failures += 1;
    if (stepper.labelWidths.some((width) => width < 24)) failures += 1;
    if (stepper.coveredByBar) failures += 1;
    console.log(`${locale} stepper: ${JSON.stringify(stepper)}${stepper.steps !== 5 || stepper.coveredByBar ? "  <-- incomplete or covered" : ""}`);
    await page.screenshot({ path: `${out}/narrow-${locale}-detail.png`, fullPage: true });
  } else {
    console.log(`${locale} detail: no awaiting case in this workspace (skipped)`);
  }
  await context.close();
}

await browser.close();
if (failures) {
  console.log(`narrow=FAILED (${failures} problems at 390px)`);
  process.exitCode = 1;
} else {
  console.log("narrow=ok (390px: no overflow, five readable steps, bar does not cover content)");
}
