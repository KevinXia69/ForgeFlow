/** Real browser clicks over controlled HTTP responses; not real ERP acceptance. */
import { chromium } from "playwright-core";
import { mkdir } from "node:fs/promises";
import { startRevisionFixture, initialCase, CASE_ID } from "./revision-fixture.mjs";
const instance = await startRevisionFixture({ directory: process.env.FORGEFLOW_STATIC_APP });
const { fixture, url } = instance;
const output = process.env.FORGEFLOW_SCREENSHOT_DIR ?? "/tmp/forgeflow-revision-ui";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
const count = suffix => fixture.calls.filter(call => call.path.endsWith(suffix)).length;
try {
  for (const lang of process.env.FORGEFLOW_PUBLIC_UI ? ["en"] : ["zh", "en"]) {
    fixture.current = initialCase(); fixture.calls = []; fixture.deployment = "local"; fixture.detailOffline = false; fixture.completeBeforeLost = false; fixture.failRevision = true;
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage(); const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.addInitScript(lang => localStorage.setItem("forgeflow.lang", lang), lang);
    await page.goto(url + "/app/#cases");
    await page.locator(`[data-case-id="${CASE_ID}"]`).click();
    await page.locator("[data-case-request-changes]").click();
    const dialog = page.getByRole("dialog");
    await dialog.locator("[data-original-margin]").getByText(/20%/).waitFor();
    await dialog.locator("[data-effective-margin]").getByText(/20%/).waitFor();
    const submit = dialog.locator("button.primary");
    assert(await submit.isDisabled(), "empty reason enabled revision");
    await page.locator("#decision-reason").fill("Tighten planning margin");
    for (const invalid of ["20", "100", "99.999", "", "-1"]) {
      await page.locator("#revision-margin").fill(invalid);
      assert(await submit.isDisabled(), `invalid margin enabled submit: ${invalid}`);
    }
    await page.locator("#revision-margin").fill("90");
    await submit.click();
    await dialog.waitFor({ state: "detached" });
    await page.locator("[data-continue-version]").waitFor();
    assert(fixture.revisionBody.changes.minimum_margin_pct === 90 && count("/revision") === 1, "wrong revision input/count");
    for (const control of ["approve", "reject", "request-changes"]) assert(await page.locator(`[data-case-${control}]`).isDisabled(), "pending revision allowed " + control);
    assert((await page.locator("[data-case-changes] h2").innerText()).includes(lang === "zh" ? "修改前" : "Earlier plan"), "old plan unlabeled");
    assert(await page.locator('[data-timeline-event="prepared"]').getAttribute("data-event-status") === "pending", "pending revision falsely showed prepared");
    assert(!(await page.locator("[data-decision-panel]").innerText()).includes("revision_requested"), "internal state leaked into decision panel");
    assert(await page.locator("[data-displayed-plan]").isVisible(), "old metrics lack version label");
    await page.screenshot({ path: `${output}/${lang}-revision-pending.png`, animations: "disabled" });
    await page.locator("[data-ask-case]").click();
    await page.locator(".chat-preset").first().click();
    await page.locator(".msg.assistant").last().waitFor();
    assert((await page.locator(".msg.assistant").last().innerText()).includes(lang === "zh" ? "修改前" : "earlier version"), "offline Q&A promoted old plan to new version");
    await page.locator(".case-chat-back").click();
    await page.reload(); await page.locator("[data-continue-version]").waitFor();
    assert(count("/prepare") === 0, "reload automatically prepared a version");
    await page.locator("[data-continue-version]").click();
    await page.locator("[data-reprepare-case]").waitFor();
    assert(fixture.current.revision.version === 2 && fixture.current.plan === null, "failed preparation invented a plan");
    const failedFixture = structuredClone(fixture.current);
    await page.locator("[data-version-history] summary").click();
    const history = await page.locator("[data-version-history]").innerText();
    assert(history.includes(lang === "zh" ? "已退役" : "Retired") && history.includes(lang === "zh" ? "准备失败" : "Preparation failed"), "history confuses failure and retirement");
    await page.locator("[data-reprepare-case]").click();
    await dialog.locator("[data-reprepare-summary]").waitFor();
    assert(await dialog.locator("#revision-margin").count() === 0, "reprepare exposed constraint replacement");
    assert(await dialog.locator("button.primary").isDisabled(), "empty new-version reason enabled submission");
    await page.keyboard.press("Escape");
    assert(count("/reprepare") === 0, "cancel prepared a version");
    await page.locator("[data-reprepare-case]").click();
    await page.locator("#decision-reason").fill("Use current facts and retain the 90% floor");
    await dialog.locator("button.primary").click();
    await dialog.waitFor({ state: "detached" });
    await page.locator("[data-continue-version]").waitFor();
    assert(fixture.reprepareBody.failed_version === 2 && Object.keys(fixture.reprepareBody).sort().join() === "failed_version,reason", "new-version request changed scope");
    await page.reload(); await page.locator("[data-continue-version]").waitFor();
    assert(count("/prepare") === 1 && count("/reprepare") === 1, "pending reload resubmitted work");
    await page.screenshot({ path: `${output}/${lang}-reprepare-pending.png`, animations: "disabled" });
    await page.locator("[data-ask-case]").click();
    await page.locator('[contenteditable="true"]').fill(lang === "zh" ? "现在是哪一个版本？" : "Which version is being prepared now?");
    await page.locator(".send").click();
    await page.locator(".msg.assistant").last().getByText(lang === "zh" ? /失败记录仍保留/ : /failure remains recorded/).waitFor();
    await page.locator(".case-chat-back").click();
    await page.locator("[data-continue-version]").click();
    await page.locator("[data-case-approve]:enabled").waitFor();
    assert(fixture.current.revision.version === 3 && fixture.current.plan.plan_id === "c".repeat(64), "wrong active version");
    assert(count("/prepare") === 2 && count("/approve") === 0, "new plan implicitly approved or re-prepared");
    await page.locator("[data-case-request-changes]").click();
    assert((await dialog.locator("[data-original-margin]").innerText()).includes("20%"), "original floor was overwritten");
    assert((await dialog.locator("[data-effective-margin]").innerText()).includes("90%"), "effective floor was lost");
    await page.keyboard.press("Escape");
    await page.screenshot({ path: `${output}/${lang}-new-version.png`, animations: "disabled" });
    console.log(`revision_ui=${lang}:version recovery and constraint controls passed`);

    // Lost mutation + failed refresh must block all decisions on a stale card.
    fixture.current = initialCase(); await page.reload();
    await page.locator("[data-case-request-changes]").click();
    await page.locator("#revision-margin").fill("90"); await page.locator("#decision-reason").fill("Lost response control");
    fixture.detailOffline = true;
    await dialog.locator("button.primary").click();
    await page.locator(".case-error").waitFor();
    await dialog.waitFor({ state: "detached" });
    assert(await page.locator("[data-case-approve]").isDisabled(), "failed refresh left old plan approvable");
    fixture.detailOffline = false;

    // Review can show pending evidence but never continue it.
    fixture.deployment = "review"; const beforeReview = fixture.calls.length;
    await page.reload(); await page.locator("[data-continue-version]").waitFor();
    assert(await page.locator("[data-continue-version]").isDisabled(), "review enabled continuation");
    assert(fixture.calls.length === beforeReview, "review loaded with a mutation");
    fixture.deployment = "local"; fixture.current = initialCase();
    delete fixture.current.revision_limits; fixture.current.plan.checks = [];
    await page.reload(); await page.locator("[data-case-request-changes]").waitFor();
    assert(await page.locator("[data-case-request-changes]").isDisabled(), "invented fallback margin enabled revision");
    // A lost success response must not leave a false preparation error.
    fixture.current = initialCase(); fixture.completeBeforeLost = true; fixture.failRevision = false;
    await page.reload(); await page.locator("[data-case-request-changes]").click();
    await page.locator("#revision-margin").fill("90"); await page.locator("#decision-reason").fill("Recover completed revision");
    await dialog.locator("button.primary").click(); await dialog.waitFor({ state: "detached" });
    await page.locator("[data-case-approve]:enabled").waitFor();
    assert(fixture.current.revision.version === 2 && await page.locator(".case-error").count() === 0, "completed revision retained false failure");
    fixture.current = structuredClone(failedFixture); await page.reload();
    await page.locator("[data-reprepare-case]").click();
    await page.locator("#decision-reason").fill("Recover completed later version");
    await dialog.locator("button.primary").click(); await dialog.waitFor({ state: "detached" });
    await page.locator("[data-case-approve]:enabled").waitFor();
    assert(fixture.current.revision.version === 3 && await page.locator(".case-error").count() === 0, "completed later version retained false failure");
    assert(errors.length === 0, errors.join("\n"));
    await context.close();
    console.log(`revision_ui=${lang}:passed; real browser, mock HTTP, no ERP/model/approval`);
  }
} finally { await browser.close(); await instance.close(); }
