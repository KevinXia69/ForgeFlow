/** Actual Chrome clicks over synthetic HTTP. Not real ERP or verifier acceptance. */
import { chromium } from "playwright-core";
import { mkdir } from "node:fs/promises";
import { startRevisionFixture, CASE_ID } from "./revision-fixture.mjs";
import { candidateCase, wireCandidateFixture, IDS } from "./candidates-fixture.mjs";
const instance = await startRevisionFixture({ directory: process.env.FORGEFLOW_STATIC_APP });
const { fixture, url } = instance;
wireCandidateFixture(fixture);
const output = process.env.FORGEFLOW_SCREENSHOT_DIR ?? "/tmp/forgeflow-candidate-ui";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
const count = suffix => fixture.calls.filter(call => call.path.endsWith(suffix)).length;
let activePage;
try {
  for (const lang of process.env.FORGEFLOW_PUBLIC_UI ? ["en"] : ["zh", "en"]) {
    fixture.current = candidateCase(); fixture.calls = []; fixture.deployment = "local";
    fixture.detailOffline = false; fixture.offline = false; fixture.selectionMode = "normal";
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage(); const errors = [];
    activePage = page;
    page.setDefaultTimeout(10000);
    page.on("pageerror", error => errors.push(String(error)));
    await page.addInitScript(lang => localStorage.setItem("forgeflow.lang", lang), lang);
    const open = async () => {
      const target = `${url}/app/#case=${CASE_ID}`;
      if (page.url() === target) await page.reload(); else await page.goto(target);
      await page.locator("[data-candidate-comparison]").waitFor();
    };
    const select = index => page.locator(`[data-select-candidate="${IDS[index]}"]`);
    const details = index => page.locator(`[data-candidate-details="${IDS[index]}"] > summary`);
    const chosen = index => page.locator(`[data-candidate="${IDS[index]}"][data-candidate-selected="true"]`).waitFor();
    const ask = async text => {
      await page.locator("[data-ask-case]").click();
      await page.locator('[contenteditable="true"]').fill(text);
      const previousReplies = await page.locator(".msg.assistant").count();
      await page.locator(".send").click();
      await page.waitForFunction(count => document.querySelectorAll(".msg.assistant").length > count, previousReplies);
      const reply = page.locator(".msg.assistant").last(); await reply.waitFor();
      const answer = await reply.innerText();
      assert(await reply.locator(".katex").count() === 0, "currency became math markup");
      await page.locator(".case-chat-back").click();
      return answer;
    };
    await open();
    assert(await page.locator("[data-candidate]").count() === 2, "distinct candidates not shown");
    assert(await page.locator("[data-case-approve]").count() === 0, "unselected group offers approval");
    await details(0).click(); await details(1).click();
    assert(await page.locator("[data-candidate-details] .cost-breakdown:visible").count() === 2, "candidate details did not open");
    await details(0).click(); await details(1).click();
    const unselected = await ask(lang === "zh" ? "比较候选方案" : "Compare the candidates");
    assert(unselected.includes("USD 62.00") && unselected.includes("USD 50.00"), "Q&A lost comparison cost facts");
    assert(!unselected.includes(lang === "zh" ? "尚未生成方案" : "No plan has been prepared"), "prepared candidates became no plan");
    await page.locator("[data-candidate-comparison]").screenshot({ path: `${output}/${lang}-comparison.png` });
    await select(0).focus(); await page.keyboard.press("Enter"); await chosen(0);
    assert(count("/select") === 1 && count("/approve") === 0, "selection executed or repeated");
    await page.locator('textarea[id^="case-comment"]').fill("Note for the first candidate only");
    await select(1).click(); await chosen(1);
    assert(await page.locator('textarea[id^="case-comment"]').inputValue() === "", "old approval note survived candidate change");
    await page.locator("[data-case-approve]").click();
    const dialog = page.getByRole("dialog"); await dialog.waitFor();
    assert((await dialog.innerText()).includes(lang === "zh" ? "优先降低规划成本" : "Prefer lower planning cost"), "approval scope lacks selected objective");
    await page.keyboard.press("Escape");
    assert(count("/approve") === 0, "cancelled approval dispatched");

    fixture.selectionMode = "pending";
    await select(0).click(); await page.locator("[data-selection-pending]").waitFor();
    assert(await page.locator("[data-case-approve]").isDisabled(), "pending selection permits approval");
    assert(await select(0).isDisabled() && await select(1).isDisabled(), "pending selection permits another choice");
    const requested = fixture.calls.filter(call => call.path.endsWith("/select")).at(-1).body;
    const beforeReload = count("/select");
    await page.reload(); await page.locator("[data-selection-pending]").waitFor();
    assert(count("/select") === beforeReload, "reload repeated selection");
    const pendingAnswer = await ask(lang === "zh" ? "现在选定哪个方案" : "Which candidate is selected now?");
    assert(pendingAnswer.includes(lang === "zh" ? "上次确认" : "last confirmed"), "pending Q&A promoted the unconfirmed request: " + pendingAnswer);
    await page.locator("[data-resume-selection]").click(); await chosen(0);
    assert(JSON.stringify(fixture.calls.filter(call => call.path.endsWith("/select")).at(-1).body) === JSON.stringify(requested), "resume changed selection CAS request");
    assert(await page.locator("[data-selection-pending]").count() === 0, "resumed choice stayed pending");

    fixture.selectionMode = "complete_then_lost";
    await select(1).click(); await chosen(1);
    assert(await page.locator(".case-error").count() === 0, "confirmed response loss left a false error");
    fixture.selectionMode = "refresh_lost";
    await select(0).click(); await page.locator(".case-error").waitFor();
    assert(await page.locator("[data-case-approve]").isDisabled(), "failed reread left stale plan approvable");
    assert(await select(0).isDisabled(), "failed reread permits new selection");
    fixture.detailOffline = false;
    await page.reload(); await page.locator("[data-resume-selection]").click(); await chosen(0);
    fixture.selectionMode = "normal";
    await page.locator('textarea[id^="case-comment"]').fill("Approve only the selected quantity candidate");
    await page.locator("[data-case-approve]").click(); await page.getByRole("checkbox").check();
    await page.getByRole("dialog").getByRole("button", { name: lang === "zh" ? "批准并执行" : "Approve & execute", exact: true }).click();
    await page.locator("[data-execution-receipt]").waitFor();
    assert(count("/approve") === 1 && fixture.calls.find(call => call.path.endsWith("/approve")).body.plan_id === IDS[0], "approval did not bind chosen candidate");
    assert(await page.locator("[data-select-candidate]").count() === 0, "executed comparison still permits switching");
    await page.reload(); await page.locator("[data-execution-receipt]").waitFor();
    assert(count("/approve") === 1, "reload repeated approval");

    fixture.current = candidateCase({ selected: 1 }); fixture.selectionMode = "conflict"; await open();
    await select(0).click(); await page.locator("[data-version-status]").waitFor();
    assert((await page.locator("[data-version-status]").innerText()).includes(lang === "zh" ? "选择与当前版本冲突" : "Selection conflicts"), "selection conflict mislabeled as failed preparation");
    assert(await page.locator('[data-timeline-event="prepared"]').getAttribute("data-event-status") === "done", "selection conflict undid prepared candidates");
    assert(await page.locator('[data-timeline-event="decision"]').getAttribute("data-event-status") === "failed", "selection conflict did not mark decision failure");
    const conflictAnswer = await ask(lang === "zh" ? "执行结果是什么" : "What was the execution result?");
    assert(conflictAnswer.includes(lang === "zh" ? "不证明 ERP" : "does not establish"), "selection conflict invented no execution");
    assert(await page.locator("[data-case-approve]").isDisabled(), "conflicted choice can be approved");
    fixture.selectionMode = "normal";
    fixture.current = candidateCase(); await open();
    await page.locator("[data-reject-candidates]").click();
    await page.locator("#decision-reason").fill("Neither candidate fits this controlled review");
    await page.getByRole("dialog").locator("button.primary").click();
    await page.locator("[data-rejection-receipt]").waitFor();
    assert((await page.locator("[data-rejection-receipt]").innerText()).includes("Neither candidate"), "group rejection reason disappeared");
    assert(fixture.current.plan === null && count("/reject") === 1, "group rejection secretly selected a plan");

    fixture.current = candidateCase({ single: true }); await open();
    assert(await page.locator("[data-candidate]").count() === 1, "single candidate duplicated");
    assert(await page.locator('[data-candidate-attempt="duplicate"]').isVisible(), "duplicate attempt explanation missing");
    assert(await page.locator("[data-select-candidate]").count() === 0, "single candidate asks for artificial choice");
    fixture.current.comparison = { status: "unavailable", version: 1, reason: "comparison_unavailable" };
    fixture.current.decision = { ...fixture.current.decision, can_approve: false, can_reject: false, can_request_changes: false };
    await open(); assert(await page.locator("[data-case-approve]").isDisabled(), "missing comparison permits approval");

    fixture.current = candidateCase(); fixture.deployment = "review"; const beforeReview = fixture.calls.length;
    await open();
    assert(await select(0).isDisabled() && await select(1).isDisabled(), "review permits selection");
    assert(fixture.calls.length === beforeReview, "review reading mutated a case");
    fixture.deployment = "local"; await open();
    await page.setViewportSize({ width: 390, height: 844 });
    await details(0).focus(); await page.keyboard.press("Enter");
    assert(await page.locator(`[data-candidate-details="${IDS[0]}"]`).getAttribute("open") !== null, "keyboard did not open details");
    const overflow = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: window.innerWidth,
      elements: [...document.querySelectorAll("body *")].filter(element => element.getBoundingClientRect().right > window.innerWidth + 1)
        .slice(0, 15).map(element => ({ tag: element.tagName, class: element.className, text: element.textContent.slice(0, 70) })) }));
    assert(overflow.width <= overflow.viewport + 1, "390px page overflows: " + JSON.stringify(overflow));
    await page.locator("[data-candidate-comparison]").screenshot({ path: `${output}/${lang}-390px.png` });
    assert(errors.length === 0, errors.join("\n"));
    await context.close();
    console.log(`candidate_ui=${lang}: selection, recovery, exact approval, rejection, read-only, Q&A, keyboard and 390px passed; synthetic HTTP only`);
  }
} catch (error) {
  if (activePage && !activePage.isClosed()) await activePage.screenshot({ path: `${output}/failure.png`, fullPage: true });
  throw error;
} finally { await browser.close(); await instance.close(); }
