import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
const cache = new Map();
function moduleUrl(path) {
  if (cache.has(path)) return cache.get(path);
  let source = stripTypeScriptTypes(readFileSync(path, "utf8"));
  source = source.replace(/from\s+(["'])(\.\.?\/[^"']+)\1/g, (_match, _quote, relative) =>
    "from " + JSON.stringify(moduleUrl(resolve(dirname(path), relative + ".ts"))));
  const url = "data:text/javascript;base64," + Buffer.from(source).toString("base64");
  cache.set(path, url); return url;
}
const caseFile = {
  case_id: "a".repeat(32), scenario: "A", state: "succeeded", synthetic: true,
  order: { ref: "SO-1042..1046", product: "Clean Room Fixture", units: 50, due_days: 8, customer: "Northwind" },
  disruption: { kind: "supplier_cancellation", vendor: "Apex Components" }, plan: {
    plan_id: "b".repeat(64), snapshot_sha256: "c".repeat(64), policy_sha256: "d".repeat(64), actions: [{}, {}],
  },
  approval: { approver_label: "Alex Chen", issued_at: "2026-10-23T12:00:00Z" },
  execution: {
    status: "succeeded", writes: [{ kind: "cancel_purchase_order" }, { kind: "create_purchase_order" }],
    readback: { records: [{}, {}], verified_steps: 7, execution_validated: true },
    verifier: { status: "passed", strict_success: true, evaluation_valid: true, evaluation_scope: "product_original_verifier" },
  },
  created_at: "2026-10-23T11:00:00Z", updated_at: "2026-10-23T12:00:00Z", reason: null,
  result: { execution_status: "succeeded", readback_status: "passed", verifier_status: "passed", strict_success: true, evaluation_valid: true, verified: true, verification_source: "original_verifier", simulation_passed: false, outcome: "verified" },
};

for (const bundle of ["web", "web-public"]) {
  const url = moduleUrl(fileURLToPath(new URL(`../../${bundle}/src/receipt.ts`, import.meta.url)));
  const { receiptMarkdown, receiptFilename } = await import(url);
  const languages = bundle === "web" ? ["en", "zh"] : ["en"];
  test(`${bundle}: receipt separates plan scope, legacy margin and execution steps`, () => {
    const record = structuredClone(caseFile); record.plan.margin = 80;
    const before = structuredClone(record), md = receiptMarkdown(record, "en");
    assert.match(md, /Total planning scope: 50 units/);
    assert.match(md, /Directly linked demand and failed supply: unknown/);
    assert.match(md, /Recorded margin \(legacy basis\): 80.00%/);
    assert.match(md, /Business document actions: 2; verified execution steps: 7/);
    assert.doesNotMatch(md, /- Impact: 50/);
    assert.deepEqual(record, before);
  });


  test(`${bundle}: receipt is shareable and does not include private fields`, () => {
    const privateFields = structuredClone(caseFile);
    privateFields.owner_uid = "owner-private-canary";
    privateFields.secret = "sk-private-canary";
    privateFields.execution.private_path = "/var/lib/private-canary";
    const md = receiptMarkdown(privateFields, "en", "review");
    for (const phrase of ["ForgeFlow business handling receipt", "Public review · read-only", "Approval scope", "Original verification", "Evidence index"]) {
      assert.ok(md.includes(phrase), phrase);
    }
    assert.ok(!md.includes("private-canary"));
    assert.equal(receiptFilename(caseFile), "forgeflow-receipt-aaaaaaaa.md");
  });

  test(`${bundle}: page modes do not override saved execution evidence`, () => {
    for (const [deployment, expected] of [["review", "Public review · read-only"], ["research", "Research evidence view"], ["customer", "Customer validation"], ["local", "Workbench"]]) {
      const md = receiptMarkdown(caseFile, "en", deployment);
      assert.ok(md.includes(expected));
      assert.ok(md.includes("Execution evidence: Real sandbox execution · writes and readback confirmed"));
      assert.ok(md.includes("Data provenance: Synthetic benchmark data · non-production ERP"));
    }
  });

  for (const lang of languages) {
    const zh = lang === "zh";
    const live = zh ? "执行证据：真实沙箱执行 · 写入与回读已确认" : "Execution evidence: Real sandbox execution · writes and readback confirmed";
    const unverified = zh ? "执行证据：未验证 · 未确认真实沙箱执行" : "Execution evidence: Unverified · real sandbox execution not confirmed";
    test(`${bundle}/${lang}: synthetic benchmark data can have confirmed real Odoo execution`, () => {
      const md = receiptMarkdown(caseFile, lang);
      assert.ok(md.includes(zh ? "合成基准数据 · 非生产 ERP" : "Synthetic benchmark data · non-production ERP"));
      assert.ok(md.includes(live));
      assert.ok(!md.includes(zh ? "不代表真实 ERP 执行" : "not a real ERP execution"));
      assert.ok(!md.includes(zh ? "不写真实 ERP" : "no real ERP write"));
      assert.ok(md.includes(zh ? "原始验收：passed" : "Original verification: passed"));
    });

    test(`${bundle}/${lang}: simulated executor is not labelled as original verification or live ERP`, () => {
      const simulated = structuredClone(caseFile);
      simulated.execution.verifier = { status: "passed", source: "synthetic_executor", strict_success: true, evaluation_valid: true };
      simulated.result = { ...simulated.result, verified: false, simulation_passed: true, verification_source: "synthetic_executor", outcome: "simulation_passed" };
      const md = receiptMarkdown(simulated, lang);
      assert.ok(md.includes(zh ? "执行证据：模拟执行 · 不连接真实 ERP" : "Execution evidence: Simulated execution · no real ERP connection"));
      assert.ok(md.includes(zh ? "模拟检查：passed" : "Simulated checks: passed"));
      assert.ok(md.includes(zh ? "模拟回读：passed" : "Simulated readback: passed"));
      assert.ok(!md.includes(live));
      assert.ok(!md.includes(zh ? "原始验收" : "Original verification"));
    });

    test(`${bundle}/${lang}: unknown, failed and unstarted cases cannot be promoted by stale success fields`, () => {
      const variants = [
        c => { c.execution = null; c.state = "execution_unknown"; },
        c => { c.execution.status = "unknown"; },
        c => { c.execution.status = "failed"; },
        c => { c.execution.status = "not_started"; },
        c => { c.state = "failed"; },
        c => { c.execution.readback.execution_validated = false; },
        c => { delete c.execution.readback.execution_validated; },
        c => { c.execution.writes = []; },
        c => { c.execution.verifier = { status: "passed", strict_success: true, evaluation_valid: true }; },
        c => { c.execution.verifier.strict_success = null; },
        c => { c.execution.verifier.evaluation_valid = false; },
        c => { c.result.verified = false; },
        c => { c.result.readback_status = "unknown"; },
        c => { c.result.verifier_status = "unknown"; },
        c => { c.result.execution_status = "unknown"; },
        c => { c.execution.verifier.source = "synthetic_executor"; },
      ];
      for (const change of variants) {
        const unknown = structuredClone(caseFile);
        change(unknown);
        const md = receiptMarkdown(unknown, lang);
        assert.ok(md.includes(unverified), change.toString());
        assert.ok(!md.includes(live), change.toString());
      }
    });

    test(`${bundle}/${lang}: stale summaries cannot report a missing, unknown or failed execution as succeeded`, () => {
      for (const status of [null, "unknown", "failed", "not_started"]) {
        const inconsistent = structuredClone(caseFile);
        if (status === null) inconsistent.execution = null;
        else inconsistent.execution.status = status;
        const md = receiptMarkdown(inconsistent, lang);
        assert.ok(md.includes(zh ? "- 执行状态：未确认" : "- Execution: Unconfirmed"));
        assert.ok(!md.includes(zh ? "- 执行状态：succeeded" : "- Execution: succeeded"));
        if (status === null) {
          assert.ok(!md.includes(zh ? "- 回读：passed" : "- Readback: passed"));
          assert.ok(!md.includes(zh ? "- 验收：passed" : "- Verification: passed"));
        }
      }
    });

    test(`${bundle}/${lang}: readback and verification conflicts cannot appear as affirmative passes`, () => {
      for (const validated of [false, undefined]) {
        const inconsistent = structuredClone(caseFile);
        inconsistent.execution.readback.execution_validated = validated;
        const md = receiptMarkdown(inconsistent, lang);
        assert.ok(md.includes(zh ? "- 回读：未确认" : "- Readback: Unconfirmed"));
        assert.ok(!md.includes(zh ? "- 回读：passed" : "- Readback: passed"));
      }
      for (const change of [
        c => { c.execution.verifier.status = "unknown"; },
        c => { c.execution.verifier.status = "failed"; },
        c => { c.execution.verifier.strict_success = null; },
        c => { c.execution.verifier.evaluation_valid = false; },
      ]) {
        const inconsistent = structuredClone(caseFile);
        change(inconsistent);
        const md = receiptMarkdown(inconsistent, lang);
        assert.ok(!md.includes(zh ? "- 原始验收：passed" : "- Original verification: passed"));
        assert.ok(md.includes(zh ? "- 原始验收：未" : "- Original verification: Un"));
      }
    });

    test(`${bundle}/${lang}: matching failed records retain their failure states`, () => {
      const failed = structuredClone(caseFile);
      failed.state = "failed";
      failed.execution.status = "failed";
      failed.execution.readback.execution_validated = false;
      failed.execution.verifier.status = "failed";
      failed.execution.verifier.strict_success = false;
      failed.result = { ...failed.result, execution_status: "failed", readback_status: "failed", verifier_status: "failed", strict_success: false, verified: false };
      const md = receiptMarkdown(failed, lang);
      assert.ok(md.includes(zh ? "- 执行状态：failed" : "- Execution: failed"));
      assert.ok(md.includes(zh ? "- 回读：failed" : "- Readback: failed"));
      assert.ok(md.includes(zh ? "- 原始验收：failed" : "- Original verification: failed"));
      assert.ok(md.includes(unverified));
    });
  }

  if (bundle === "web-public") {
    test("web-public: the public bundle retains its English-only export contract", () => {
      assert.equal(receiptMarkdown(caseFile, "zh"), receiptMarkdown(caseFile, "en"));
    });
  }
}
