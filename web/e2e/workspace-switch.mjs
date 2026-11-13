// Requires a local service, two isolated reviewer invites, and a readable case owned by the first.
import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [port, caseId] = process.argv.slice(2);
assert(/^\d{1,5}$/.test(port) && /^[a-f0-9]{32}$/.test(caseId));
const owner = process.env.FORGEFLOW_BROWSER_INVITE, other = process.env.FORGEFLOW_SECOND_BROWSER_INVITE;
assert(owner && other && owner !== other);
const base = "http://127.0.0.1:" + port;
const browser = await chromium.launch({channel: "chrome", headless: true});
const question = "Which evidence supports the original supplier notice for the first reviewer?";
const marker = "local-browser-test-only";
async function login(page, invite) {
  await page.locator("#invite-code").fill(invite);
  await page.locator(".login-card button[type=submit]").click();
  await page.locator(".settings-trigger").waitFor();
}
async function testConfig(page) {
  const scope = (await (await page.request.get(base + "/api/auth/me")).json()).scope_id;
  await page.evaluate(({scope, marker}) => {
    localStorage.setItem("forgeflow.model." + scope, JSON.stringify({
      profile: "custom", base_url: "http://127.0.0.1:1/v1", model: "local-scope-check", api_key: marker,
    }));
  }, {scope, marker});
  return scope;
}
async function ownerGone(page) {
  await page.locator(".settings-trigger").waitFor();
  assert.equal(await page.locator("[data-case-decision-summary]").count(), 0);
  assert.equal(await page.getByText(question, {exact: true}).count(), 0);
  await page.locator(".settings-trigger").click();
  if (await page.locator("#settings-tab-model").count()) await page.locator("#settings-tab-model").click();
  const model = page.locator("#settings-model");
  assert.equal(await model.count() > 0 && await model.inputValue() === "local-scope-check", false);
}
try {
  const context = await browser.newContext(), page = await context.newPage();
  await page.goto(base); await login(page, owner);
  await testConfig(page);
  const second = await context.newPage();
  await second.goto(base);
  await second.locator(".settings-trigger").waitFor();
  await page.bringToFront();
  const composer = page.locator("[data-composer-input]");
  await composer.fill(question);
  await second.bringToFront(); await page.bringToFront();
  assert.equal(await composer.innerText(), question);
  const network = await context.newCDPSession(page);
  await network.send("Network.enable");
  await network.send("Network.emulateNetworkConditions", {
    offline: false, latency: 700, downloadThroughput: -1, uploadThroughput: -1,
  });
  const answered = page.waitForResponse(r => r.url().endsWith("/api/assistant/answer"));
  await composer.press("Enter");
  await second.bringToFront(); await page.bringToFront();
  const answerStatus = (await answered).status();
  assert.equal(answerStatus, 200);
  await page.locator(".msg.assistant").last().waitFor();
  await network.detach();
  await page.goto(base + "/#case=" + caseId);
  await page.locator("[data-case-decision-summary]").waitFor();

  await second.bringToFront();
  await second.locator(".settings-trigger").click();
  if (await second.locator("#settings-tab-general").count()) await second.locator("#settings-tab-general").click();
  await second.getByRole("button", {name: /Sign out|退出工作区|退出登录/i}).click();
  await login(second, other);
  await page.bringToFront();
  await page.locator("[data-case-decision-summary]").waitFor({state:"detached"});
  await ownerGone(page);
  await context.close();

  // Replace only the HttpOnly cookie (e.g. a different API client), bypassing
  // the workbench's cross-tab notification. Click from the still-focused old tab.
  const race = await browser.newContext(), stale = await race.newPage();
  await stale.goto(base); await login(stale, owner); const scope = await testConfig(stale);
  const pending = stale.locator("[data-composer-input]");
  await pending.fill("Check the workspace binding of this local test question.");
  const changed = await race.request.post(base + "/api/auth/session", {
    data:{invite_code:other}, headers:{Origin:base},
  });
  assert.equal(changed.status(), 201);
  const response = stale.waitForResponse(r => r.url().endsWith("/api/assistant/answer"));
  await pending.press("Enter");
  const refused = await response;
  assert.equal(refused.request().headers()["x-forgeflow-workspace"], scope);
  assert.equal(refused.status(), 409);
  assert.equal(refused.headers()["x-forgeflow-workspace-changed"], "1");
  await stale.waitForLoadState("load");
  await ownerGone(stale);
  await race.close();

  // A completed login on the fallback page must retire an open React workspace
  // even if that tab itself cannot reach the identity endpoint at the time.
  const offline = await browser.newContext(), oldTab = await offline.newPage();
  await oldTab.goto(base + "/#case=" + caseId); await login(oldTab, owner);
  await oldTab.locator("[data-case-decision-summary]").waitFor();
  const staticTab = await offline.newPage();
  const oldNetwork = await offline.newCDPSession(oldTab);
  await oldNetwork.send("Network.enable");
  await oldNetwork.send("Network.emulateNetworkConditions", {
    offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0,
  });
  await offline.clearCookies();
  await staticTab.goto(base + "/static/index.html");
  await staticTab.locator("#invite-code").fill(other);
  await staticTab.locator("#login-button").click();
  await staticTab.locator("#app-panel").waitFor();
  await oldTab.locator("[data-case-decision-summary]").waitFor({state:"detached"});
  await offline.close();
  console.log(JSON.stringify({
    chrome: browser.version(), focus_preserved_draft: true, same_scope_answer_http: answerStatus,
    previous_case_chat_and_config_cleared: true, unannounced_cookie_swap_http: refused.status(),
    scope_mismatch_signalled: refused.headers()["x-forgeflow-workspace-changed"] === "1",
    fallback_login_cleared_offline_tab: true,
  }));
} finally {
  await browser.close();
}
