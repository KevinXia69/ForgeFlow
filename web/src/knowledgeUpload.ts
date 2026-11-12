export type UploadProgress = "processing" | "indexing" | "ready" | "review" | "failed";

export type UploadedDocument = {
  source_path: string;
  parse_status?: string;
  lifecycle?: { searchable?: boolean; needs_review?: boolean; withdrawn?: boolean };
};

/** Parsing completion alone does not establish that a document is searchable. */
export function uploadProgress(jobState?: string, document?: UploadedDocument): UploadProgress {
  if (["failed", "cancelled"].includes(jobState ?? "") || document?.lifecycle?.withdrawn
    || document?.parse_status === "failed") return "failed";
  if (document?.lifecycle?.needs_review) return "review";
  if (document?.lifecycle?.searchable) return "ready";
  if (jobState === "succeeded" || document) return "indexing";
  return "processing";
}
