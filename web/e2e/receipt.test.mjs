import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { fileURLToPath } from "node:url";
const source = fileURLToPath(new URL("../src/receipt.ts", import.meta.url));
const url = "data:text/javascript;base64," + Buffer.from(stripTypeScriptTypes(readFileSync(source, "utf8"))).toString("base64");
const { receiptMarkdown, receiptFilename } = await import(url);
const caseFile = {
  case_id: "a".repeat(32), scenario: "A", state: "succeeded", synthetic: true,
  order: { ref: "SO-1042..1046", product: "Clean Room Fixture", units: 50, due_days: 8, customer: "Northwind" },
  disruption: { kind: "supplier_cancellation", vendor: "Apex Components" }, plan: {
    plan_id: "b".repeat(64), snapshot_sha256: "c".repeat(64), policy_sha256: "d".repeat(64), actions: [{}, {}],
  },
  approval: { approver_label: "Alex Chen", issued_at: "2026-10-23T12:00:00Z" },
  execution: { status: "succeeded", readback: { records: [{}, {}], verified_steps: 7 }, verifier: { status: "passed", strict_success: true } },
  created_at: "2026-10-23T11:00:00Z", updated_at: "2026-10-23T12:00:00Z", reason: null,
  result: { execution_status: "succeeded", readback_status: "passed", verifier_status: "passed", strict_success: true, evaluation_valid: true, verified: true, outcome: "verified" },
};
test("receipt is shareable and does not leak private paths or secrets", () => {
  const md = receiptMarkdown(caseFile, "en", "review");
  for (const phrase of ["ForgeFlow business handling receipt", "Public review · read-only", "Approval scope", "Original verification", "Evidence index"]) {
    assert.ok(md.includes(phrase) || (phrase === "No real ERP" && md.includes("no real ERP")), phrase);
  }
  assert.ok(!md.includes("/Users/"));
  assert.ok(!md.includes("sk-"));
  assert.equal(receiptFilename(caseFile), "forgeflow-receipt-aaaaaaaa.md");
});

test("receipt labels research and customer scopes", () => {
  assert.ok(receiptMarkdown(caseFile, "en", "research").includes("Research evidence"));
  assert.ok(receiptMarkdown(caseFile, "en", "customer").includes("Customer validation"));
  assert.ok(receiptMarkdown(caseFile, "en", "local").includes("Demo / synthetic"));
});
