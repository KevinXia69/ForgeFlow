// Read-only controls against a real loopback FastAPI workbench.
import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const port = Number(process.argv[2]);
const profile = process.argv[3];
const entrances = { review: ["/recovery-review"], continuation: ["/investigation"], none: [] };
assert(Number.isInteger(port) && port > 0 && port <= 65535 && Object.hasOwn(entrances, profile));
const invite = process.env.FORGEFLOW_BROWSER_INVITE;
assert(invite, "A temporary local workspace invite is required");
const base = `http://127.0.0.1:${port}`;
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const context = await browser.newContext();
  await context.route("**/*", route => new URL(route.request().url()).origin === base
    ? route.continue() : route.abort());
  const page = await context.newPage();
  await page.goto(base);
  await page.locator("input[type=password]").fill(invite);
  await page.locator("form button[type=submit]").click();
  await page.locator("aside").waitFor();
  const links = page.locator('aside a[href^="/"]');
  assert.deepEqual(await links.evaluateAll(elements => elements.map(el => el.pathname)), entrances[profile]);
  if (entrances[profile].length) {
    await links.click();
    await page.locator(profile === "review" ? "#review" : "#workspace").waitFor();
    await page.locator("header a").first().click();
    await page.locator("aside").waitFor();
  }

  const response = await context.request.get(base + "/api/runs");
  assert.equal(response.status(), 200);
  const rows = await response.json();
  assert(rows.length, "Seed this instance with a historical run");
  await page.locator("[data-nav=runs]").click();
  const rowLinks = page.locator(".run-link");
  await rowLinks.first().waitFor();
  const pagination = page.locator(".pagination button");
  if (rows.length > 15) {
    await pagination.last().click();
    assert.equal(await rowLinks.count(), Math.min(15, rows.length - 15));
    await pagination.first().click();
    assert.equal(await rowLinks.count(), 15);
  }

  await page.locator('.evidence-filters [aria-haspopup="listbox"]').first().click();
  await page.getByRole("option").last().click();
  const unknown = rows.filter(row => row.strict_success !== false && row.evaluation_valid !== false
    && (row.strict_success == null || row.evaluation_valid == null));
  assert.equal(await rowLinks.count(), Math.min(15, unknown.length));
  console.log(JSON.stringify({ browser: browser.version(), profile, runs: rows.length, unknown: unknown.length }));
} finally {
  await browser.close();
}
