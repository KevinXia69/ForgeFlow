import { chromium } from "playwright-core";
import assert from "node:assert/strict";

const [port, id] = process.argv.slice(2);
assert(/^\d{1,5}$/.test(port) && /^[a-f0-9]{32}$/.test(id));
const origin = "http://127.0.0.1:" + port;
const browser = await chromium.launch({channel: "chrome", headless: true});
const context = await browser.newContext();
const report = {case_id: id, steps: [], requests: []};
try {
  const page = await context.newPage();
  page.setDefaultTimeout(45000);
  page.on("response", response => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith("/api/product/cases/" + id))
      report.requests.push({method:response.request().method(), path, status:response.status()});
  });
  await page.goto(origin + "/app/#case=" + id);
  await page.locator(".login-card input").fill(process.env.FORGEFLOW_BROWSER_INVITE);
  await page.locator('.login-card button[type="submit"]').click();
  await page.locator("[data-recovery-review]").waitFor();
  report.candidate_count = await page.locator("[data-candidate]").count();
  assert(report.candidate_count === 1 || report.candidate_count === 2);
  report.tradeoff = await page.locator("[data-review-tradeoff]").allTextContents();
  assert.equal(report.tradeoff.length, report.candidate_count === 2 ? 1 : 0);
  if (report.candidate_count === 2) {
    assert(await page.locator("[data-review-difference]").count() > 0);
    report.decisive_lines = await page.locator("[data-review-difference]").allTextContents();
  }
  const choose = page.locator("[data-select-candidate]:enabled").first();
  if (report.candidate_count === 2) {
    await choose.click();
    report.steps.push("selected a real alternative against the current comparison");
  } else {
    assert.equal(await choose.count(), 0);
    report.steps.push("single actual plan without a second choice or cost contrast");
  }
  await page.locator("[data-review-open]").waitFor();
  await page.reload();
  const selected = page.locator('[data-candidate-selected="true"]');
  await selected.waitFor();
  report.selected_plan_id = await selected.getAttribute("data-candidate");
  report.steps.push("selected candidate persists after reload");
  await page.locator('[data-review-source="' + report.selected_plan_id + '"]').click();
  const snapshot = page.locator("[data-review-snapshot]");
  await snapshot.waitFor();
  report.snapshot_sha256 = await snapshot.getAttribute("data-review-snapshot");
  assert(await page.locator('[data-review-row^="mrp.workcenter:"]').count() > 0);
  const orderId = await page.locator("[data-review-order]").inputValue();
  report.order_id = orderId;
  const original = await page.locator('[data-review-row="sale.order:' + orderId + '"]').innerText();
  assert(original.includes("commitment_date="));
  report.original_row = original;
  report.steps.push("read original scoped ERP row and manufacturing standard");
  await page.locator("[data-review-kind]").selectOption("deadline");
  await page.locator("[data-review-note]").fill("Please verify the current committed delivery date on the registered sales order.");
  await page.locator("[data-review-open]").click();
  await page.locator('[data-review-disposition="pending"]').waitFor();
  await page.reload();
  await page.locator('[data-review-disposition="pending"]').waitFor();
  assert(await page.locator("[data-review-open]").isDisabled());
  report.steps.push("submitted objection persists across reload");
  await page.locator("[data-review-check]").click();
  const result = page.locator('[data-review-disposition="retained"]');
  await result.waitFor();
  report.disposition = await result.innerText();
  assert(report.disposition.includes("commitment_date") && report.disposition.includes("write_date"));
  assert.equal(await selected.getAttribute("data-candidate"),report.selected_plan_id);
  report.steps.push("registered source confirms same field; original plan selected");
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.error = String(error.message).slice(0,800);
  process.exitCode = 1;
} finally {
  await browser.close();
  process.stdout.write(JSON.stringify(report));
}
