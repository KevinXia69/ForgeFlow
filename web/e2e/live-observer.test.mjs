import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";
import { chromium } from "playwright-core";
import { observeCaseStreams } from "./lib/live-observer.mjs";

test("final survives product reader cancellation without another request or reasoning export", async () => {
  const path = `/api/product/cases/${"a".repeat(32)}/ask/stream`;
  let requests = 0;
  const server = createServer((request, response) => {
    if (request.url !== path) { response.end("<!doctype html><title>Observer fixture</title>"); return; }
    requests++;
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write('data: {"type":"activity","reasoning":"private-trace-canary"}\n\n');
    setTimeout(() => response.end('data: {"type":"final","answer":"Public answer","mode":"harness","source_mode":"model","case_context":{"version":1}}\n\n'), 20);
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  try {
    const context = await browser.newContext(); await context.addInitScript(observeCaseStreams);
    const page = await context.newPage(); await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async path => {
      const response = await fetch(path, { method: "POST" });
      const reader = response.body.getReader(); let text = "";
      while (!text.includes('"type":"final"')) {
        const part = await reader.read(); if (part.done) throw new Error("missing final");
        text += new TextDecoder().decode(part.value);
      }
      await reader.cancel();
    }, path);
    await page.waitForFunction(() => Boolean(window.__forgeflowAcceptanceAnswer));
    const observed = await page.evaluate(() => window.__forgeflowAcceptanceAnswer);
    assert.equal(observed.final.answer, "Public answer"); assert.equal(observed.errors, 0);
    assert.equal(observed.path, path); assert.equal(requests, 1);
    assert.ok(!JSON.stringify(observed).includes("private-trace-canary"));
  } finally { await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});
