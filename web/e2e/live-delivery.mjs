/** Real DOM workflow; business/model POSTs originate only from UI interaction. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { chromium } from "playwright-core";
import { gotoView, assertStartPage } from "./lib/nav.mjs";
import { observeCaseStreams } from "./lib/live-observer.mjs";

assert.equal(process.env.FORGEFLOW_ALLOW_LIVE_TEST, "1", "explicit live QA authorization required");
for (const key of ["FORGEFLOW_URL", "FORGEFLOW_INVITE", "FORGEFLOW_SCREENSHOT_DIR", "FORGEFLOW_EXPECTED_ASSETS", "FORGEFLOW_TESTED_REVISION"]) {
  assert.ok(process.env[key], `Missing ${key}; no default live target is used`);
}
const BASE = new URL(process.env.FORGEFLOW_URL).origin;
const OUT = resolve(process.env.FORGEFLOW_SCREENSHOT_DIR);
const revision = process.env.FORGEFLOW_TESTED_REVISION;
assert.match(revision, /^[a-f0-9]{40}$/);
const assets = JSON.parse(await readFile(process.env.FORGEFLOW_EXPECTED_ASSETS, "utf8"));
assert.ok(assets["index.html"], "built index hash required");
for (const [path, hash] of Object.entries(assets)) {
  assert.ok(path && !path.startsWith("/") && !path.split("/").includes("..") && !path.includes("\\"));
  assert.match(hash, /^[a-f0-9]{64}$/);
}
process.umask(0o077);
await mkdir(OUT, { mode: 0o700 }); // Never overwrite a prior attempt, including failures.
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
const pick = (value, keys) => Object.fromEntries(keys.split(" ").map(key => [key, value?.[key]]));
const report = { schema: 1, tested_revision: revision, started_at: new Date().toISOString(), passed: false,
  driver_sha256: sha(await readFile(new URL(import.meta.url))), stages: [], checkpoints: {}, screenshots: [], writes: [],
  observer_sha256: sha(await readFile(new URL("./lib/live-observer.mjs", import.meta.url))),
  interaction: "automated_chromium_real_dom_under_user_authorization",
  limitations: { public_deployment_changed: false, provider_billing_verified: false,
    raw_model_body_archived: false, human_usability_study: false, ci_run: false } };
let stage = "launch", browser, context, page;
const save = () => writeFile(`${OUT}/browser-acceptance.json`, JSON.stringify(report, null, 2) + "\n");
const mark = async name => { stage = name; report.stages.push({ name, at: new Date().toISOString() }); await save(); };
const checkpoint = async phase => {
  if (!process.env.FORGEFLOW_CHECKPOINT_SCRIPT) return;
  assert.ok(process.env.FORGEFLOW_CHECKPOINT_PYTHON && process.env.FORGEFLOW_STORAGE_STATE);
  // This file is private, and must never be included in the public evidence.
  await context.storageState({ path: process.env.FORGEFLOW_STORAGE_STATE });
  const result = spawnSync(process.env.FORGEFLOW_CHECKPOINT_PYTHON, [process.env.FORGEFLOW_CHECKPOINT_SCRIPT, phase],
    { env: process.env, encoding: "utf8", timeout: 120000 });
  assert.equal(result.status, 0, "read-only checkpoint failed; no business action retried");
  report.checkpoints[phase] = pick(JSON.parse(result.stdout),
    "phase table_count source_unchanged tables_unchanged_from_initial tables_unchanged_from_after_approval case_count case_ids");
  await save();
};
const screenshot = async (name, target = page) => {
  const bytes = await target.screenshot({ path: `${OUT}/${name}.png`, animations: "disabled",
    mask: [page.locator('#invite-code, [id^="case-approver-"]')] });
  report.screenshots.push({ name: `${name}.png`, bytes: bytes.length, sha256: sha(bytes) }); await save();
};
const read = async path => { const response = await page.request.get(BASE + path); assert.equal(response.status(), 200); return response.json(); };
const samePlan = (record, original) => {
  assert.equal(record.case_id, original.case_id); assert.equal(record.plan?.plan_id, original.plan.plan_id);
  assert.equal(record.revision?.version, original.revision.version);
};
const responseTo = path => page.waitForResponse(r => new URL(r.url()).pathname === path && r.request().method() === "POST", { timeout: 300000 });
try {
  browser = await chromium.launch({ channel: "chrome", headless: true });
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2, locale: "zh-CN" });
  // Observe a cloned response: the product cancels its reader after final, so
  // Chromium may no longer expose response.text() through the debugging API.
  // No request, response bytes, route or product state is changed by this observer.
  await context.addInitScript(observeCaseStreams);
  page = await context.newPage();
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.startsWith("/api/product/cases")) report.writes.push({ path, at: new Date().toISOString() });
  });
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.name));
  await mark("verify_served_assets"); report.assets = {};
  for (const [path, expected] of Object.entries(assets)) {
    const response = await page.request.get(`${BASE}/app/${path === "index.html" ? "" : path}`);
    assert.equal(response.status(), 200, "frozen asset unavailable");
    const actual = sha(await response.body()); assert.equal(actual, expected, "served frontend differs from frozen build");
    report.assets[path] = actual;
  }
  await mark("login");
  await page.goto(BASE + "/app/", { waitUntil: "domcontentloaded" });
  await page.locator("#language-select").click();
  await page.getByRole("option", { name: "简体中文", exact: true }).click();
  await page.locator("#invite-code").fill(process.env.FORGEFLOW_INVITE);
  await page.getByRole("button", { name: "进入工作台", exact: true }).click();
  await page.locator(".sidebar").waitFor({ timeout: 15000 });
  await assertStartPage(page, (value, message) => assert.ok(value, message));
  assert.deepEqual(await read("/api/product/cases"), [], "fresh QA case store required");
  const caps = await read("/api/capabilities");
  assert.equal(caps.environment?.execution, "live_sandbox"); assert.equal(caps.live_execute, true);
  await checkpoint("initial"); await screenshot("01-start");

  await mark("prepare_in_browser"); await gotoView(page, "cases");
  assert.equal(await page.locator("[data-cases-view]").getAttribute("data-cases-mode"), "live");
  const preparedResponse = responseTo("/api/product/cases");
  await page.locator("[data-create-case]").click();
  const preparedHttp = await preparedResponse; assert.equal(preparedHttp.status(), 201);
  const prepared = await preparedHttp.json();
  report.case_id = prepared.case_id; report.plan_id = prepared.plan?.plan_id; report.version = prepared.revision?.version;
  assert.equal(prepared.state, "awaiting_approval"); assert.equal(prepared.approval, null); assert.equal(prepared.execution, null);
  assert.match(report.case_id, /^[a-f0-9]{32}$/); assert.match(report.plan_id, /^[a-f0-9]{64}$/);
  await page.locator('[data-case-approve]:not([disabled])').waitFor({ timeout: 300000 });
  await checkpoint("after-prepare"); await screenshot("02-proposed-changes", page.locator("[data-case-changes]"));

  await mark("model_question_in_browser"); await page.locator("[data-ask-case]").click();
  const question = "方案要取消和新建的采购数量各是多少？";
  await page.locator("[data-composer-input]").fill(question);
  const answerResponse = responseTo(`/api/product/cases/${prepared.case_id}/ask/stream`);
  await page.locator("button.send").click();
  const answerHttp = await answerResponse; assert.equal(answerHttp.status(), 200);
  await page.waitForFunction(() => Boolean(window.__forgeflowAcceptanceAnswer), null, { timeout: 180000 });
  const observed = await page.evaluate(() => window.__forgeflowAcceptanceAnswer);
  assert.ok(!observed.capture_failed, "SSE observation failed; do not resend question");
  assert.equal(observed.path, `/api/product/cases/${prepared.case_id}/ask/stream`);
  assert.equal(observed.errors, 0);
  const answer = observed.final;
  report.answer = { question, ...pick(answer, "answer mode source_mode case_context fallback_reason") }; await save();
  assert.equal(answer?.mode, "harness"); assert.equal(answer?.source_mode, "model");
  assert.equal(answer.case_context?.case_id, prepared.case_id); assert.equal(answer.case_context?.plan_id, prepared.plan.plan_id);
  assert.equal(answer.case_context?.version, prepared.revision.version); assert.ok(answer.answer?.trim());
  await page.locator('[data-case-answer-source="model"]').waitFor({ timeout: 15000 });
  await screenshot("03-model-answer"); await page.locator(".case-chat-back").click();
  const beforeApproval = await read(`/api/product/cases/${prepared.case_id}`); samePlan(beforeApproval, prepared);
  assert.equal(beforeApproval.approval, null); assert.equal(beforeApproval.execution, null);
  await checkpoint("before-approval");

  await mark("approval_confirmation_in_browser"); await page.locator("[data-case-approve]").click();
  const dialog = page.getByRole("dialog"); await dialog.waitFor();
  const approve = dialog.locator("[data-confirm-approval]");
  assert.equal(await approve.isDisabled(), true, "acknowledgement must gate approval");
  const countBeforeConfirm = report.writes.length;
  await dialog.getByRole("checkbox").check(); assert.equal(await approve.isEnabled(), true);
  assert.equal(report.writes.length, countBeforeConfirm, "checking consent alone sent a request");
  await screenshot("04-approval-confirmation", dialog);
  const approvedResponse = responseTo(`/api/product/cases/${prepared.case_id}/approve`);
  report.approval_outcome = "pending_or_unknown"; await save(); await approve.click();
  const approvedHttp = await approvedResponse; assert.equal(approvedHttp.status(), 200);
  const executed = await approvedHttp.json(); report.approval_outcome = "response_received"; samePlan(executed, prepared);
  assert.equal(executed.approval?.plan_id, prepared.plan.plan_id);
  report.approval = pick(executed.approval, "plan_id issued_at");
  const execution = executed.execution;
  report.execution = { state: executed.state, status: execution?.status,
    writes: execution?.writes?.map(item => pick(item, "document kind quantity")),
    readback: pick(execution?.readback, "execution_validated verified_steps receipt_sha256 readback_sha256 verified_at"),
    verifier: pick(execution?.verifier, "status strict_success evaluation_valid evaluation_scope score passed_rule_count applicable_rule_count failed_rule_count reward_sha256") };
  await save(); assert.equal(executed.state, "succeeded"); assert.equal(execution?.readback?.execution_validated, true);
  assert.equal(execution?.verifier?.strict_success, true); assert.equal(execution?.verifier?.evaluation_valid, true);
  assert.equal(execution?.verifier?.evaluation_scope, "product_original_verifier");
  await page.locator('[data-execution-receipt][data-receipt-state="success"]').waitFor({ timeout: 300000 });
  await checkpoint("after-approval"); await screenshot("05-execution-receipt", page.locator("[data-execution-receipt]"));

  await mark("reload_and_restore"); const writeCount = report.writes.length;
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator('[data-execution-receipt][data-receipt-state="success"]').waitFor({ timeout: 20000 });
  const restored = await read(`/api/product/cases/${prepared.case_id}`); samePlan(restored, prepared);
  assert.deepEqual(restored.execution, executed.execution);
  await page.locator("[data-ask-case]").click();
  await page.locator('[data-case-answer-source="model"]').waitFor({ timeout: 15000 });
  await page.locator('[data-case-answer-context="earlier"]').waitFor({ timeout: 15000 });
  assert.equal(await page.locator(".msg.user").count(), 1); assert.equal(await page.locator(".msg.assistant").count(), 1);
  assert.equal(report.writes.length, writeCount, "refresh/restoration submitted another model or business request");
  await checkpoint("after-reload"); await screenshot("06-restored-answer");
  assert.deepEqual(report.writes.map(item => item.path), ["/api/product/cases", `/api/product/cases/${prepared.case_id}/ask/stream`, `/api/product/cases/${prepared.case_id}/approve`]);
  assert.deepEqual(pageErrors, [], "browser runtime error observed");
  report.reload = { same_execution: true, original_answer_restored: true, earlier_answer_label_visible: true, new_post_requests: 0 };
  report.passed = true;
  console.log(JSON.stringify({ passed: true, case_id: report.case_id, rules_passed: execution.verifier.passed_rule_count, screenshots: report.screenshots.length }));
} catch (error) {
  report.failure = { stage, error_type: error.name };
  // Debug traces may contain local paths. This private file is never published.
  await writeFile(`${OUT}/failure-private.txt`, String(error.stack ?? error));
  if (page) await screenshot("failure").catch(() => {});
  console.error(JSON.stringify({ passed: false, stage, error_type: error.name, evidence_preserved: true })); process.exitCode = 1;
} finally {
  if (context && process.env.FORGEFLOW_STORAGE_STATE) await context.storageState({ path: process.env.FORGEFLOW_STORAGE_STATE }).catch(() => {});
  report.finished_at = new Date().toISOString(); await save(); await context?.close(); await browser?.close();
}
