import { test } from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const module = process.env.FORGEFLOW_VERIFICATION_MODULE;
const { originalScoreThresholdMiss } = await import(module ? pathToFileURL(module).href : "../src/verificationExplanation.ts");
const execution = { verifier: { evaluation_scope: "product_original_verifier", evaluation_valid: true,
  strict_success: false, original_passed: false, score: 95.18, applicable_rule_count: 95,
  passed_rule_count: 95, failed_rule_count: 0 } };
test("distinguishes all-rule pass from original overall-score failure", () => {
  const before = structuredClone(execution);
  assert.equal(originalScoreThresholdMiss(execution), true);
  assert.deepEqual(execution, before);
});
test("does not explain unknown, synthetic, invalid or failed-rule evidence as score-only", () => {
  for (const change of [{source:"synthetic_executor"}, {evaluation_scope:"customer_verifier"},
    {evaluation_valid:null}, {original_passed:null}, {strict_success:true},
    {score:99}, {score:NaN}, {score:Infinity}, {score:-1},
    {applicable_rule_count:0}, {applicable_rule_count:Infinity, passed_rule_count:Infinity},
    {passed_rule_count:94, failed_rule_count:1}, {failed_rule_count:null}]) {
    assert.equal(originalScoreThresholdMiss({verifier:{...execution.verifier,...change}}), false);
  }
  assert.equal(originalScoreThresholdMiss(null), false);
});
