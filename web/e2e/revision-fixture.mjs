/** Test-only HTTP fixture. No ERP, model, credentials or original verifier. */
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { resolve, sep, extname } from "node:path";
import { fileURLToPath } from "node:url";

export const CASE_ID = "1".repeat(32);
const PLAN_IDS = ["a".repeat(64), "b".repeat(64), "c".repeat(64)];
const stamp = "2026-10-23T12:00:00Z";
const permissions = { can_approve: true, can_reject: true, can_request_changes: true,
  can_reconcile: false, reason: null, revision_reason: null, blocked_checks: [], unknown_checks: [], warnings: [] };
const plan = version => ({ plan_id: PLAN_IDS[version - 1], margin: 93, on_time: true, new_spend: 70,
  cost_delta: null, policy_sha256: "d".repeat(64), snapshot_sha256: "e".repeat(64), prepared_at: stamp,
  actions: [{ kind: "cancel_po", document: "PO-FIXTURE", quantity: 10, summary: "Cancel disrupted supply" }],
  checks: [{ code: "margin", label: "Planning margin", status: "pass", actual: "93%", threshold: version === 1 ? "20%" : "90%" }] });

export function initialCase() {
  return { case_id: CASE_ID, scenario: "A", synthetic: true, state: "awaiting_approval", reason: null,
    order: { ref: "SO-FIXTURE", product: "Browser test fixture", customer: "Test customer", units: 10, due_days: 8 },
    disruption: { kind: "supplier_cancellation", vendor: "Test supplier" }, plan: plan(1),
    approval: null, execution: null, created_at: stamp, updated_at: stamp,
    revision: { version: 1, parent_plan_id: null, reason: null, changes: {} }, history: [], audit: [],
    revision_limits: { original_margin_pct: 20, effective_margin_pct: 20, max_exclusive_pct: 100, decimal_places: 2 },
    decision: structuredClone(permissions),
    result: { execution_status: null, readback_status: "unknown", verifier_status: "unknown", strict_success: null,
      evaluation_valid: null, verified: false, simulation_passed: false, outcome: "awaiting_approval" } };
}

