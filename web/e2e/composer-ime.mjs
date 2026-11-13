/** Composer keyboard regression. Built UI and mock API only; no model or ERP calls. */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

// Point this at a temporary static root containing app/ to test either bundle.
const root = resolve(process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url)));
const contentTypes = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const server = createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    const path = resolve(root, `.${pathname}`, ...(pathname.endsWith("/") ? ["index.html"] : []));
    if (!path.startsWith(root + sep)) throw new Error("outside static root");
    const content = await readFile(path);
    response.writeHead(200, { "Content-Type": contentTypes[extname(path)] || "application/octet-stream" });
    response.end(content);
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
let browser;
try {
  browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage();
  const submitted = [];
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const responses = {
      "/api/auth/me": { scope_id: "composer-ime-test", display_name: "Test", deployment: "local" },
      "/api/config": { deployment: "local", runner_enabled: false },
      "/api/product/cases": [],
      "/api/runs": [],
      "/api/capabilities": { case_approve: false },
    };
    if (path === "/api/assistant/stream") {
      submitted.push(route.request().postDataJSON());
      return route.fulfill({ contentType: "text/event-stream", body: 'id: 1\ndata: {"type":"final","found":true,"mode":"harness","answer":"Mock answer","passages":[],"citations":[],"activity":[]}\n\n' });
    }
    if (Object.hasOwn(responses, path)) return route.fulfill({ json: responses[path] });
    return route.fulfill({ status: 404, json: { detail: "unexpected_test_request" } });
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/app/`);
  const input = page.locator("[data-composer-input]");
  const text = "测试输入尚未完成";
  await input.fill(text);
  await page.waitForFunction(() => !document.querySelector("button.send").disabled);

  // Exercise the browser keyboard fields at React's actual contenteditable
  // boundary; checking only a helper would miss an unguarded event handler.
  for (const fields of [{ isComposing: true, keyCode: 13 }, { isComposing: false, keyCode: 229 }]) {
    await input.evaluate((element, fields) => {
      element.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter", code: "Enter", bubbles: true, cancelable: true, ...fields,
      }));
    }, fields);
    assert.equal(await page.locator(".msg.user").count(), 0, "IME confirmation submitted a user message");
    assert.equal(submitted.length, 0, "IME confirmation started a model request");
    assert.ok((await input.innerText()).includes(text), "IME confirmation cleared the draft");
  }

  // A synthetic keydown has no OS input method behind it, so reload before
  // testing real keyboard editing rather than depending on its DOM side effects.
  await page.reload();
  await input.fill(text);
  await page.waitForFunction(() => !document.querySelector("button.send").disabled);
  await input.press("Shift+Enter");
  await page.keyboard.insertText("继续输入");
  assert.match(await input.innerText(), /测试输入尚未完成\n+继续输入/, "Shift+Enter did not insert a newline");
  assert.equal(await page.locator(".msg.user").count(), 0, "Shift+Enter submitted a message");
  assert.equal(submitted.length, 0);

  const reply = page.waitForResponse(response => new URL(response.url()).pathname === "/api/assistant/stream");
  await input.press("Enter");
  await reply;
  await page.locator(".msg.assistant").waitFor();
  assert.equal(submitted.length, 1, "ordinary Enter must submit exactly once");
  assert.match(submitted[0].question, /测试输入尚未完成\n+继续输入/);
  assert.equal(await page.locator(".msg.user").count(), 1);
  console.log("composer-ime=ok (composition Enter, Safari 229, Shift+Enter, ordinary Enter; mock API)");
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
