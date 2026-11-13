/** Controlled ImpactChain UI replay from saved A reprojection; no ERP/model. */
import { chromium } from "playwright-core";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CASE_ID, startRevisionFixture } from "./revision-fixture.mjs";

const source = process.env.FORGEFLOW_IMPACT_FILE
  ? new URL(process.env.FORGEFLOW_IMPACT_FILE, "file://" + process.cwd() + "/")
  : new URL("./fixtures/impact-a-reprojection.json", import.meta.url);
const evidence = JSON.parse(readFileSync(source, "utf8"));
const replay = structuredClone(evidence.cases[0].replay_case);
replay.case_id = CASE_ID;
replay.synthetic = true;
replay.decision = { ...(replay.decision || {}), can_approve: false, can_reject: false, can_request_changes: false };
const staticDir = process.env.FORGEFLOW_IMPACT_STATIC;
const instance = await startRevisionFixture(staticDir ? { directory: resolve(staticDir) } : {});
instance.fixture.current = replay;
if (process.env.FORGEFLOW_IMPACT_REVIEW === "1") instance.fixture.deployment = "review";
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, locale: "en-US" });
  const lang = process.env.FORGEFLOW_IMPACT_LANG || "en";
  await page.addInitScript(value => localStorage.setItem("forgeflow.lang", value), lang);
  await page.goto(`${instance.url}/app/#case=${CASE_ID}`);
  await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
  await page.locator("[data-impact-summary]:visible").first().waitFor();
  if (process.env.FORGEFLOW_IMPACT_REVIEW === "1") {
    if (await page.locator("[data-case-approve-sticky]").count()) throw new Error("review exposed sticky approval");
    if (await page.locator("[data-case-approve]").count()) throw new Error("review exposed approval");
    if (await page.locator("[data-select-candidate]").count()) throw new Error("review exposed candidate selection");
  }
  const summary = await page.locator("[data-impact-summary]:visible").first().innerText();
  if (!(summary.includes("recorded relationships") || summary.includes("原记录有")) || !(summary.includes("not recorded") || summary.includes("未记录"))) throw new Error(`impact summary missing plain counts: ${summary}`);
  if (await page.locator("[data-impact-details]:visible[open]").count()) throw new Error("impact details are open by default");
  const select = page.locator("[data-impact-order]:visible").first();
  await select.click();
  await page.getByRole("listbox").waitFor();
  const options = await page.getByRole("option").count();
  if (options < 2) throw new Error("impact fixture needs at least two orders");
  await page.getByRole("option").nth(1).click();
  await page.getByRole("listbox").waitFor({ state: "detached" });
  await page.locator("[data-impact-readiness]:visible").first().waitFor();
  await page.locator("[data-impact-details]:visible > summary").first().click();
  await page.locator("[data-impact-phase]:visible").first().waitFor();
  await page.locator("[data-impact-node]:visible").first().click();
  await page.locator("[data-impact-node-facts]:visible").first().waitFor();
  const sourceDetails = page.locator("[data-impact-node-facts]:visible details").first();
  if (await sourceDetails.count()) {
    await sourceDetails.locator("summary").click();
    await sourceDetails.locator("dl, [data-source-unavailable]").first().waitFor();
  }
  await page.keyboard.press("Escape");
  await page.locator("[data-impact-node-facts]:visible").first().waitFor({ state: "detached" });
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) throw new Error(`390px horizontal overflow: ${overflow}px`);
  await page.screenshot({ path: "/tmp/forgeflow-impact-390.png", fullPage: true });
  console.log(`impact-preview=ok (${source.pathname.split("/").pop()}, ${lang}; no ERP/model)`);
} finally {
  await browser.close();
  await instance.close();
}
