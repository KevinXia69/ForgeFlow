/** Isolated business-workbench behavior tests. Mock API only; never ERP evidence. */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { gotoView, assertStartPage } from "./lib/nav.mjs";
const port = 4179;
const root = process.env.FORGEFLOW_STATIC_DIR || fileURLToPath(new URL("../../src/forgeflow/api/static/", import.meta.url));
const server = spawn("python3", ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", root], { stdio: "ignore" });
await new Promise(resolve => setTimeout(resolve, 500));
const browser = await chromium.launch({ channel: "chrome", headless: true });
const assert = (value, message) => { if (!value) throw new Error(message); };
const until = async predicate => { for (let i = 0; i < 500; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 10)); } throw new Error('mock stream request did not arrive'); };
const id = "a".repeat(32), unknownId = "b".repeat(32), planId = "c".repeat(64);
const template = {
  case_id: id, scenario: "A", state: "awaiting_approval", synthetic: true,
  order: { ref: "SO-1..5", product: "Clean Room Sealed Fixture", units: 50, due_days: 8, customer: "Northwind Manufacturing" },
  disruption: { kind: "supplier_cancellation", vendor: "Apex Components" },
  plan: { plan_id: planId, actions: [{ kind: "cancel_po", document: "PO-8841", quantity: 50, summary: "Cancel disrupted supply" }], margin: null, on_time: null, new_spend: null, cost_delta: null, policy_sha256: "d".repeat(64), snapshot_sha256: "e".repeat(64), prepared_at: "2026-10-22T12:00:00Z", checks: [{ code: "budget", label: "Budget", status: "unknown", actual: null, threshold: null }] },
  revision_limits: { original_margin_pct: 27.5, effective_margin_pct: 27.5, max_exclusive_pct: 100, decimal_places: 2 },
  approval: null, execution: null, created_at: "2026-10-22T12:00:00Z", updated_at: "2026-10-22T12:00:00Z", reason: null,
  decision: { can_approve: true, reason: null, blocked_checks: [], unknown_checks: [], warnings: [], can_reject: true, can_request_changes: true, can_reconcile: false, revision_reason: null },
  result: { execution_status: null, readback_status: "unknown", verifier_status: "unknown", strict_success: null, evaluation_valid: null, verified: false, outcome: "awaiting_approval" },
};
let current = structuredClone(template), owner = "owner-A", offline = false, approvals = 0, rejected = 0, revisions = 0, creates = 0;
let unstartedCancellations = 0, cancellationRefused = false;
let preparationRequests = 0, preparationReply = "normal";

// Selects are listbox menus (no native popups), so a choice is two clicks:
// open the trigger by its label, then click the option by its text.
async function chooseOption(page, label, optionText) {
  await page.getByRole('button', { name: label, exact: true }).click();
  await page.getByRole('listbox').waitFor();
  await page.getByRole('option', { name: optionText, exact: false }).first().click();
  await page.getByRole('listbox').waitFor({ state: 'detached' });
}
async function openOptions(page, label) {
  await page.getByRole('button', { name: label, exact: true }).click();
  await page.getByRole('listbox').waitFor();
  const labels = await page.getByRole('option').allInnerTexts();
  await page.keyboard.press('Escape');
  await page.getByRole('listbox').waitFor({ state: 'detached' });
  return labels;
}

