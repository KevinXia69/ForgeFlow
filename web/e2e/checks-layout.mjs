/**
 * Read-only layout measurement against existing A/B sandbox cases.
 *
 * Required: FORGEFLOW_LAYOUT_A_URL and FORGEFLOW_LAYOUT_B_URL.
 * Optional: FORGEFLOW_LAYOUT_A_CASE_ID / FORGEFLOW_LAYOUT_B_CASE_ID. Without
 * an ID, that API must contain exactly one case for the requested scenario.
 * FORGEFLOW_LAYOUT_INVITE (or FORGEFLOW_INVITE) supplies the session invite.
 * FORGEFLOW_LAYOUT_OUT writes checks-layout.json to the specified directory.
 *
 * Creates an authentication session only. Never creates/selects/approves a
 * case, asks the model, or writes ERP data. Produces no screenshots or video.
 */
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright-core";

const invite = process.env.FORGEFLOW_LAYOUT_INVITE ?? process.env.FORGEFLOW_INVITE ?? "demo-invite-code-0001";
const targets = ["A", "B"].map(scenario => {
  const value = process.env[`FORGEFLOW_LAYOUT_${scenario}_URL`];
  assert.ok(value, `FORGEFLOW_LAYOUT_${scenario}_URL is required`);
  const url = new URL(value);
  assert.ok(["http:", "https:"].includes(url.protocol), "API URL must use HTTP(S)");
  assert.ok(!url.username && !url.password, "Use the invite environment variable, not URL credentials");
  return { scenario, base: url.origin, caseId: process.env[`FORGEFLOW_LAYOUT_${scenario}_CASE_ID`] };
});

const browser = await chromium.launch({ channel: "chrome", headless: true });
const results = [];
try {
  for (const target of targets) {
    for (const lang of ["zh", "en"]) {
      for (const width of [1440, 390]) {
        const context = await browser.newContext({ viewport: { width, height: 1000 } });
        try {
          const session = await context.request.post(`${target.base}/api/auth/session`, {
            data: { invite_code: invite },
          });
          assert.equal(session.status(), 201, `${target.scenario}: session creation failed`);
          let caseId = target.caseId;
          if (!caseId) {
            const response = await context.request.get(`${target.base}/api/product/cases`);
            assert.equal(response.status(), 200, `${target.scenario}: cannot read existing cases`);
            const cases = (await response.json()).filter(item => item.scenario === target.scenario);
            assert.equal(cases.length, 1, `Set FORGEFLOW_LAYOUT_${target.scenario}_CASE_ID when the API has zero or multiple cases`);
            caseId = cases[0].case_id;
          }
          const response = await context.request.get(`${target.base}/api/product/cases/${encodeURIComponent(caseId)}`);
          assert.equal(response.status(), 200, `${target.scenario}: existing case unavailable`);
          const caseFile = await response.json();
          assert.equal(caseFile.scenario, target.scenario, "Case scenario differs from requested target");
          assert.ok(caseFile.comparison?.plans?.length, "Existing case has no candidate checks to measure");
          await context.addInitScript(value => localStorage.setItem("forgeflow.lang", value), lang);

          const blockedRequests = [];
          await context.route("**/*", async route => {
            const request = route.request();
            if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
              blockedRequests.push({ method: request.method(), path: new URL(request.url()).pathname });
              await route.abort("blockedbyclient");
            } else await route.continue();
          });
          const page = await context.newPage();
          const errors = [];
          page.on("pageerror", error => errors.push(String(error)));
          await page.goto(`${target.base}/app/#case=${encodeURIComponent(caseId)}`);
          await page.locator("[data-candidate-details]").first().waitFor();
          for (const details of await page.locator("[data-candidate-details]").all()) {
            await details.locator("summary").first().click();
          }
          const measured = await page.locator(".candidate-card .policy-check").evaluateAll(rows => rows.map(row => {
            const label = row.querySelector(".policy-check-label").getBoundingClientRect();
            const value = row.querySelector(".policy-check-value").getBoundingClientRect();
            return {
              overlap: label.left < value.right && label.right > value.left && label.top < value.bottom && label.bottom > value.top,
              overflow: row.scrollWidth > row.clientWidth + 1,
            };
          }));
          assert.ok(measured.length, "No visible checks measured");
          assert.ok(!measured.some(item => item.overlap || item.overflow), JSON.stringify({ scenario: target.scenario, lang, width, measured }));
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, "Page has horizontal overflow");
          assert.deepEqual(blockedRequests, [], "Page attempted a non-read-only request");
          assert.deepEqual(errors, [], "Browser reported a script error");
          results.push({ scenario: target.scenario, lang, width, case_id: caseId, plan_id: caseFile.plan?.plan_id,
            checks: measured.length, overlap: false, overflow: false, mutation_requests: 0, console_errors: 0 });
        } finally {
          await context.close();
        }
      }
    }
  }
} finally {
  await browser.close();
}
const report = { category: "read_only_existing_case_layout_measurement", passed: true, combinations: results.length,
  captures_created: false, results };
const output = JSON.stringify(report, null, 2) + "\n";
if (process.env.FORGEFLOW_LAYOUT_OUT) {
  await mkdir(process.env.FORGEFLOW_LAYOUT_OUT, { recursive: true });
  await writeFile(path.join(process.env.FORGEFLOW_LAYOUT_OUT, "checks-layout.json"), output);
}
process.stdout.write(output);
