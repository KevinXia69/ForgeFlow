import type { Passage, SourceMetadata } from "./types";

export type DocumentTarget = {
  sourcePath: string;
  version?: number;
  locator?: string;
  excerpt?: string;
  sourceMetadata?: SourceMetadata;
};

export function sourceUrl(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length > 2048 || /[\s\u0000-\u001f\u007f]/u.test(value)) return;
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || !url.hostname || url.username || url.password) return;
    return url.href;
  } catch { return; }
}

function sourceMetadata(value: unknown): SourceMetadata | undefined {
  if (!value || typeof value !== "object") return;
  const raw = value as Record<string, unknown>, result: SourceMetadata = {};
  for (const [key, limit] of [["title", 240], ["publisher", 240], ["original_locator", 600], ["source_version", 240]] as const) {
    if (typeof raw[key] === "string" && raw[key].trim()) result[key] = raw[key].trim().slice(0, limit);
  }
  const url = sourceUrl(raw.original_url);
  if (url?.startsWith("https:")) result.original_url = url;
  if (raw.material_class === "industry_reference") result.material_class = raw.material_class;
  return Object.keys(result).length ? result : undefined;
}

/** Keep the source version that produced the answer, including saved chats. */
export function documentCitation(citation: string, passages: Passage[]): DocumentTarget | null {
  if (!citation.startsWith("document://")) return null;
  const matches = passages.filter((row) => citation === `${row.path}:${row.start_line}-${row.end_line}`
    || (row.locator && citation === `${row.path}#${row.locator}`)
    || citation === row.path);
  const passage = matches[0];
  if (passage) {
    // The string citation does not encode version. Multiple captured versions
    // cannot safely be resolved to the first one or to today's document.
    if (new Set(matches.map((row) => row.document_version)).size > 1) {
      return { sourcePath: passage.path.slice("document://".length), locator: passage.locator || undefined };
    }
    const version = passage.document_version;
    const metadata = typeof version === "number" && Number.isInteger(version) && version > 0
      ? sourceMetadata(passage.source_metadata) : undefined;
    return {
      sourcePath: passage.path.slice("document://".length),
      version: typeof version === "number" && Number.isInteger(version) && version > 0 ? version : undefined,
      locator: passage.locator || `${passage.start_line}-${passage.end_line}`,
      excerpt: passage.text,
      ...(metadata ? { sourceMetadata: metadata } : {}),
    };
  }
  const match = /^document:\/\/(.+?)(?:#(.+)|:(\d+)-(\d+))$/.exec(citation);
  if (!match) return null;
  // Old citations without a captured version must never silently show today's document.
  return { sourcePath: match[1], locator: match[2] || `${match[3]}-${match[4]}` };
}

export type CitationReference = { citation: string; target: DocumentTarget | null; url?: string };

/** Collapse alternate locators/URLs for one captured source, not unrelated sources. */
export function citationReferences(citations: string[], passages: Passage[]): CitationReference[] {
  const result: CitationReference[] = [], seen = new Set<string>();
  for (const citation of citations) {
    if (typeof citation !== "string") continue;
    let target = documentCitation(citation, passages);
    const url = sourceUrl(citation);
    if (!target && url) {
      const matching = passages.filter((row) => sourceUrl(row.source_metadata?.original_url) === url);
      if (matching.length && new Set(matching.map(row => `${row.path}:${row.document_version}`)).size === 1) {
        const row = matching[0];
        target = documentCitation(row.locator ? `${row.path}#${row.locator}` : `${row.path}:${row.start_line}-${row.end_line}`, passages);
      }
    }
    if (!target && !url) continue;
    const key = target ? `${target.sourcePath}:${target.version ?? "unknown"}` : url!;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push({ citation, target, ...(!target && url ? { url } : {}) });
  }
  return result;
}