const questions = [];
let approvalGate = null, releaseApproval = null;
let streamMode = 'off', streamStarts = 0;
const streamTurns = new Map(), streamRequests = [], cancelledTurns = [], heldStreams = new Map();
let capabilityScenario = "A", detailOffline = false, qaOffline = false, approvalReply = "normal";
// Reproduce the safe API projection of a partial journal: six dispatches,
// five verified steps, no success receipt. Counts are not ERP write counts.
const partialHistoryPlans = [
  { plan_id: planId, state: 'unknown', journal_present: true, success: null, prepared: { step_count: 6 }, execution: { status: 'unknown', journal_status: 'recorded', dispatch_count: 6, verified_count: 5, pending_dispatch: true } },
  { plan_id: '8'.repeat(64), state: 'prepared', journal_present: true, success: null, prepared: { step_count: 6 } },
  { plan_id: '9'.repeat(64), state: 'prepared', journal_present: true, success: null, execution: { status: 'not_recorded', journal_status: 'plan_only', dispatch_count: 0, verified_count: 0, pending_dispatch: false } },
];
const runs = Array.from({ length: 105 }, (_, i) => ({ run_id: `history-${String(i).padStart(3, "0")}`, task: i < 5 ? "2283_medium_repair_plan_medium" : "2280_medium_repair_plan_medium", task_label: i < 5 ? "其他任务" : "供应商断供后的计划修复", started_at: i === 0 ? "2026-10-21T12:00:00Z" : "2026-10-22T12:00:00Z", agent: { name: i ? "pi" : "nop", category: i ? "agent_evaluation" : "environment_control" }, experiment: { model: "synthetic-test-model", budget_seconds: 10, contract_guidance: { profile: "none", sha256: null, recorded: false }, planner: { profile: "cp-sat-recovery-v1", label: "CP-SAT 变更集执行与回读" }, data_profile: { name: "planning-metadata-v1", label: "规划参数可见性变体" }, execution_guidance: { profile: i % 2 ? "none" : "recovery-workflow-v1", sha256: "a".repeat(64) }, security_profile: { name: i % 2 ? "restricted" : "default" } }, score: i === 3 ? 17.5 : i ? 98 : 100, strict_success: i === 0, evaluation_valid: true }));
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.addInitScript(() => { if (!localStorage.getItem("forgeflow.lang")) localStorage.setItem("forgeflow.lang", "zh"); });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON();
    if (path === "/api/auth/me") return route.fulfill({ json: { scope_id: owner, display_name: "Alex Chen", deployment: "local" } });
    if (path === "/api/config") return route.fulfill({ json: { deployment: "local", runner_enabled: true, dsh_web_url: "http://example.invalid/?token=never-render" } });
    if (path === "/api/settings/model") return route.fulfill({ json: { configured: false } });
    if (path === "/api/capabilities") return route.fulfill({ json: { product_scenarios: [{ id: capabilityScenario, task: capabilityScenario === "A" ? "2280_medium_repair_plan_medium" : "2299_hard_repair_plan_hard", can_create: !offline }], case_read: true, case_create: !offline, case_prepare: !offline, case_approve: !offline, product_approval: !offline, environment: { execution: "synthetic_executor", scenario: capabilityScenario, ready: !offline } } });
    if (path === "/api/product/cases" && route.request().method() === "POST") { creates++; assert(body.scenario === capabilityScenario, "created unsupported scenario"); return route.fulfill({ json: current }); }
    if (path === "/api/product/cases") return route.fulfill({ json: [current, { ...template, case_id: unknownId, order: { ...template.order, product: "Carport Solar Canopy Module", units: 228, customer: "Helios Retail", ref: "SO-2077..2088" }, disruption: { ...template.disruption, vendor: "Northwind Components" }, state: "succeeded", decision: { ...template.decision, can_approve: false }, execution: { status: "succeeded", writes: [], readback: null, verifier: { status: "passed", score: 100 } }, result: { ...template.result, execution_status: "succeeded", outcome: "verification_unknown" } }] });
    if (path === `/api/product/cases/${id}`) return detailOffline ? route.fulfill({ status: 503, json: { detail: "test_detail_offline" } }) : route.fulfill({ json: current });
    if (path.endsWith('/ask/cancel') || path === '/api/assistant/cancel') {
      cancelledTurns.push(body.turn_id);
      heldStreams.get(body.turn_id)?.();
      return route.fulfill({ status: 204 });
    }
    if (path.endsWith('/ask/stream') || path === '/api/assistant/stream') {
      if (streamMode === 'off') return route.fulfill({ status: 404, json: { detail: 'not_found' } });
      streamRequests.push({ ...body, lastId: route.request().headers()['last-event-id'], path });
      if (body.resume && !streamTurns.has(body.turn_id)) return route.fulfill({ status: 410, json: { detail: 'turn_unavailable' } });
      if (!body.resume && !streamTurns.has(body.turn_id)) { streamTurns.set(body.turn_id, body.question); streamStarts++; }
      if (streamMode === 'hold' && !body.resume) await new Promise(resolve => heldStreams.set(body.turn_id, resolve));
      if (streamMode === 'cut' && !body.resume) return route.fulfill({ contentType: 'text/event-stream', body: 'id: 1\ndata: {"type":"trace","activity":[]}\n\n' });
      const event = cancelledTurns.includes(body.turn_id) ? { type: 'error', code: 'turn_cancelled' } : { type: 'final', found: true, mode: 'harness', activity: [{ kind: 'read', label: 'Read synthetic evidence', status: 'done' }], answer: `Stream answer: ${streamTurns.get(body.turn_id)}`, citations: [], passages: [], evidence_refs: [{ kind: 'case', id, href: `/api/product/cases/${id}`, label: 'Current case' }] };
      return route.fulfill({ contentType: 'text/event-stream', body: `id: 2\ndata: ${JSON.stringify(event)}\n\n` }).catch(() => {});
    }
    if (path.endsWith("/ask")) { if (qaOffline) return route.fulfill({ status: 503, json: { detail: "test_qa_offline" } }); questions.push(body); return route.fulfill({ json: { found: true, answer: body.question === "Show case reference" ? `Reference: case ${id}` : `Recorded answer: ${body.question}`, citations: ["Case fact"], evidence_refs: [{ kind: "case", id, href: `/api/product/cases/${id}`, label: "Current case" }] } }); }
    if (path.endsWith("/prepare")) {
      preparationRequests++;
      if (preparationReply === "interrupted") {
        current = { ...current, state: "failed", reason: "product_preparation_interrupted", decision: { ...current.decision, preparation_pending: false, can_resume_preparation: false } };
      } else {
        current = { ...structuredClone(template), decision: { ...template.decision, preparation_pending: false, can_resume_preparation: false } };
      }
      if (preparationReply === "lost") return route.abort("failed");
      return route.fulfill({ json: current });
    }
    if (path.endsWith("/cancel-unstarted")) {
      unstartedCancellations++; assert(body.plan_id === current.plan.plan_id, "wrong plan closed");
      if (cancellationRefused) return route.fulfill({ status: 409, json: { detail: "product_dispatch_present" } });
      current = { ...current, state: "rejected", reason: "unstarted_execution_cancelled", execution: { status: "not_started", writes: [], readback: null, verifier: { status: "not_run" } }, decision: { ...current.decision, can_approve: false, can_reconcile: false, can_cancel_unstarted: false }, result: { ...current.result, outcome: "not_started", verified: false } };
      return route.fulfill({ json: current });
    }
    if (path.endsWith("/approve")) {
      if (approvalGate) await approvalGate;
      approvals++; assert(body.plan_id === current.plan.plan_id, "wrong plan approved");
      current = { ...current, state: "succeeded", updated_at: "2026-10-22T12:01:00Z", approval: { plan_id: current.plan.plan_id, approver_label: "Reviewer mock-a", comment: body.comment, issued_at: "2026-10-22T12:01:00Z", expires_at: "2026-10-22T12:16:00Z" }, execution: { status: "succeeded", writes: [], readback: { execution_validated: true, verified_steps: 7 }, verifier: { source: "synthetic_executor", status: "passed", score: 100, strict_success: true, evaluation_valid: true, checks: [] } }, decision: { ...template.decision, can_approve: false, can_reject: false, can_request_changes: false }, result: { execution_status: "succeeded", readback_status: "passed", verifier_status: "passed", strict_success: true, evaluation_valid: true, verified: false, simulation_passed: true, verification_source: "synthetic_executor", outcome: "simulation_passed" } };
      if (approvalReply !== "normal") { if (approvalReply === "lost_and_unreadable") detailOffline = true; return route.abort("failed"); }
      return route.fulfill({ json: current });
    }
    if (path.endsWith("/reject")) { rejected++; current = { ...current, state: "rejected", reason: "rejected", audit: [{ action: "rejected", comment: body.reason, actor_label: "Reviewer mock-a", at: "2026-10-22T12:02:00Z" }], decision: { ...current.decision, can_approve: false, can_reject: false, can_request_changes: false } }; return route.fulfill({ json: current }); }
    if (path.endsWith("/revision")) { assert(body.changes?.minimum_margin_pct > 27.5, "revision omitted supported structured constraint"); revisions++; current = { ...current, plan: { ...current.plan, plan_id: "f".repeat(64) }, revision: { version: 2, parent_plan_id: planId, reason: body.reason, changes: {} } }; return route.fulfill({ json: current }); }
    if (path.endsWith("/events")) return route.fulfill({ contentType: "text/event-stream", body: "event: end\ndata: {}\n\n" });
    if (path === "/api/runs") return route.fulfill({ json: runs });
    if (/\/api\/runs\/[^/]+$/.test(path)) {
      const failed = path.endsWith('history-001'), missing = path.endsWith('history-002'), partial = path.endsWith('history-003');
      const evaluation = partial ? { score: 17.5, strict_success: false, evaluation_valid: true } : missing ? { score: 100, strict_success: null, evaluation_valid: null, rule_results: null } : failed ? { score: 98.5, strict_success: false, evaluation_valid: true, passed_rule_count: 2, applicable_rule_count: 3, failed_rule_count: 1, rule_results: [
        { dimension: 'constraint', rule: 'demand_coverage', status: 'PASS', applicable: true },
        { dimension: 'constraint', rule: 'origin_traceability', status: 'FAIL', applicable: true },
        { dimension: 'constraint', rule: 'margin_floor', status: 'PASS', applicable: true },
        { dimension: 'optional', rule: 'alternate_route', status: 'NA', applicable: false },
      ] } : { score: 100, strict_success: true, evaluation_valid: true };
      return route.fulfill({ json: { run_id: path.split("/").pop(), task: { id: "2280_medium_repair_plan_medium", label: "供应商断供后的计划修复" }, experiment: runs.find(run => run.run_id === path.split("/").pop())?.experiment, state: "succeeded", evaluation, timing: {} } });
    }
    if (path === '/api/runs/history-003/plans') return route.fulfill({ json: partialHistoryPlans });
    if (path.startsWith('/api/runs/history-003/plans/') && !path.endsWith('/approvals')) return route.fulfill({ json: partialHistoryPlans.find(item => item.plan_id === path.split('/').pop()) });
    if (path.endsWith("/plans")) return route.fulfill({ json: [{ plan_id: planId, state: "completed", prepared: { step_count: 1 } }] });
    if (path.endsWith("/approvals")) return route.fulfill({ json: [{ status: "used" }] });
    if (path.includes("/plans/")) return route.fulfill({ json: { plan_id: planId, state: "completed", prepared: { step_count: 1 } } });
    return route.fulfill({ status: 404, json: { detail: "not_found" } });
  });
  await page.goto(`http://127.0.0.1:${port}/app/`);
  // The start page is the ask surface; cases keep their own route.
  await assertStartPage(page, assert);
  await page.locator('[data-home-card="decision"]').waitFor();
  await page.screenshot({ path: "/tmp/forgeflow-v12-home.png", fullPage: true });
  await gotoView(page, "cases");
  await page.locator("[data-case-row]").first().waitFor();
  await page.screenshot({ path: "/tmp/forgeflow-v11-cases.png", fullPage: true });
  assert(await page.locator("[data-first-run-welcome]").count() === 1, "first-run welcome missing");
  await page.locator("[data-first-run-welcome]").getByRole("button", { name: "知道了", exact: true }).click();
  assert(await page.locator("[data-first-run-welcome]").count() === 0, "welcome did not dismiss");
  assert(await page.locator("iframe").count() === 0, "native shell leaked into business UI");
  assert(await page.locator('[data-saved-view="verified"]').innerText() === "已验收 0" || (await page.locator('[data-saved-view="verified"]').innerText()).endsWith("0"), "execution success counted verified without strict proof");
  await page.locator(`[data-case-id="${id}"]`).click();
  await page.locator('[data-case-detail]').waitFor();
  await page.screenshot({ path: "/tmp/forgeflow-v11-detail.png", fullPage: true });
  assert(await page.locator("[data-case-decision-summary]").count() === 1, "case decision summary missing");
  assert(await page.locator("[data-case-meta-details][open]").count() === 0, "case metadata is open by default");
  const visibleCase = await page.locator("[data-case-detail]").innerText();
  assert(!/[a-f0-9]{32}/i.test(visibleCase), "raw case id is visible in the first screen");
  const tiny = await page.locator("[data-case-detail] *").evaluateAll(nodes => nodes.filter(node => {
    const style = getComputedStyle(node);
    return node.textContent?.trim() && !node.closest("[aria-hidden='true']")
      && style.display !== "none" && style.visibility !== "hidden" && parseFloat(style.fontSize) < 14;
  }).map(node => `${node.tagName}.${node.className}:${getComputedStyle(node).fontSize}`));
  assert(tiny.length === 0, `sub-14px case text: ${tiny.slice(0, 5).join(", ")}`);
  assert((await page.locator('[data-kpi="margin"] .kpi-value').innerText()) === "—", "missing margin invented");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("[data-export-receipt]").click(),
  ]);
  const receipt = await download.createReadStream();
  let receiptText = "";
  for await (const chunk of receipt) receiptText += chunk;
  if (!receiptText.includes("ForgeFlow business handling receipt") && !receiptText.includes("ForgeFlow 业务处理回执")) throw new Error("receipt download missing title");
  if (receiptText.includes("/Users/") || receiptText.includes("sk-")) throw new Error("receipt leaked local path or secret");
  await page.locator("[data-case-evidence-link]").click();
  await page.locator("[data-case-evidence]").waitFor();
  await page.waitForFunction(() => document.activeElement?.id === "case-evidence", null, { timeout: 5000 });
  await page.locator('[data-ask-case]').click();
  await page.locator('[data-case-chat]').waitFor();
  for (const question of ["What changed?", "Which orders are affected?"]) {
    const input = page.locator('[data-case-chat] [data-composer-input]');
    await input.fill(question); await page.locator('[data-case-chat] button.send').click();
    await page.getByText(`Recorded answer: ${question}`, { exact: true }).waitFor();
  }
  assert(questions.length === 2 && questions[0].conversation_id === questions[1].conversation_id, "case conversation was not preserved");
  await page.locator('.case-chat-back').click(); await page.locator('[data-ask-case]').click();
  await page.locator('.msg.assistant').nth(1).waitFor();
  assert(await page.locator('.msg.assistant').count() === 2, "history lost when returning");
  await page.reload(); await page.locator('[data-case-chat]').waitFor();
  assert(await page.locator('.msg.assistant').count() === 2, "history lost on reload");
  assert(questions.length === 2, "reload resent a model request");
  await page.locator('[data-case-chat] .chat-new').click();
  assert(await page.locator('.msg.assistant').count() === 0, "new conversation reused visible messages");
  // The conversation history is a list now, not a <select>. Select the saved
  // thread that is not the active one.
  await page.locator('.chat-thread:not(.is-active)').first().click();
  assert(await page.locator('.msg.assistant').count() === 2, "saved conversation not selectable");
  owner = "owner-B"; await page.reload(); await page.locator('[data-case-chat]').waitFor();
  assert(await page.locator('.msg.assistant').count() === 0, "cross-owner history leaked");
  owner = "owner-A"; await page.reload(); await page.locator('.case-chat-back').click();
  await page.locator('[data-case-approve]').click();
  await page.getByRole('dialog').waitFor(); assert(approvals === 0, "approval happened before confirmation");
  await page.getByRole('button', { name: '取消', exact: true }).last().click();
  assert(approvals === 0, "cancel submitted approval");
  await page.locator('textarea[id^="case-comment"]').fill('Verified mock approval note');
  await page.locator('[data-case-approve]').click();
  await page.getByRole('checkbox').check();
  approvalGate = new Promise(resolve => { releaseApproval = resolve; });
  await page.getByRole('dialog').getByRole('button', { name: '批准并执行', exact: true }).click();
  await page.locator('[data-activity-active="true"] .activity-log-line.is-active').first().waitFor();
  const pendingSteps = page.locator('[data-activity-active="true"] .activity-log-line');
  assert((await pendingSteps.nth(0).getAttribute('class')).includes('is-active'), 'unconfirmed approval marked done');
  assert((await pendingSteps.nth(1).getAttribute('class')).includes('is-pending'), 'apply shown active before approval confirmation');
  assert(approvals === 0, 'held approval unexpectedly completed');
  releaseApproval(); approvalGate = null;
  await page.locator('[data-execution-receipt][data-receipt-state="simulated"]').waitFor();
  assert(approvals === 1, "approval did not happen exactly once");
  assert((await page.locator("[data-receipt-readback]").innerText()) === "未记录明细 · 已核验 7 个步骤", "missing readback details became zero records");
  await page.reload();
  // The approval note is retained but stays inside the collapsed detail on a
  // completed case, so the business receipt carries no rehearsal wording.
  await page.locator('[data-execution-receipt]').waitFor();
  const noteCount = await page.getByText('Verified mock approval note', { exact: true }).count();
  assert(noteCount >= 1, 'approval note was not retained');
  for (let index = 0; index < noteCount; index += 1) {
    assert(!(await page.getByText('Verified mock approval note', { exact: true }).nth(index).isVisible()), 'approval note is exposed on the first screen');
  }
  await page.locator('details[data-technical-details] > summary').first().click();
  await page.getByText('Verified mock approval note', { exact: true }).first().waitFor();
  assert(await page.locator('[data-case-approve]').count() === 0, "completed case can still approve");
  await page.locator('[data-view-audit]').click(); await page.locator('[data-case-evidence]').waitFor();
  assert(!(await page.locator('[data-activity-timer]').first().innerText()).includes("·"), "missing audit timestamps invented execution duration");
  assert(page.url().includes(`case=${id}`), "case evidence lost case context");
  await gotoView(page, "runs");
  assert(await page.locator('tbody tr').count() === 15, "history table not paginated");
  await chooseOption(page, '实验类别', '环境对照');
  assert(await page.locator('tbody tr').count() === 1, 'control and agent experiments mixed');
  assert((await page.locator('[data-evidence-counts]').innerText()).includes('1 / 当前筛选 1'), 'filtered denominator incorrect');
  await page.getByRole('button', { name: '清除筛选', exact: true }).click();
  // index 2 of the menu: all, unknown, then the first recorded configuration
  await page.getByRole('button', { name: '记录配置', exact: true }).click();
  await page.getByRole('listbox').waitFor();
  await page.getByRole('option').nth(2).click();
  await page.getByRole('listbox').waitFor({ state: 'detached' });
  assert((await page.locator('[data-evidence-counts]').innerText()).includes('当前筛选 53'), 'nested planner configs collapsed');
  await page.getByRole('button', { name: '清除筛选', exact: true }).click();
  await page.getByRole('textbox', { name: '开始日期', exact: true }).fill('2026-10-22');
  assert((await page.locator('[data-evidence-counts]').innerText()).includes('当前筛选 104'), 'date filter incorrect');
  await page.getByRole('button', { name: '清除筛选', exact: true }).click();
  await page.locator('.run-link').first().click(); await page.locator('[data-evidence-detail]').waitFor();
  await page.locator('[data-evidence="panel"]').waitFor();
  assert(await page.locator('[data-runs-list]').count() === 0, "run detail still below entire list");
  assert(await page.getByRole('button', { name: /批准|New run|新建运行/ }).count() === 0, "history mutation exposed");
  await page.getByRole('button', { name: '返回运行列表', exact: true }).click();
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await page.getByRole('searchbox').fill('history-100');
  assert(await page.locator('tbody tr').count() === 1, "run search not applied");
  await gotoView(page, "cases");
  current = structuredClone(template); offline = true;
  await page.getByRole('button', { name: '重检连接', exact: true }).click();
  await page.locator('[data-case-row]').first().waitFor();
  await page.waitForFunction(() => document.querySelector('[data-cases-ready="true"]') && document.querySelector('[data-create-case]')?.disabled);
  assert(await page.locator('[data-create-case]').isDisabled(), "offline creation not disabled");
  assert(await page.locator('[data-case-row]').count() === 2, "offline history hidden by fixture");
  assert(creates === 0, "unexpected create request");
  offline = false;
  await page.getByRole('button', { name: '重检连接', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('[data-create-case]').disabled);
  await page.locator(`[data-case-id="${id}"]`).click();
  await page.locator('[data-case-request-changes]').click();
  await page.locator('#decision-reason').fill('Recheck the budget');
  await page.getByRole('button', { name: '提交决定', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'detached' }); assert(revisions === 1, "revision did not reach API");
  await page.locator('[data-case-reject]').click(); await page.locator('#decision-reason').fill('Rejected in isolated test');
  await page.getByRole('button', { name: '提交决定', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'detached' }); assert(rejected === 1, "reject did not reach API");
  assert(await page.locator('[data-case-approve]').isDisabled(), "rejected case allows approval");
  assert((await page.locator('[data-rejection-receipt]').innerText()).includes("Rejected in isolated test"), "rejection reason hidden");
  await page.getByRole('button', { name: '设置', exact: true }).click();
  const dialog = page.getByRole('dialog'); await dialog.waitFor();
  assert(await dialog.evaluate(node => node.contains(document.activeElement)), 'settings did not receive focus');
  for (let i = 0; i < 10; i++) { await page.keyboard.press('Tab'); assert(await dialog.evaluate(node => node.contains(document.activeElement)), 'focus escaped settings'); }
  await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'detached' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('[data-ask-case]').click(); await page.locator('[data-case-chat]').waitFor();
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  assert(width <= 392, `narrow layout overflows: ${width}`);
  await page.screenshot({ path: '/tmp/forgeflow-v9-mobile.png', animations: 'disabled' });
  current = { ...template, state: "failed", plan: null, approval: null, execution: null, audit: [], reason: "internal_error", decision: { ...template.decision, can_approve: false, can_reject: false, can_request_changes: false }, result: { ...template.result, outcome: "failed" } };
  await page.locator('.case-chat-back').click(); await page.reload(); await page.locator('[data-case-timeline]').waitFor();
  assert((await page.locator('[data-timeline-event="prepared"]').innerText()).includes('方案准备未完成'), 'prepare failure not identified');
  assert(!(await page.locator('[data-case-timeline]').innerText()).includes('计划已批准'), 'missing approval rendered as approved');
  assert((await page.locator('[data-timeline-event="executing"]').innerText()).includes('尚未执行'), 'prepare failure rendered as execution failure');
  assert(await page.locator('[data-timeline-event="decision"]').getAttribute('data-event-status') === 'pending', 'decision stage incorrectly completed');
  assert(await page.locator('[data-timeline-event="executing"]').getAttribute('data-event-status') === 'pending', 'execution stage incorrectly failed');
  // Recorded action kinds, rather than order category, determine all labels and confirmation counts.
  const purchaseActions = [
    ...Array.from({ length: 5 }, (_, i) => ({ kind: "update_po", document: `P0000${i + 1}`, summary: `Update purchase_order P0000${i + 1}`, quantity: 10 })),
    { kind: "cancel_po", document: "P00006", summary: "Cancel purchase order", quantity: 3 },
    { kind: "create_po", document: "new:purchase.order:0007", summary: "Create purchase order", quantity: 3 },
  ];
  current = { ...structuredClone(template), plan: { ...structuredClone(template.plan), actions: purchaseActions } };
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const locale of ["zh", "en"]) {
    await page.evaluate(value => localStorage.setItem("forgeflow.lang", value), locale);
    await page.reload(); await page.locator('[data-change-table]').waitFor();
    const labels = await page.locator('[data-change-table] .case-action-kind').allTextContents();
    const update = locale === "zh" ? "更新采购单" : "Update PO";
    const create = locale === "zh" ? "新建采购单" : "Create PO";
    const cancel = locale === "zh" ? "取消采购单" : "Cancel PO";
    assert(labels.filter(label => label === update).length === 5, `${locale}: updates mislabeled`);
    assert(labels.filter(label => label === create).length === 1, `${locale}: false create count`);
    assert(labels.filter(label => label === cancel).length === 1, `${locale}: cancel mislabeled`);
    assert(!(await page.locator('[data-change-table]').innerText()).includes('Apex Components'), 'cancellation invented supplier attribution');
    await page.locator('[data-case-approve]').click();
    const confirmation = page.getByRole('dialog');
    assert((await confirmation.innerText()).includes(`${update}: 5`), 'confirmation update count incorrect');
    assert((await confirmation.innerText()).includes(`${create}: 1`), 'confirmation create count incorrect');
    // The close icon shares the accessible name; exercise the visible footer action.
    await confirmation.getByRole('button', { name: locale === "zh" ? "取消" : "Cancel", exact: true }).filter({ hasText: locale === "zh" ? /^取消$/ : /^Cancel$/ }).click();
  }
  current.plan.actions = [
    { kind: "create_mo", document: "new:mrp.production:1", summary: "Create manufacturing order", quantity: 3 },
    { kind: "cancel_mo", document: "MO00001", summary: "Cancel manufacturing order", quantity: 3 },
    { kind: "update_mo", document: "MO00002", summary: "Update manufacturing order", quantity: 3 },
  ];
  await page.reload(); await page.locator('[data-change-table]').waitFor();
  assert((await page.locator('[data-change-table] .case-action-kind').allTextContents()).join('|') === 'Create MO|Cancel MO|Adjust MO', 'manufacturing actions collapsed');
  assert((await page.locator('[data-action-document="new:mrp.production:1"]').innerText()) === 'New MO 1', 'new manufacturing document mislabeled PO');
  current.plan.actions = [
    ...['0002', '0004', '0009'].map((token, index) => ({ kind: 'create_mo', document: `new:mrp.production:${token}`, summary: `Create manufacturing_order new:mrp.production:${token}`, quantity: 3, before: null, after: { quantity: 3, start_day: index + 1, origin_refs: [{ model: 'mrp.production', name: null, token: 'new:mrp.production:0002' }], workorder_plan: { duration_expected_minutes: 120, start_day: index + 1 }, public_standard: { time_per_unit_minutes: 40 } } })),
    { kind: 'cancel_mo', document: 'WH/MO/0001', summary: 'Cancel manufacturing_order WH/MO/0001', quantity: 3 },
    { kind: 'update_po', document: 'P00001', summary: 'Update purchase_order P00001', quantity: 10 },
  ];
  await page.reload(); await page.locator('[data-change-table]').waitFor();
  assert((await page.locator('[data-change-table] [data-action-document]').allTextContents()).slice(0, 3).join('|') === 'New MO 1|New MO 2|New MO 3', 'new MO labels do not distinguish planned records');
  assert(!(await page.locator('[data-change-table]').innerText()).includes('manufacturing_order') && !(await page.locator('[data-change-table]').innerText()).includes('new:mrp.production'), 'action summaries expose internal models or unassigned tokens');
  await page.locator('[data-plan-view="diff"]').click();
  assert(await page.locator('[data-change-diff] pre').count() === 0, 'diff remains a raw JSON wall');
  const firstSnapshot = page.locator('.change-diff-row').first();
  assert((await firstSnapshot.innerText()).includes('Start day') && !(await firstSnapshot.innerText()).includes('name assigned'), 'snapshot fields missing or nested references not folded');
  await firstSnapshot.getByText('Origin documents (1)', { exact: true }).click();
  assert((await firstSnapshot.innerText()).includes('New MO 1 (name assigned on execution)'), 'unassigned MO name represented as actual ERP name');
  assert(!(await firstSnapshot.innerText()).includes('null') && !(await firstSnapshot.innerText()).includes('new:mrp.production'), 'diff exposes null or internal placeholder');
  await page.locator('[data-plan-view="table"]').click();
  assert(approvals === 1, 'label checks accidentally approved');
  const createdDocuments = current.plan.actions.filter(action => action.kind === 'create_mo').map(action => ({ document: action.document, kind: action.kind, quantity: action.quantity, state: 'confirmed' }));
  current = { ...current, state: 'executed', execution: { status: 'succeeded', writes: createdDocuments, readback: { execution_validated: true, verified_steps: 3, records: [...createdDocuments, { document: 'WH/MO/RECORDED', kind: 'create_mo', state: 'confirmed' }, { document: 'new:mrp.production:0088', kind: 'create_mo', state: 'confirmed' }] }, verifier: { source: 'synthetic_executor' } }, decision: { ...current.decision, can_approve: false, can_reject: false, can_request_changes: false }, result: { ...current.result, verified: false, simulation_passed: true, outcome: 'simulation_passed' } };
  await page.reload(); await page.locator('[data-case-verification]').waitFor();
  // Execution records are one list with tabs: readback and writes must not be
  // rendered as two stacked copies of the same document batch.
  assert(await page.locator('[data-case-verification] .case-readback').count() === 1, 'execution records rendered as more than one list');
  const readbackDocuments = await page.locator('[data-case-verification] .case-readback .mono').allTextContents();
  assert(readbackDocuments.join('|') === 'New MO 1|New MO 2|New MO 3|WH/MO/RECORDED|New MO', 'readback document labels differ from the plan, replace actual names, or infer token numbers');
  await page.locator('[data-record-view-button="writes"]').click();
  const writeDocuments = await page.locator('[data-case-verification] .case-readback .mono').allTextContents();
  assert(writeDocuments.join('|') === 'New MO 1|New MO 2|New MO 3', 'write document labels differ from the plan, replace actual names, or infer token numbers');
  assert(await page.locator('[data-case-verification] .case-readback').count() === 1, 'switching the record view stacked a second list');
  await page.locator('[data-record-view-button="readback"]').click();
  current = { ...structuredClone(template), state: "execution_unknown", approval: { plan_id: planId, issued_at: "2026-10-22T12:01:00Z", approver_label: "Reviewer test" }, execution: { status: "unknown", writes: [], readback: null, verifier: {} }, decision: { ...template.decision, can_approve: false, can_reject: false, can_request_changes: false, can_reconcile: true }, result: { ...template.result, outcome: "execution_unknown" } };
  await page.reload(); await page.locator('[data-execution-receipt]').waitFor();
  assert((await page.locator('.case-state-caption').innerText()).includes('Execution outcome unknown'), 'unknown execution shown as detected');
  assert((await page.locator('[data-timeline-event="executing"]').innerText()).includes('writes may already have occurred'), 'timeline interpreted missing write receipts as zero');
  assert((await page.locator('[data-receipt-writes]').innerText()).includes('reconciliation'), 'receipt missing write uncertainty');
  assert(!(await page.locator('[data-case-detail]').innerText()).includes('Execution finished'), 'unknown execution declared finished');
  assert(!(await page.locator('[data-case-verification]').innerText()).includes('0 writes'), 'unknown writes incorrectly zero');
  assert(await page.locator('[data-case-approve]').count() === 0, 'unknown execution can be approved again');
  await page.getByRole('button', { name: 'Reconcile original execution', exact: true }).waitFor();
  runs[2].experiment.contract_guidance = { profile: 'reviewed-request-v1', sha256: 'f'.repeat(64), recorded: true };
  runs[4].experiment.contract_guidance = { profile: 'none', sha256: null, recorded: true };
  await gotoView(page, "runs");
  assert((await page.locator('.recent').first().innerText()).includes('Task 2283'), 'generic sidebar label hid task identity');
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
  const configLabels = await openOptions(page, 'Recorded configuration');
  assert(configLabels[2] !== configLabels[3] && configLabels[2].includes('guidance:') && configLabels[3].includes('guidance:'), 'configurations lack distinguishing factors');
  assert(configLabels.every(label => label.length <= 200), 'configuration options exceed compact budget');
  assert(configLabels.every(label => !/[a-f0-9]{32}/.test(label)), 'configuration option exposes long hashes');
  assert(configLabels.some(label => label.includes('contract: Reviewed request')) && configLabels.some(label => label.includes('contract: None')) && configLabels.some(label => label.includes('contract: Not recorded')), 'contract guidance lost recorded/none/reviewed distinctions');
  await chooseOption(page, 'Recorded configuration', 'contract: Reviewed request');
  assert(await page.locator('tbody tr').count() === 1 && (await page.locator('tbody').innerText()).includes('history-002'), 'reviewed contract factor merged with prior runs');
  await chooseOption(page, 'Recorded configuration', 'contract: None');
  assert(await page.locator('tbody tr').count() === 1 && (await page.locator('tbody').innerText()).includes('history-004'), 'explicit none merged with old unrecorded factor');
  await page.getByRole('button', { name: 'Clear filters', exact: true }).click();

  await page.locator('.run-link').first().click(); await page.locator('[data-evidence="panel"]').waitFor();
  assert(!(await page.locator('[data-evidence="panel"]').innerText()).match(/[\u4e00-\u9fff]/u), 'English profile display used untranslated backend label');
  capabilityScenario = 'B';
  current = { ...structuredClone(template), scenario: 'B', order: { ref: 'SO-1..12', product: 'Carport Solar Canopy Module', units: 228, due_days: 8, due_days_max: 12, customer: '12 confirmed sales orders' }, disruption: { kind: 'workcenter_outage', workcenter: 'Assembly Line 1' } };
  await gotoView(page, "cases");
  await page.getByRole('button', { name: 'Run production-line outage example', exact: true }).click();
  await page.locator('[data-case-detail]').waitFor();
  assert(creates === 1, 'B creation did not use supported scenario');
  assert((await page.locator('[data-case-facts]').innerText()).includes('Work center'), 'B workcenter labeled supplier');
  assert((await page.locator('[data-case-facts]').innerText()).includes('8–12 days'), 'B delivery window collapsed');
  assert((await page.locator('[data-case-detail] h1').innerText()).includes('Production-line outage'), 'B disruption shown as supply exception');
  assert((await page.locator('[data-timeline-event="detected"]').innerText()).includes('Production outage detected'), 'B timeline still called supply exception');
  await page.locator('[data-ask-case]').click(); await page.locator('[data-case-chat]').waitFor();
  const messagesBeforeRefresh = await page.locator('.msg').allTextContents();
  const requestsBeforeRefresh = questions.length;
  current = { ...current, state: 'executing', approval: { plan_id: current.plan.plan_id }, execution: null, decision: { ...current.decision, can_approve: false, can_reject: false, can_request_changes: false }, result: { ...current.result, outcome: 'executing' } };
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.locator('.case-chat-page-context').getByText('Executing', { exact: true }).waitFor();
  assert(JSON.stringify(await page.locator('.msg').allTextContents()) === JSON.stringify(messagesBeforeRefresh), 'detail refresh reset case conversation');
  assert(questions.length === requestsBeforeRefresh, 'detail refresh repeated a model question');
  // Freshness is published as an attribute so it is readable in every layout,
  // including the chat drawer where the case details block is not mounted.
  const lastConfirmed = (await page.locator('[data-case-read-at]').first().getAttribute('data-case-read-at')) ?? '';
  detailOffline = true;
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Showing the last retrieved state' }).waitFor();
  assert(((await page.locator('[data-case-read-at]').first().getAttribute('data-case-read-at')) ?? '') === lastConfirmed, 'failed refresh advanced last confirmed time');
  assert(await page.locator('[data-case-chat]').count() === 1, 'failed refresh removed case context');
  qaOffline = true;
  await page.locator('[data-case-chat] [data-composer-input]').fill('What happened during execution?');
  await page.locator('[data-case-chat] button.send').click();
  await page.locator('.msg.assistant').filter({ hasText: 'Execution is in progress' }).waitFor();
  const fallbackText = await page.locator('.msg.assistant').last().innerText();
  assert(!fallbackText.includes('No execution has occurred') && !fallbackText.includes('0 writes'), 'executing without receipt falsely declared unexecuted');
  await page.locator('.case-chat-back').click(); await page.locator('[data-case-timeline]').waitFor();
  assert((await page.locator('[data-timeline-event="executing"]').innerText()).includes('Execution is in progress'), 'running timeline falsely declared unexecuted');
  assert(approvals === 1, 'refresh or offline answer resubmitted approval');
  detailOffline = false;
  current = { ...current, state: 'preparing', plan: null, approval: null, execution: null, result: { ...current.result, outcome: 'preparing' } };
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.locator('[data-case-state="preparing"]').waitFor();
  await page.locator('[data-ask-case]').click();
  const noPlanConfirmed = (await page.locator('[data-case-read-at]').first().getAttribute('data-case-read-at')) ?? '';
  detailOffline = true;
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Showing the last retrieved state' }).waitFor();
  await page.locator('[data-case-chat] [data-composer-input]').fill('Which orders are affected?');
  await page.locator('[data-case-chat] button.send').click();
  await page.locator('.msg.assistant').filter({ hasText: '228 units of Carport Solar Canopy Module across 12 sales orders' }).waitFor();
  const noPlanFacts = await page.locator('.msg.assistant').last().innerText();
  assert(noPlanFacts.includes('8–12 days') && noPlanFacts.includes('Case Q&A is unavailable'), 'cached facts omitted delivery window or unavailable notice');
  assert(!noPlanFacts.includes('0 documents') && !noPlanFacts.includes('nothing to execute'), 'known impact facts replaced by missing plan');
  assert(((await page.locator('[data-case-read-at]').first().getAttribute('data-case-read-at')) ?? '') === noPlanConfirmed, 'offline answer advanced case observation time');
  await page.locator('[data-case-chat] [data-composer-input]').fill('What does the plan change?');
  await page.locator('[data-case-chat] button.send').click();
  await page.locator('.msg.assistant').last().getByText('No recovery plan has been prepared for this case, so there is nothing to execute.', { exact: true }).waitFor();
  await page.locator('.case-chat-back').click();
  detailOffline = false; qaOffline = false; capabilityScenario = 'A'; current = structuredClone(template);
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.waitForFunction(() => { const button = document.querySelector('[data-case-approve]'); return button && !button.disabled; });
  // Healthy capabilities/list reads cannot authorize writes from an unreadable, stale case.
  const staleCaseTime = (await page.locator('[data-case-read-at]').first().getAttribute('data-case-read-at')) ?? '';
  const staleCaseFacts = await page.locator('[data-case-facts]').innerText();
  const beforeStaleWrites = [approvals, rejected, revisions];
  current = { ...current, order: { ...current.order, customer: 'Updated customer after successful detail read' } };
  detailOffline = true;
  for (let attempt = 0; attempt < 2; attempt++) {
    await page.getByRole('button', { name: 'Check connection', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Write actions are paused' }).waitFor();
    await page.waitForFunction(() => !document.querySelector('[data-environment] button')?.disabled);
    assert(((await page.locator('[data-case-read-at]').first().getAttribute('data-case-read-at')) ?? '') === staleCaseTime, 'failed case read advanced confirmation time');
    assert((await page.locator('[data-case-facts]').innerText()) === staleCaseFacts, 'list response replaced the unreadable selected case');
    assert((await page.locator('[data-environment]').innerText()).includes('Demo mode'), 'case read failure incorrectly reported executor offline');
    for (const action of ['approve', 'request-changes', 'reject']) assert(await page.locator(`[data-case-${action}]`).isDisabled(), `stale case enabled ${action}`);
  }
  assert(JSON.stringify([approvals, rejected, revisions]) === JSON.stringify(beforeStaleWrites), 'failed case refresh sent a write');
  detailOffline = false;
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.locator('[data-case-facts]').getByText('Updated customer after successful detail read', { exact: true }).waitFor();
  for (const action of ['approve', 'request-changes', 'reject']) assert(await page.locator(`[data-case-${action}]`).isEnabled(), `successful case read did not restore ${action}`);
  assert(await page.getByRole('alert').filter({ hasText: 'Write actions are paused' }).count() === 0, 'case read success left stale-state warning');
  approvalReply = 'lost';
  await page.locator('[data-case-approve]').click(); await page.getByRole('checkbox').check();
  await page.getByRole('dialog').getByRole('button', { name: 'Approve & execute', exact: true }).click();
  await page.locator('[data-execution-receipt][data-receipt-state="simulated"]').waitFor();
  await page.getByText('The approval response was interrupted. The saved case state has been reloaded; consult its receipt before taking further action.', { exact: true }).waitFor();
  await page.locator('[data-view-audit]').click(); await page.locator('[data-activity="true"]').waitFor();
  assert(await page.locator('[data-activity="true"] .is-failed').count() === 0, 'stale network failure activity overrode saved success');
  assert(approvals === 2, 'lost response was resubmitted');
  current = structuredClone(template);
  await gotoView(page, "cases"); await page.locator(`[data-case-id="${id}"]`).click();
  await page.waitForFunction(() => !document.querySelector('[data-case-approve]')?.disabled);
  approvalReply = 'lost_and_unreadable';
  await page.locator('[data-case-approve]').click(); await page.getByRole('checkbox').check();
  await page.getByRole('dialog').getByRole('button', { name: 'Approve & execute', exact: true }).click();
  await page.getByText('The approval response and case refresh are unavailable. Writes may have started. Check the connection to reload this case before making another decision.', { exact: true }).waitFor();
  assert(await page.locator('[data-case-approve]').isDisabled(), 'unconfirmed dispatch enabled repeat approval');
  assert(await page.locator('[data-case-request-changes]').isDisabled(), 'unconfirmed dispatch enabled revision');
  assert(await page.locator('[data-case-reject]').isDisabled(), 'unconfirmed dispatch enabled rejection');
  detailOffline = false;
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.locator('[data-execution-receipt][data-receipt-state="simulated"]').waitFor();
  assert(await page.locator('[data-case-approve]').count() === 0, 'confirmed dispatch restored old approval controls');
  assert(approvals === 3, 'reconnect repeated ERP dispatch');
  current = { ...current, updated_at: '2026-10-22T12:07:00Z', audit: [{ action: 'approved', plan_id: planId, at: '2026-10-22T12:01:00Z' }, { action: 'execution_observed', plan_id: planId, at: '2026-10-22T12:01:11Z' }], execution: { ...current.execution, verifier: { status: 'failed', score: 98.5, evaluation_scope: 'product_original_verifier', passed_rule_count: 2, applicable_rule_count: 3, failed_rule_count: 1, rules: { by_dimension: { constraint: [{ rule: 'origin_traceability', status: 'FAIL', applicable: true, expr: 'DO_NOT_RENDER_EXPRESSION', args: { hidden: 'DO_NOT_RENDER_ARGS' } }, { rule: 'demand_coverage', status: 'PASS', applicable: true }, { rule: 'margin_floor', status: 'PASS', applicable: true }] } } } }, result: { ...current.result, simulation_passed: false, strict_success: false, verification_source: 'original_verifier', outcome: 'verification_failed' } };
  await page.getByRole('button', { name: 'Check connection', exact: true }).click();
  await page.locator('[data-rule-results] .rule-failures').getByText('origin_traceability', { exact: true }).waitFor();
  assert((await page.locator('[data-rule-counts]').innerText()) === 'Passed 2 / 3 applicable rules · Failed 1', 'product receipt omitted original rule counts');
  assert(!(await page.locator('[data-case-verification]').innerText()).includes('DO_NOT_RENDER'), 'product rule presentation leaked verifier expression or arguments');
  await page.locator('[data-view-audit]').click();
  assert((await page.locator('[data-activity-timer]').first().innerText()) === 'Execution · 11s', 'execution duration mislabeled as full workflow time');

  qaOffline = false;
  await page.locator('[data-ask-case]').click();
  await page.locator('[data-case-chat] [data-composer-input]').fill('Show case reference');
  await page.locator('[data-case-chat] button.send').click();
  await page.locator('.msg.assistant').last().waitFor();
  if ((await page.locator('.msg.assistant').last().innerText()).includes('Reference:')) throw new Error('reference line remains visible');
  assert((await page.locator('.msg.assistant').last().getByRole('link', { name: 'Current case', exact: true }).getAttribute('href')) === `#case=${id}&evidence=1`, 'display shortening changed source link');
  const savedChat = await page.evaluate(key => localStorage.getItem(key), `forgeflow.chat.v2.${owner}:case:${id}`);
  assert(savedChat.includes(`Reference: case ${id}`), 'display shortening overwrote raw answer history');
  // Shared transport persists the exact in-flight turn across a page reload.
  streamMode = 'hold';
  await page.locator('[data-case-chat] .chat-new').click();
  await page.locator('[data-composer-input]').fill('Resume this case turn'); await page.locator('button.send').click();
  await page.waitForFunction(key => !!JSON.parse(localStorage.getItem(key) || '{}').sessions?.find(s => s.pendingTurn)?.pendingTurn, `forgeflow.chat.v2.${owner}:case:${id}`);
  await until(() => streamStarts >= 1);
  const heldTurn = streamRequests.at(-1).turn_id;
  await page.reload(); await page.locator('.msg.assistant').filter({ hasText: 'Stream answer: Resume this case turn' }).waitFor();
  assert(streamStarts === 1 && streamRequests.at(-1).resume === true && streamRequests.at(-1).turn_id === heldTurn, 'case reload issued a second model turn');
  assert(await page.locator('.msg.user').count() === 1, 'case resume duplicated user prompt');
  heldStreams.get(heldTurn)?.();
  // A connection ending after a trace resumes its cursor, without a new producer.
  streamMode = 'cut';
  await page.locator('[data-composer-input]').fill('Reconnect this turn'); await page.locator('button.send').click();
  await page.locator('.msg.assistant').filter({ hasText: 'Stream answer: Reconnect this turn' }).waitFor();
  assert(streamStarts === 2 && streamRequests.at(-1).lastId === '1' && streamRequests.at(-1).resume === true, 'case SSE disconnect lost cursor or repeated producer');
  // Stop cancels only its own turn; a subsequent question receives its own answer.
  await gotoView(page, "home");
  streamMode = 'hold';
  await page.locator('[data-composer-input]').fill('Old question'); await page.locator('button.send').click();
  await until(() => streamStarts >= 3);
  const stoppedId = streamRequests.at(-1).turn_id;
  await page.getByRole('button', { name: 'Stop response', exact: true }).click();
  await page.locator('.msg.assistant').filter({ hasText: 'Stopped receiving this response' }).waitFor();
  assert(cancelledTurns.includes(stoppedId), 'Stop did not call its turn cancellation API');
  streamMode = 'normal';
  await page.locator('[data-composer-input]').fill('New question'); await page.locator('button.send').click();
  await page.locator('.msg.assistant').filter({ hasText: 'Stream answer: New question' }).waitFor();
  assert(streamStarts === 4 && streamRequests.at(-1).turn_id !== stoppedId, 'new question reused stopped turn');
  assert((await page.locator('.msg.assistant').last().locator('[data-activity-timer]').innerText()) === 'Done', 'missing model elapsed time invented zero-duration completion');
  await page.evaluate(key => { const saved = JSON.parse(localStorage.getItem(key)); const session = saved.sessions.find(item => item.id === saved.active); const activity = session.messages.at(-1).meta.activity; delete activity.hideDuration; activity.startedAt = activity.finishedAt = Date.now(); localStorage.setItem(key, JSON.stringify(saved)); }, `forgeflow.chat.v2.${owner}:knowledge`);
  await page.reload(); await page.locator('.msg.assistant').last().locator('[data-activity-timer]').waitFor();
  assert((await page.locator('.msg.assistant').last().locator('[data-activity-timer]').innerText()) === 'Done', 'legacy persisted fabricated zero duration still visible');

  assert(!(await page.locator('.msg.assistant').last().innerText()).includes('Old question'), 'old turn answer attached to new question');
  // Expired/restarted server buffers are not recreated during resume.
  streamMode = 'hold';
  await page.locator('[data-composer-input]').fill('Unavailable after restart'); await page.locator('button.send').click();
  await until(() => streamStarts >= 5);
  const unavailableId = streamRequests.at(-1).turn_id; streamTurns.delete(unavailableId);
  await page.reload(); await page.getByText('The previous response can no longer be resumed. Your question was not resent and no new model call was started.', { exact: true }).waitFor();
  assert(streamStarts === 5, 'expired resume silently started another model request');
  heldStreams.get(unavailableId)?.(); streamMode = 'off';
  await gotoView(page, "runs");
  await page.getByRole('searchbox').fill('history-001'); await page.locator('.run-link').click();
  await page.locator('[data-rule-results]').waitFor();
  assert((await page.locator('.rule-failures').innerText()).includes('origin_traceability'), 'failure rule hidden behind raw JSON');
  assert((await page.locator('[data-rule-counts]').innerText()) === 'Passed 2 / 3 applicable rules · Failed 1', 'rule count conflates applicable with passed');
  await page.getByText('All recorded rules (4)', { exact: true }).click();
  assert((await page.locator('[data-rule-results] tbody tr').first().innerText()).includes('origin_traceability'), 'failed rule not ordered first');
  assert((await page.locator('.plan-picker').innerText()).includes('Execution status: Executed'), 'historical execution state not distinguished from verification');
  await page.getByRole('button', { name: 'Back to runs', exact: true }).click();
  await page.getByRole('searchbox').fill('history-003'); await page.locator('.run-link').click();
  await page.locator('.plan-picker button').nth(2).waitFor();
  const partialStates = await page.locator('.plan-picker button').allTextContents();
  assert(partialStates.slice(0, 2).every(text => text.includes('Execution outcome unconfirmed; partial execution may have occurred')), 'partial journal or legacy prepared payload falsely declared unexecuted');
  assert(partialStates[2].includes('Prepared; no execution result recorded'), 'plan-only journal wrongly treated as execution');
  assert(!partialStates.join('|').includes('not applied') && !partialStates.join('|').includes('Executed'), 'partial execution received a definitive execution result');
  assert((await page.locator('[data-evidence="panel"]').innerText()).includes('17.5') && (await page.locator('[data-evidence="strict-success"]').innerText()) === 'No' && (await page.locator('[data-evidence="evaluation-valid"]').innerText()) === 'Yes', 'plan status changed the original failed evaluation');
  await page.getByRole('button', { name: 'Back to runs', exact: true }).click();
  await page.getByRole('searchbox').fill('history-002'); await page.locator('.run-link').click();
  await page.locator('[data-rule-results]').waitFor();
  assert((await page.locator('[data-rule-counts]').innerText()).includes('Passed — / — applicable rules · Failed —'), 'missing rule counts became zero or passed');
  assert((await page.locator('[data-rule-results]').innerText()).includes('Individual rule results were not supplied'), 'missing rule projection hidden');
  assert((await page.locator('[data-contract-guidance]').innerText()) === 'Reviewed request', 'contract guidance missing from run detail');
  // A durable approval intent can be resumed; execution has not been sent.
  current = { ...structuredClone(template), approval: { plan_id: planId, phase: 'authorizing', comment: 'Original approval note' }, reason: 'approval_authorization_pending', decision: { ...template.decision, can_approve: true, can_reject: false, can_request_changes: false, can_reconcile: false, authorization_pending: true } };
  approvalReply = 'normal';
  await gotoView(page, "cases"); await page.locator(`[data-case-id="${id}"]`).click();
  await page.locator('[data-authorization-pending]').waitFor();
  assert((await page.locator('[data-case-approve]').innerText()) === 'Continue this approval', 'pending authorization looks like a new approval');
  assert(await page.locator('[data-case-reject]').isDisabled() && await page.locator('[data-case-request-changes]').isDisabled(), 'pending authorization can be replaced');
  assert(await page.locator('textarea[id^="case-comment"]').isDisabled(), 'pending intent can change its comment');
  const beforeContinuation = approvals;
  await page.locator('[data-case-approve]').click(); await page.getByRole('dialog').waitFor();
  assert(approvals === beforeContinuation, 'resume dispatched before confirmation');
  assert((await page.getByRole('dialog').innerText()).includes('no execution request has been sent'), 'resume confirmation hides authorization state');
  await page.getByRole('checkbox').check();
  await page.getByRole('dialog').getByRole('button', { name: 'Continue this approval', exact: true }).click();
  await page.locator('[data-execution-receipt][data-receipt-state="simulated"]').waitFor();
  assert(approvals === beforeContinuation + 1, 'authorization continuation duplicated submission');
  current = { ...structuredClone(template), decision: { ...template.decision, can_approve: false, reason: 'product_version_required' } };
  await gotoView(page, "cases"); await page.locator(`[data-case-id="${id}"]`).click();
  await page.locator('[data-decision-disabled]').filter({ hasText: 'This plan belongs to an older version and is read-only. Prepare a new plan in a separate sandbox running the current version.' }).waitFor();
  assert(await page.locator('[data-case-approve]').isDisabled(), 'legacy plan can request new execution');
  // Explicit closure is distinct from read-only reconciliation and never approves.
  current = { ...structuredClone(template), state: "executing", approval: { plan_id: planId, phase: "dispatching" }, decision: { ...template.decision, can_approve: false, can_reject: false, can_request_changes: false, can_reconcile: true, can_cancel_unstarted: true }, result: { ...template.result, outcome: "executing" } };
  await gotoView(page, "cases"); await page.locator(`[data-case-id="${id}"]`).click();
  const approvalsBeforeClosure = approvals;
  await page.locator('[data-cancel-unstarted]').click();
  await page.getByRole('dialog').waitFor();
  assert(unstartedCancellations === 0, 'opening dialog already retired the plan');
  assert((await page.getByRole('dialog').innerText()).includes('does not interrupt a running task'), 'closure implies active ERP can be interrupted');
  cancellationRefused = true;
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm check and closure', exact: true }).click();
  await page.getByRole('dialog').getByRole('alert').filter({ hasText: 'Execution is already recorded' }).waitFor();
  assert((await page.locator('.case-state-pill').innerText()).includes('Executing'), 'refused closure changed case state');
  assert(approvals === approvalsBeforeClosure, 'cancellation issued an approval');
  cancellationRefused = false;
  await page.getByRole('dialog').getByRole('button', { name: 'Confirm check and closure', exact: true }).click();
  await page.locator('[data-execution-receipt][data-receipt-state="not-started"]').waitFor();
  assert((await page.locator('.case-state-pill').innerText()) === 'Closed without execution', 'unstarted closure displayed as rejected or unknown');
  assert((await page.locator('[data-execution-receipt]').innerText()).includes('does not mean the business recovery succeeded'), 'closure shown as verification success');
  assert(await page.locator('[data-rejection-receipt]').count() === 0, 'closure invented a user rejection');
  assert(await page.locator('[data-case-verification]').count() === 0, 'closure invented an ERP verification');
  assert(await page.locator('[data-cancel-unstarted]').count() === 0, 'closed plan still offers closure');
  assert(unstartedCancellations === 2 && approvals === approvalsBeforeClosure, 'closure duplicated or issued approval');
  // Preparation reply recovery is explicit, owner-bound, and never an approval.
  const pendingPreparation = { ...structuredClone(template), state: "preparing", plan: null, reason: "preparation_confirmation_pending", decision: { ...template.decision, can_approve: false, can_reject: false, can_request_changes: false, preparation_pending: true, can_resume_preparation: true }, result: { ...template.result, outcome: "preparing" } };
  const approvalsBeforePreparation = approvals, createsBeforePreparation = creates;
  current = structuredClone(pendingPreparation);
  await gotoView(page, "cases"); await page.locator(`[data-case-id="${id}"]`).click();
  assert((await page.locator('.case-state-pill').first().innerText()).includes('Preparation needs confirmation'), 'pending preparation portrayed as active work');
  assert((await page.locator('[data-prepare-case]').first().innerText()) === 'Continue this preparation', 'recovery looks like a new plan request');
  assert(preparationRequests === 0, 'reading a pending case automatically retried preparation');
  await page.locator('[data-prepare-case]').first().click();
  await page.locator('[data-case-approve]').waitFor();
  assert(preparationRequests === 1 && approvals === approvalsBeforePreparation && creates === createsBeforePreparation, 'prepare recovery approved or created another case');
  assert(current.plan.plan_id === planId, 'prepare recovery replaced the original plan');
  current = structuredClone(pendingPreparation); preparationReply = 'lost';
  await gotoView(page, "cases"); await page.locator(`[data-case-id="${id}"]`).click();
  await page.locator('[data-prepare-case]').first().click();
  await page.locator('[data-case-approve]').waitFor();
  assert(preparationRequests === 2, 'lost HTTP reply triggered an automatic prepare replay');
  assert(await page.locator('.case-error').count() === 0, 'recovered plan still displays preparation failure');
  current = structuredClone(pendingPreparation); preparationReply = 'interrupted';
  await gotoView(page, "cases"); await page.locator(`[data-case-id="${id}"]`).click();
  await page.locator('[data-prepare-case]').first().click();
  await page.locator('.case-no-plan').filter({ hasText: 'The previous preparation was interrupted' }).waitFor();
  assert(await page.locator('[data-prepare-case]').count() === 0, 'interrupted solver offers blind retry');
  assert(preparationRequests === 3 && approvals === approvalsBeforePreparation && creates === createsBeforePreparation, 'interrupted preparation dispatched another action');
  console.log('hermetic=ok (mock API; no ERP execution)');
} finally { await browser.close(); server.kill(); }
