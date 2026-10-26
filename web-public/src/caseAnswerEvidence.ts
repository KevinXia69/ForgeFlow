import type { CaseEvidenceCoverage, CaseEvidenceUsage, CaseSourceMode, ProductAskCitation } from "./types";

const FIELD = /^[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+))*$/;
const TOKEN = /^[A-Za-z0-9_.:-]{1,160}$/;

/** Explicit provenance only: old replies must not be upgraded by the current case. */
export function readCaseSourceMode(value: unknown): CaseSourceMode | undefined {
  return value === "model" || value === "deterministic" || value === "local" ? value : undefined;
}

export function readCaseCitations(value: unknown): ProductAskCitation[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(row => {
    if (!row || typeof row !== "object" || typeof row.field !== "string" || !FIELD.test(row.field)) return [];
    return [{ field: row.field, label: typeof row.label === "string" ? row.label : row.field,
      ...(Object.hasOwn(row, "value") ? { value: row.value } : {}),
      ...(typeof row.snapshot_id === "string" && TOKEN.test(row.snapshot_id) ? { snapshot_id: row.snapshot_id } : {}) }];
  });
}

/** Legacy prose citations remain context labels, never field-level claims. */
export function readCaseReferenceLabels(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
}

export function readCaseEvidenceUsage(value: unknown): CaseEvidenceUsage | undefined {
  if (!value || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  const coverage: CaseEvidenceCoverage[] = Array.isArray(record.coverage) ? record.coverage.flatMap(row => {
    if (!row || typeof row !== "object" || typeof row.status !== "string") return [];
    return [{ status: row.status, ...(typeof row.topic === "string" ? { topic: row.topic } : {}),
      ...(typeof row.reason === "string" ? { reason: row.reason } : {}),
      ...(Number.isSafeInteger(row.total) && row.total >= 0 ? { total: row.total } : {}),
      ...(Number.isSafeInteger(row.shown) && row.shown >= 0 ? { shown: row.shown } : {}) }];
  }) : [];
  return {
    ...(typeof record.snapshot_id === "string" && TOKEN.test(record.snapshot_id) ? { snapshot_id: record.snapshot_id } : {}),
    ...(typeof record.source === "string" ? { source: record.source } : {}), coverage,
  };
}

export function caseCitationValue(citation: ProductAskCitation, missing: string): string {
  if (!Object.hasOwn(citation, "value") || citation.value === undefined) return missing;
  if (typeof citation.value === "string") return citation.value;
  try { return JSON.stringify(citation.value) ?? missing; } catch { return missing; }
}
