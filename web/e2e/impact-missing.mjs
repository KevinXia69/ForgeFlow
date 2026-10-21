/** Old/legacy case without an impact chain must say so, not crash. */
import { chromium } from "playwright-core";
import { readFileSync } from "node:fs";
import { CASE_ID, startRevisionFixture } from "./revision-fixture.mjs";
const evidence = JSON.parse(readFileSync(new URL("./fixtures/impact-a-reprojection.json", import.meta.url), "utf8"));
const replay = structuredClone(evidence.cases[0].replay_case);
replay.case_id = CASE_ID;
replay.synthetic = true;
replay.plan.impact_chain = null;
for (const plan of replay.comparison?.plans ?? []) plan.impact_chain = null;
replay.decision = { ...(replay.decision || {}), can_approve: false, can_reject: false, can_request_changes: false };
const instance = await startRevisionFixture();
instance.fixture.current = replay;
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, locale: "en-US" });
  await page.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
  await page.goto(`${instance.url}/app/#case=${CASE_ID}`);
  await page.locator("[data-case-detail]").waitFor({ timeout: 15000 });
  const missing = page.locator("[data-case-missing]");
  await missing.waitFor();
  const text = await missing.innerText();
  if (!text.includes("impact links")) throw new Error(`missing impact explanation absent: ${text}`);
  if (await page.locator("[data-impact-summary]").count()) throw new Error("missing chain rendered an impact summary");
  console.log("impact-missing=ok (saved A without chain; no ERP/model)");
} finally {
  await browser.close();
  await instance.close();
}
