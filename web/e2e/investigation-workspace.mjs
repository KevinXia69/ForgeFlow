// Use an isolated local continuation service. Both test identities must start empty.
import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [port] = process.argv.slice(2);
assert(/^\d{1,5}$/.test(port), "expected a local port");
const owner = process.env.FORGEFLOW_BROWSER_INVITE, other = process.env.FORGEFLOW_SECOND_BROWSER_INVITE;
assert(owner && other && owner !== other);
const base = "http://127.0.0.1:" + port, cases = base + "/api/investigation/cases";
async function login(page, code) {
  await page.goto(base + "/investigation");
  const ready = page.waitForResponse(r => r.url() === cases && r.request().method() === "GET" && r.status() === 200);
  await page.locator("#invite").fill(code);
  await page.locator("#login-form button").click();
  await ready;
  await page.locator("#workspace").waitFor();
}
const browser = await chromium.launch({channel:"chrome", headless:true});
try {
  const context = await browser.newContext(), first = await context.newPage();
  context.setDefaultTimeout(10000);
  await login(first, owner);
  const scope = (await (await first.request.get(base + "/api/auth/me")).json()).scope_id;
  await first.locator("#empty").waitFor();
  const swap = await context.request.post(base + "/api/auth/session", {data:{invite_code:other}});
  assert.equal(swap.status(), 201);
  const reopened = first.waitForEvent("load");
  const result = first.waitForResponse(r => r.url() === cases && r.request().method() === "POST");
  await first.locator("#create").click();
  const denied = await result;
  assert.equal(denied.request().headers()["x-forgeflow-workspace"], scope);
  assert.equal(denied.status(), 409);
  assert.equal(denied.headers()["x-forgeflow-workspace-changed"], "1");
  assert.deepEqual((await (await context.request.get(cases)).json()).cases, []);
  await reopened;

  const details = await browser.newContext(), oldTab = await details.newPage();
  details.setDefaultTimeout(10000);
  const sameOwnerLogin = await details.newPage();
  await sameOwnerLogin.goto(base + "/investigation");
  await sameOwnerLogin.locator("#invite").waitFor();
  await login(oldTab, owner);
  await oldTab.locator("#create").click();
  await oldTab.locator("#question").waitFor();
  const caseId = new URL(oldTab.url()).searchParams.get("case");
  assert(caseId);
  // The peer has already rendered A's case when its connection goes offline.
  // A successful login in another tab must still retire that document.
  const offline = await details.newPage();
  await offline.goto(oldTab.url());
  await offline.locator("#question").waitFor();
  const network = await details.newCDPSession(offline);
  await network.send("Network.enable");
  await network.send("Network.emulateNetworkConditions", {
    offline:true, latency:0, downloadThroughput:0, uploadThroughput:0,
  });
  const second = await details.newPage();
  await oldTab.bringToFront();
  const draft = oldTab.locator("#reply");
  await draft.fill("Unsent reply for the original owner");
  await second.bringToFront(); await oldTab.bringToFront();
  assert.equal(await draft.inputValue(), "Unsent reply for the original owner");
  const verified = oldTab.waitForResponse(r => r.url() === base + "/api/auth/me"
    && r.request().headers()["x-forgeflow-workspace"] && r.status() === 200);
  await sameOwnerLogin.locator("#invite").fill(owner);
  await sameOwnerLogin.locator("#login-form button").click();
  await verified;
  await draft.waitFor();
  assert.equal(await draft.inputValue(), "Unsent reply for the original owner");
  assert.equal(new URL(oldTab.url()).searchParams.get("case"), caseId);
  await details.clearCookies();
  await login(second, other);
  await oldTab.waitForFunction(() => !location.search && !document.querySelector("#question")?.textContent
    && !document.querySelector("#reply")?.value);
  assert.equal(await oldTab.locator("#case-panels").isVisible(), false);
  await offline.locator("#question").waitFor({state:"hidden"});
  await network.send("Network.emulateNetworkConditions", {
    offline:false, latency:0, downloadThroughput:-1, uploadThroughput:-1,
  });
  await offline.waitForFunction(() => !location.search && !document.querySelector("#question")?.textContent);
  assert.equal((await details.request.get(cases + "/" + caseId)).status(), 404);
  assert.deepEqual((await (await details.request.get(cases)).json()).cases, []);
  console.log(JSON.stringify({chrome:browser.version(), stale_create:denied.status(),
    second_owner_cases:0, previous_details_and_draft_cleared:true, same_owner_relogin_preserved_draft:true, offline_peer_concealed_and_recovered:true}));
} finally {
  await browser.close();
}