export async function startRevisionFixture({ directory, port = 0 } = {}) {
  const root = resolve(directory ?? fileURLToPath(new URL("../../src/forgeflow/api/static/app", import.meta.url)));
  const fixture = { current: initialCase(), deployment: "local", offline: false, detailOffline: false,
    loseReplies: true, failRevision: true, completeBeforeLost: false, calls: [], detailReads: 0,
    onApproval: null, onSelection: null, onReject: null, revisionBody: null, reprepareBody: null };
  const view = () => {
    const value = structuredClone(fixture.current);
    if (fixture.deployment === "review" || fixture.offline) for (const key of Object.keys(value.decision)) {
      if (key.startsWith("can_")) value.decision[key] = false;
    }
    return value;
  };
  const finishPreparation = () => {
          if (fixture.current.decision.revision_pending) {
            fixture.current.history.push({ version: 1, plan: fixture.current.plan, retired_at: stamp, reason: fixture.revisionBody.reason });
            fixture.current.revision = { version: 2, parent_plan_id: PLAN_IDS[0], reason: fixture.revisionBody.reason, changes: fixture.revisionBody.changes };
            fixture.current.revision_limits.effective_margin_pct = fixture.revisionBody.changes.minimum_margin_pct;
            delete fixture.current.revision_request;
            fixture.current.state = fixture.failRevision ? "failed" : "awaiting_approval";
            fixture.current.plan = fixture.failRevision ? null : plan(2);
            fixture.current.reason = fixture.failRevision ? "planning_model_infeasible" : null;
            fixture.current.decision = fixture.failRevision ? { ...permissions, can_approve: false, can_reject: false, can_request_changes: false, can_reprepare: true } : structuredClone(permissions);
            if (fixture.failRevision) fixture.current.audit.push({ action: "revision_failed", version: 2, at: stamp, reason: "planning_model_infeasible" });
          } else if (fixture.current.decision.repreparation_pending) {
            fixture.current.revision.version = 3;
            fixture.current.plan = plan(3);
            fixture.current.state = "awaiting_approval"; fixture.current.reason = null;
            fixture.current.decision = structuredClone(permissions);
            delete fixture.current.repreparation_request;
          }
  };
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, "http://fixture.invalid").pathname;
    const send = (value, status = 200) => { res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(value)); };
    try {
      if (!path.startsWith("/api/")) {
        if (path === "/viewport.html") {
          // Manual CUA layout harness: a real 390px CSS viewport, not device
          // emulation. The synthetic API and app remain on this same origin.
          res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
          return res.end(`<!doctype html><html lang="en"><title>390px manual layout check</title>
            <style>body{margin:12px;background:#ddd;font:14px system-ui}iframe{display:block;width:390px;height:640px;border:0;background:white}</style>
            <p>Controlled UI preview · 390px CSS viewport · no ERP or model</p>
            <iframe title="390px workbench" src="/app/#case=${CASE_ID}"></iframe></html>`);
        }
        if (path === "/") { res.writeHead(302, { Location: "/app/" }); return res.end(); }
        const file = resolve(root, path === "/app/" ? "index.html" : decodeURIComponent(path).replace(/^\/app\//, ""));
        if (!file.startsWith(root + sep) || !(await stat(file)).isFile()) return send({ detail: "not_found" }, 404);
        const mime = { ".html": "text/html", ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".json": "application/json" };
        res.writeHead(200, { "Content-Type": mime[extname(file)] ?? "application/octet-stream" });
        return res.end(await readFile(file));
      }
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null;
      if (req.method === "POST") fixture.calls.push({ path, body });
      if (path === "/api/auth/me") return send({ scope_id: "browser-test-owner", display_name: "Test operator", deployment: fixture.deployment });
      if (path === "/api/config") return send({ deployment: fixture.deployment, runner_enabled: false });
      if (path === "/api/settings/model") return send({ configured: false });
      if (path === "/api/capabilities") {
        const ready = fixture.deployment !== "review" && !fixture.offline;
        return send({ case_read: true, case_create: false, case_prepare: ready, case_approve: ready,
          product_approval: ready, product_scenarios: [], environment: { execution: "synthetic_executor", ready, scenario: fixture.current.scenario } });
      }
      if (path === "/api/product/cases") return send([view()]);
      if (path === `/api/product/cases/${CASE_ID}`) { fixture.detailReads++; return fixture.detailOffline ? send({ detail: "test_unavailable" }, 503) : send(view()); }
      if (path.endsWith("/events")) return send({ detail: "no_stream_in_fixture" }, 404);
      if (path.startsWith(`/api/product/cases/${CASE_ID}/`) && req.method === "POST") {
        if (fixture.deployment === "review") return send({ detail: "review_read_only" }, 403);
        if (fixture.offline) return send({ detail: "executor_unavailable" }, 503);
        if (path.endsWith("/select") && fixture.onSelection) {
          const reply = await fixture.onSelection(body);
          return send(reply.body, reply.status ?? 200);
        }
        if (path.endsWith("/reject") && fixture.onReject) {
          const reply = await fixture.onReject(body);
          return send(reply.body, reply.status ?? 200);
        }
        if (path.endsWith("/revision")) {
          if (body.plan_id !== fixture.current.plan?.plan_id) return send({ detail: "plan_mismatch" }, 409);
          fixture.revisionBody = body;
          fixture.current.state = "revision_requested";
          fixture.current.reason = "revision_confirmation_pending";
          fixture.current.revision_request = { version: 2, changes: body.changes, reason: body.reason, requested_at: stamp, error: "unavailable" };
          fixture.current.decision = { ...permissions, can_approve: false, can_reject: false, can_request_changes: false,
            revision_pending: true, can_resume_revision: true, can_resume_preparation: false };
          if (fixture.completeBeforeLost) finishPreparation();
          return fixture.loseReplies ? send({ detail: "test_reply_lost" }, 503) : send(view());
        }
        if (path.endsWith("/reprepare")) {
          fixture.reprepareBody = body;
          if (body.failed_version !== fixture.current.revision.version) return send({ detail: "product_revision_conflict" }, 409);
          fixture.current.state = "preparing";
          fixture.current.reason = "preparation_confirmation_pending";
          fixture.current.repreparation_request = { version: 3, failed_version: 2, changes: fixture.current.revision.changes,
            reason: body.reason, requested_at: stamp, error: "unavailable" };
          fixture.current.decision = { ...permissions, can_approve: false, can_reject: false, can_request_changes: false,
            can_reprepare: false, preparation_pending: true, can_resume_preparation: false, repreparation_pending: true, can_resume_repreparation: true };
          if (fixture.completeBeforeLost) finishPreparation();
          return fixture.loseReplies ? send({ detail: "test_reply_lost" }, 503) : send(view());
        }
        if (path.endsWith("/prepare")) {
          finishPreparation();
          return send(view());
        }
        if (path.endsWith("/approve")) return fixture.onApproval ? send(await fixture.onApproval(body)) : send({ detail: "fixture_does_not_execute" }, 409);
        return send({ detail: "not_supported_in_fixture" }, 409);
      }
      if (path === "/api/runs" || path === "/api/knowledge") return send([]);
      return send({ detail: "not_found" }, 404);
    } catch { return send({ detail: "fixture_error" }, 500); }
  });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(port, "127.0.0.1", resolve); });
  return { fixture, url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise(resolve => server.close(resolve)) };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const instance = await startRevisionFixture({ directory: process.env.FORGEFLOW_STATIC_APP });
  console.log(JSON.stringify({ url: instance.url, scope: "test fixture only; no ERP/model execution" }));
  for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, async () => { await instance.close(); process.exit(0); });
}
