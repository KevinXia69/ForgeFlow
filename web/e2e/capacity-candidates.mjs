/** Actual browser over invented HTTP: capacity profile, never ERP/verifier evidence. */
import { chromium } from "playwright-core";
import { mkdir } from "node:fs/promises";
import { startRevisionFixture, CASE_ID } from "./revision-fixture.mjs";
import { candidateCase, wireCandidateFixture, IDS } from "./candidates-fixture.mjs";
const instance = await startRevisionFixture({ directory: process.env.FORGEFLOW_STATIC_APP });
const { fixture, url } = instance;
wireCandidateFixture(fixture);
const output = process.env.FORGEFLOW_SCREENSHOT_DIR ?? "/tmp/forgeflow-capacity-ui";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
let page;
try {
  for (const lang of process.env.FORGEFLOW_PUBLIC_UI ? ["en"] : ["zh", "en"]) {
    fixture.current = candidateCase({ capacity: true }); fixture.calls = []; fixture.deployment = "local";
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
    page = await context.newPage(); page.setDefaultTimeout(10000); const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.addInitScript(lang => localStorage.setItem("forgeflow.lang", lang), lang);
    await page.goto(`${url}/app/#case=${CASE_ID}`);
    await page.locator("[data-candidate-comparison]").waitFor();
    assert(await page.locator("[data-candidate]").count() === 3, "third candidate missing");
    assert(await page.locator(`[data-standard-minutes="${IDS[2]}"]`).innerText() === "10", "capacity metric missing");
    assert(await page.locator(`[data-standard-minutes="${IDS[0]}"]`).innerText() === "30", "quantity metric missing");
    assert(await page.locator("[data-case-approve]").count() === 0, "unselected approval enabled");
    await page.locator(`[data-select-candidate="${IDS[2]}"]`).focus(); await page.keyboard.press("Enter");
    await page.locator(`[data-candidate="${IDS[2]}"][data-candidate-selected="true"]`).waitFor();
    await page.locator("[data-case-approve]").click();
    const dialog = page.getByRole("dialog"); await dialog.waitFor();
    assert((await dialog.innerText()).includes(lang === "zh" ? "优先保留产能" : "Preserve manufacturing capacity"), "approval scope wrong");
    await page.keyboard.press("Escape");
    assert(fixture.calls.filter(call => call.path.endsWith("/approve")).length === 0, "canceled approval executed");
    await page.reload(); await page.locator(`[data-candidate="${IDS[2]}"][data-candidate-selected="true"]`).waitFor();
    await page.locator("[data-ask-case]").click();
    await page.locator('[contenteditable="true"]').fill(lang === "zh" ? "比较候选方案" : "Compare the candidates");
    await page.locator(".send").click();
    await page.waitForFunction(() => document.querySelectorAll(".msg.assistant").length > 0);
    const reply = await page.locator(".msg.assistant").last().innerText();
    assert(reply.includes("90.00") && reply.includes(lang === "zh" ? "标准产线占用" : "Standard workcenter load"), "fallback Q&A lost capacity");
    await page.locator(".case-chat-back").click();
    await page.locator("[data-candidate-comparison]").screenshot({ path: `${output}/${lang}-capacity.png` });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator(`[data-candidate-details="${IDS[2]}"] > summary`).click();
    const size = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
    assert(size.width <= size.viewport + 1, "narrow overflow: " + JSON.stringify(size));
    await page.locator("[data-candidate-comparison]").screenshot({ path: `${output}/${lang}-capacity-390px.png` });
    fixture.deployment = "review"; await page.reload();
    await page.locator("[data-candidate-comparison]").waitFor();
    for (const button of await page.locator("[data-select-candidate]").all()) assert(await button.isDisabled(), "review permits selection");
    assert(!errors.length, "browser error: " + errors.join());
    assert(fixture.calls.filter(call => call.path.endsWith("/select")).length === 1, "duplicate selection");
    await context.close();
    console.log(`capacity_ui=${lang}: three choices, measurements, keyboard selection, exact approval cancellation, reload, fallback Q&A, 390px, read-only pass; invented HTTP only`);
  }
} catch (error) {
  if (page && !page.isClosed()) await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  throw error;
} finally { await browser.close(); await instance.close(); }
