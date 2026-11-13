/** Browser attachment contract. Synthetic API; no provider or ERP calls. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const port = Number(process.env.FORGEFLOW_BROWSER_PORT || 4187);
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const appPath = process.env.FORGEFLOW_APP_PATH || "/app/";
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
let browser;
const payload = Buffer.from("FORGEFLOW-BROWSER-ATTACHMENT-ISOLATION");
const ref = { digest: createHash("sha256").update(payload).digest("hex"), name: "brief.txt", media_type: "text/plain", size: payload.length };
const model = { profile: "custom", base_url: "https://model.invalid/v1", model: "synthetic", api_key: "synthetic-test-key" };

try {
  for (let attempt = 0; ; attempt++) {
    try { await fetch(`http://127.0.0.1:${port}${appPath}`); break; }
    catch (error) { if (attempt >= 50) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
  }
  browser = await chromium.launch({ channel: "chrome", headless: true });
  for (const mode of ["stream", "fallback", "byok"]) {
    const context = await browser.newContext();
    await context.addInitScript(({ mode, model }) => {
      localStorage.setItem("forgeflow.lang", "en");
      if (mode === "byok") localStorage.setItem("forgeflow.model.attachment-owner", JSON.stringify(model));
    }, { mode, model });
    const page = await context.newPage();
    const errors = [], requests = [];
    let uploads = 0, companyUploads = 0;
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === "/api/auth/me") return route.fulfill({ json: { scope_id: "attachment-owner", display_name: "Synthetic reviewer", deployment: "local" } });
      if (path === "/api/config") return route.fulfill({ json: { deployment: "local", runner_enabled: false } });
      if (path === "/api/settings/model") return route.fulfill({ json: { configured: false } });
      if (["/api/runs", "/api/workspaces", "/api/odoo20/tasks"].includes(path)) return route.fulfill({ json: [] });
      if (path === "/api/attachments") {
        assert.equal(request.headers()["x-forgeflow-workspace"], "attachment-owner");
        assert.ok(request.postDataBuffer().includes(payload), "upload omitted the selected file");
        uploads++;
        return route.fulfill({ json: ref });
      }
      if (path === "/api/knowledge/documents") { companyUploads++; return route.fulfill({ status: 500, json: {} }); }
      if (path === "/api/assistant/stream" || path === "/api/assistant/answer") {
        const body = request.postDataJSON();
        requests.push({ path, body });
        assert.deepEqual(body.attachments, [ref], "model request lost the uploaded reference");
        assert.ok(body.question.trim(), "document-only send needs its default question");
        if (mode === "byok") assert.deepEqual(body.model, model);
        else assert.equal(body.model, undefined);
        if (mode === "fallback" && path.endsWith("/stream")) return route.fulfill({ status: 503, json: { detail: "stream_unavailable" } });
        const answer = { found: true, mode: mode === "stream" ? "harness" : "model", answer: `Attachment accepted: ${mode}`, citations: [], passages: [] };
        if (path.endsWith("/stream")) return route.fulfill({ contentType: "text/event-stream", body: `id: 1\ndata: ${JSON.stringify({ type: "final", ...answer })}\n\n` });
        return route.fulfill({ json: answer });
      }
      return route.fulfill({ status: 404, json: { detail: "not_found" } });
    });
    await page.goto(`http://127.0.0.1:${port}${appPath}`);
    await page.locator("[data-home-view]").waitFor();
    assert.equal(await page.locator(".chat-preset").count(), 4);
    assert.equal(await page.locator("button.send").isDisabled(), true, "empty turn should remain disabled");
    await page.locator('.composer input[type="file"]').setInputFiles({ name: ref.name, mimeType: ref.media_type, buffer: payload });
    await page.getByRole("button", { name: "Use in this conversation", exact: true }).click();
    await page.locator("[data-composer-document]").waitFor();
    assert.equal(await page.locator("button.send").isEnabled(), true, "document-only turn cannot be sent");
    await page.locator("button.send").click();
    await page.getByText(`Attachment accepted: ${mode}`, { exact: true }).waitFor();
    assert.equal(uploads, 1);
    assert.equal(companyUploads, 0, "chat attachment was saved as company material");
    assert.equal(requests.length, mode === "fallback" ? 2 : 1);
    assert.equal(await page.locator("[data-composer-document]").count(), 0, "attachment was retained for an unrelated turn");
    const requestCount = requests.length;
    await page.reload();
    await page.getByText(`Attachment accepted: ${mode}`, { exact: true }).waitFor();
    assert.equal(requests.length, requestCount, "reload resent a completed attachment turn");
    await page.locator('[data-chat-new]').click();
    assert.equal(await page.locator("[data-composer-document]").count(), 0);
    assert.equal(await page.locator("button.send").isDisabled(), true);
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`attachments ${mode}=ok`);
  }
} finally {
  if (browser) await browser.close();
  server.kill();
}
