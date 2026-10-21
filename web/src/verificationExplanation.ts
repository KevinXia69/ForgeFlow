import type { CaseFile } from "./types";

/** Explain only a confirmed original score failure, never unknown or synthetic evidence. */
export function originalScoreThresholdMiss(execution: CaseFile["execution"] | null | undefined): boolean {
  const value = execution?.verifier;
  return value?.source !== "synthetic_executor" && value?.evaluation_scope === "product_original_verifier"
    && value.evaluation_valid === true && value.strict_success === false && value.original_passed === false
    && typeof value.score === "number" && Number.isFinite(value.score) && value.score >= 0 && value.score < 99
    && typeof value.applicable_rule_count === "number" && Number.isInteger(value.applicable_rule_count) && value.applicable_rule_count > 0
    && value.passed_rule_count === value.applicable_rule_count && value.failed_rule_count === 0;
}
