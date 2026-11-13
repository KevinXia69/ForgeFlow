/** Candidate switching with per-candidate impact chains; synthetic HTTP only. */
import { chromium } from "playwright-core";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { CASE_ID, startRevisionFixture } from "./revision-fixture.mjs";
import { candidateCase, wireCandidateFixture } from "./candidates-fixture.mjs";

const realFile = process.env.FORGEFLOW_CANDIDATES_FILE;
let caseFile;
if (realFile) {
  const saved = JSON.parse(readFileSync(new URL(realFile, "file://" + process.cwd() + "/"), "utf8"));
  caseFile = structuredClone(saved.cases[0].replay_case);
  caseFile.case_id = CASE_ID;
} else {
  const evidence = JSON.parse(readFileSync(new URL("./fixtures/impact-a-reprojection.json", import.meta.url), "utf8"));
  const baseChain = evidence.cases[0].replay_case.plan.impact_chain;
  caseFile = candidateCase({ capacity: true });
  for (const plan of caseFile.comparison.plans) {
    const chain = structuredClone(baseChain);
    chain.basis.plan_id = plan.plan_id;
    chain.basis.snapshot_sha256 = plan.snapshot_sha256;
    plan.impact_chain = chain;
  }
}
const staticDir = process.env.FORGEFLOW_IMPACT_STATIC;
const instance = await startRevisionFixture(staticDir ? { directory: resolve(staticDir) } : {});
wireCandidateFixture(instance.fixture);
instance.fixture.current = caseFile;
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, locale: "en-US" });
  await page.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
  await page.goto(`${instance.url}/app/#case=${CASE_ID}`);
  await page.locator("[data-candidate-comparison=ready]").waitFor({ timeout: 15000 });
  const candidates = page.locator("[data-candidate]");
  const candidateCount = await candidates.count();
  if (candidateCount < 2) throw new Error(`candidate fixture needs at least two plans, got ${candidateCount}`);
  const second = candidates.nth(1);
  await second.locator("[data-candidate-details] > summary").click();
  await second.locator("[data-impact-summary]").waitFor();
  const secondSummary = await second.locator("[data-impact-summary]").innerText();
  if (!secondSummary.includes("recorded relationships")) throw new Error("second candidate impact summary missing");
  await second.locator("[data-impact-details] > summary").click();
  await second.locator("[data-impact-node]").first().click();
  await second.locator("[data-impact-node-facts]").waitFor();
  if (await candidates.nth(0).locator("[data-impact-node-facts]").count()) throw new Error("candidate facts leaked into the other candidate");
  const select = page.locator(`[data-select-candidate="${caseFile.comparison.plans[1].plan_id}"]`);
  if (await select.count()) {
    await select.click();
    await page.locator(`[data-candidate="${caseFile.comparison.plans[1].plan_id}"][data-candidate-selected="true"]`).waitFor();
  } else {
    if (caseFile.state !== "succeeded") throw new Error("read-only candidate has no select button for an unexpected state");
  }
  console.log(`impact-candidates=ok (${realFile ? "saved B data" : "synthetic HTTP"}; no ERP/model)`);
} finally {
  await browser.close();
  await instance.close();
}
