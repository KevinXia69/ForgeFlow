/** Answer provenance is immutable; compare it only with the latest retrieved case. */
import type { CaseAnswerContext, CaseFile } from "./types";

export type AnswerContextNotice = { kind: "recorded" | "earlier" | "unknown"; text: string };

export function caseAnswerContext(caseFile: CaseFile): CaseAnswerContext {
  const comparison = caseFile.comparison?.status === "ready" ? caseFile.comparison : null;
  return {
    schema_version: 1, case_id: caseFile.case_id, state: caseFile.state,
    version: caseFile.revision?.version ?? null, plan_id: caseFile.plan?.plan_id ?? null,
    comparison_id: comparison?.comparison_id ?? null,
    selection_revision: comparison?.selection_revision ?? null,
    selection_pending: caseFile.decision?.selection_pending === true,
    updated_at: caseFile.updated_at ?? null, outcome: caseFile.result?.outcome ?? null,
  };
}

export function readAnswerContext(value: unknown): CaseAnswerContext | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const text = (value: unknown, limit: number) => typeof value === "string" && value.length > 0 && value.length <= limit;
  const optionalText = (value: unknown) => value === null || text(value, 128);
  const version = (value: unknown) => value === null || (typeof value === "number" && Number.isSafeInteger(value) && value > 0);
  if (record.schema_version !== 1 || !text(record.case_id, 128) || !text(record.state, 64)
      || !version(record.version) || !version(record.selection_revision)
      || typeof record.selection_pending !== "boolean"
      || ![record.plan_id, record.comparison_id, record.updated_at, record.outcome].every(optionalText)) return null;
  return record as CaseAnswerContext;
}

export function answerContextKind(value: unknown, current: CaseAnswerContext): AnswerContextNotice["kind"] {
  const recorded = readAnswerContext(value);
  if (!recorded || recorded.case_id !== current.case_id) return "unknown";
  return (Object.keys(current) as (keyof CaseAnswerContext)[]).every(key => recorded[key] === current[key])
    ? "recorded" : "earlier";
}
