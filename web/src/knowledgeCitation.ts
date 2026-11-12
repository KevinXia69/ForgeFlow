import type { Passage } from "./types";

export type DocumentTarget = {
  sourcePath: string;
  version?: number;
  locator?: string;
  excerpt?: string;
};

/** Keep the source version that produced the answer, including saved chats. */
export function documentCitation(citation: string, passages: Passage[]): DocumentTarget | null {
  if (!citation.startsWith("document://")) return null;
  const passage = passages.find((row) => citation === `${row.path}:${row.start_line}-${row.end_line}`
    || (row.locator && citation === `${row.path}#${row.locator}`)
    || citation === row.path);
  if (passage) {
    const version = passage.document_version;
    return {
      sourcePath: passage.path.slice("document://".length),
      version: typeof version === "number" && Number.isInteger(version) && version > 0 ? version : undefined,
      locator: passage.locator || `${passage.start_line}-${passage.end_line}`,
      excerpt: passage.text,
    };
  }
  const match = /^document:\/\/(.+?)(?:#(.+)|:(\d+)-(\d+))$/.exec(citation);
  if (!match) return null;
  // Old citations without a captured version must never silently show today's document.
  return { sourcePath: match[1], locator: match[2] || `${match[3]}-${match[4]}` };
}
