// Read-only smoke for the unbundled console on a local instance with the benchmark runner off.
import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [port] = process.argv.slice(2);
assert(/^\d{1,5}$/.test(port) && process.env.FORGEFLOW_BROWSER_INVITE);
const origin = "http://127.0.0.1:" + port;
const browser = await chromium.launch({channel: "chrome", headless: true});
try {
  const page = await browser.newPage();
  const locator = id => page.locator("#" + id);
  const config = await (await page.request.get(origin + "/api/config")).json();
  assert.equal(config.runner_enabled, false);
  await page.goto(origin + "/static/index.html");
  await locator("invite-code").fill(process.env.FORGEFLOW_BROWSER_INVITE);
  await locator("login-button").click();
  await locator("app-panel").waitFor();
  for (const target of ["A", "B", "runs"]) {
    await page.locator('[data-quick="' + target + '"]').click();
    if (target !== "runs") assert.equal(await locator("scenario-select").inputValue(), target);
    assert(await locator("create-run").isDisabled());
    const refreshed = page.waitForResponse(r => r.url().endsWith("/api/runs") && r.request().method() === "GET");
    await locator("runs-refresh").click();
    assert.equal((await refreshed).status(), 200);
    await page.locator('[data-view="assistant"]').click();
  }
  for (const [name, path] of [["recovery_review", "/recovery-review"], ["investigation", "/investigation"]]) {
    assert.equal(await page.locator('a[href="' + path + '"]').isVisible(), config.workbenches[name]);
  }

  await locator("kb-question").fill("ERP-Bench");
  await locator("kb-button").click();
  await page.locator(".kb-turn.assistant .citation").first().waitFor();
  await locator("new-chat").click();
  assert.equal(await page.locator(".kb-turn").count(), 0);

  const network = await page.context().newCDPSession(page);
  await network.send("Network.enable");
  await network.send("Network.emulateNetworkConditions", {
    offline: false, latency: 700, downloadThroughput: -1, uploadThroughput: -1,
  });
  await locator("kb-question").fill("ERP-Bench");
  const answer = page.waitForResponse(r => r.url().endsWith("/api/knowledge/answer"));
  await locator("kb-button").click();
  await locator("new-chat").click();
  assert.equal((await answer).status(), 200);
  await (await answer).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await locator("kb-results").textContent(), "");
  assert.equal(await locator("kb-status").textContent(), "");
  assert(await locator("kb-button").isEnabled());
  console.log("Shortcuts, read-only runner, registered investigation links and isolated knowledge replies passed.");
} finally {
  await browser.close();
}
