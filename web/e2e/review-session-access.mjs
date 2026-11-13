// Use a dedicated local workspace with a registered owner awaiting clarification.
import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [port] = process.argv.slice(2);
assert(/^\d{1,5}$/.test(port), "expected a local port");
const owner = process.env.FORGEFLOW_BROWSER_INVITE;
const other = process.env.FORGEFLOW_SECOND_BROWSER_INVITE;
assert(owner && other && owner !== other, "two different local test invites required");
const url = "http://127.0.0.1:" + port + "/recovery-review";

async function login(page, code) {
  await page.locator("#invite").fill(code);
  const response = page.waitForResponse(r => r.url().endsWith("/api/auth/session") && r.request().method() === "POST");
  await page.locator("#login-form button").click();
  return (await response).status();
}

const browser = await chromium.launch({channel: "chrome", headless: true});
try {
  const context = await browser.newContext();
  const first = await context.newPage();
  await first.goto(url);
  assert.equal(await login(first, "deliberately-invalid-test-invite"), 401);
  await first.waitForFunction(() => document.querySelector("#message").textContent.length > 0);
  assert.equal(await login(first, owner), 201);
  const original = first.locator("[data-review-turn]").first();
  await original.waitFor();
  const operation = await original.getAttribute("data-review-turn");
  assert.equal(await first.locator("#message").innerText(), "");

  // A confirmed logout hides all business rows. The same owner can recover an
  // undelivered operation with its original selections and id after login.
  const notice = first.locator("#notice"), line = first.locator("#line");
  await notice.selectOption({index: 1});
  await line.selectOption({index: 1});
  const selections = [await notice.inputValue(), await line.inputValue()];
  await context.setOffline(true);
  await first.locator("#clarify-form button").click();
  await first.waitForFunction(() => Boolean(sessionStorage.getItem("recovery-review.pending")));
  const pending = await first.evaluate(() => sessionStorage.getItem("recovery-review.pending"));
  await context.clearCookies();
  await context.setOffline(false);
  await first.locator("#login").waitFor();
  assert.equal(await first.locator("#turns").textContent(), "");
  assert.equal(await first.locator("#event").textContent(), "");
  assert.equal(await login(first, owner), 201);
  await notice.waitFor();
  assert.deepEqual([await notice.inputValue(), await line.inputValue()], selections);
  assert.equal(await first.evaluate(() => sessionStorage.getItem("recovery-review.pending")), pending);

  // Keep the owner's tab suspended while another tab logs in, like a background
  // tab waking up with an already-replaced browser cookie.
  await first.clock.install();
  await first.clock.pauseAt(new Date(Date.now() + 300));
  const second = await context.newPage();
  await context.clearCookies();
  await second.goto(url);
  assert.equal(await login(second, other), 201);
  await second.getByText("当前身份无权访问此工作台，请使用登记身份重新登录。").waitFor();

  const denied = first.waitForResponse(r => r.url().endsWith("/recovery-review/session") && r.status() === 403);
  await first.bringToFront();
  await first.clock.runFor(3600);
  await denied;
  await first.locator("#login").waitFor();
  assert.equal(await first.locator("#review").isVisible(), false);
  assert.equal(await first.locator("#turns").textContent(), "");
  assert.equal(await first.locator("#event").textContent(), "");
  assert.equal(await first.locator("#question").inputValue(), "");
  assert.equal(await first.locator("#clarify-form option").count(), 0);
  assert.equal(await first.evaluate(() => sessionStorage.getItem("recovery-review.pending")), null);

  assert.equal(await login(first, owner), 201);
  await original.waitFor();
  assert.equal(await original.getAttribute("data-review-turn"), operation);
  assert.equal(await first.locator("#message").innerText(), "");
  console.log("Real session denied the new identity, cleared the prior review and restored its registered owner.");
} finally {
  await browser.close();
}
