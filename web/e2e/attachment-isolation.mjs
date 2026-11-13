/** A late attachment response must never attach to a different draft or chat. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = Number(process.env.FORGEFLOW_BROWSER_PORT || 4196);
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const base = `http://127.0.0.1:${port}`;
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
const file = name => ({ name, mimeType: "text/plain", buffer: Buffer.from(name) });
const reference = name => ({ name, media_type: "text/plain", size: Buffer.byteLength(name), digest: createHash("sha256").update(name).digest("hex") });
let browser;
try {
  for (let attempt = 0; ; attempt++) {
    try { await fetch(`${base}/app/`); break; }
    catch (error) { if (attempt >= 50) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
  }
  browser = await chromium.launch({ channel: "chrome", headless: true });
  for (const mode of ["cancel", "new-chat", "replace"]) {
    const context = await browser.newContext();
    await context.addInitScript(() => localStorage.setItem("forgeflow.lang", "en"));
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const held = Promise.withResolvers();
    let uploads = 0;
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      const json = value => route.fulfill({ json: value });
      if (path === "/api/auth/me") return json({ scope_id: "attachment-isolation", display_name: "Test reviewer", deployment: "hosted" });
      if (path === "/api/config") return json({ deployment: "hosted", runner_enabled: false });
      if (path === "/api/settings/model") return json({ configured: false });
      if (["/api/runs", "/api/workspaces", "/api/odoo20/tasks"].includes(path)) return json([]);
      if (path === "/api/attachments") {
        uploads++;
        if (uploads === 1) { held.resolve(route); return; }
        return json(reference("current.txt"));
      }
      return route.fulfill({ status: 404, json: { detail: "not_found" } });
    });
    await page.goto(`${base}/app/`);
    await page.locator("[data-home-view]").waitFor();
    const input = page.locator('.composer input[type="file"]');
    await input.setInputFiles(file("old.txt"));
    await page.getByRole("button", { name: "Use in this conversation", exact: true }).click();
    const firstRequest = await held.promise;
    if (mode === "cancel") await page.locator(".composer-document-choice").getByRole("button", { name: "Cancel", exact: true }).click();
    if (mode === "new-chat") await page.locator("[data-chat-new]").click();
    if (mode === "replace") {
      await input.setInputFiles(file("current.txt"));
      await page.getByRole("button", { name: "Use in this conversation", exact: true }).click();
      await page.locator("[data-composer-document]").getByText("current.txt", { exact: true }).waitFor();
    }
    const response = page.waitForResponse(res => res.request() === firstRequest.request());
    await firstRequest.fulfill({ json: reference("old.txt") });
    await response;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.locator("[data-composer-document]").getByText("old.txt", { exact: true }).count(), 0, `${mode}: a late response restored the old attachment`);
    if (mode === "replace") {
      assert.equal(await page.locator("[data-composer-document]").innerText(), "current.txt\n×");
      assert.equal(await page.locator("button.send").isEnabled(), true);
    } else {
      assert.equal(await page.locator("[data-composer-document]").count(), 0);
      assert.equal(await page.locator("button.send").isDisabled(), true);
    }
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`attachment isolation ${mode}=ok`);
  }
} finally {
  if (browser) await browser.close();
  server.kill();
}
