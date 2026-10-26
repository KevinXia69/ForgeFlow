import type { ModelConfig } from "./modelConfig";
import type {
  Answer,
  Approval,
  AssistantHistoryTurn,
  Capabilities,
  CaseFile,
  Config,
  ModelSettingsInfo,
  ModelTestResult,
  Plan,
  ProductAskAnswer,
  RunDetail,
  RunSummary,
  ActivityState,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...(init.body ? { "Content-Type": "application/json" } : {}),
    ...((init.headers as Record<string, string>) ?? {}),
  };
  const response = await fetch(path, { credentials: "same-origin", ...init, headers });
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    throw new ApiError(typeof detail === "string" ? detail : response.statusText, response.status);
  }
  return body as T;
}

export const getConfig = () => api<Config>("/api/config");
export const getCapabilities = () => api<Capabilities>("/api/capabilities");
export const getRuns = () => api<RunSummary[]>("/api/runs");
export const getRun = (runId: string) => api<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`);
export const getPlans = (runId: string) =>
  api<Plan[]>(`/api/runs/${encodeURIComponent(runId)}/plans`);
export const getPlan = (runId: string, planId: string) =>
  api<Plan>(`/api/runs/${encodeURIComponent(runId)}/plans/${planId}`);
export const getApprovals = (runId: string, planId: string) =>
  api<Approval[]>(`/api/runs/${encodeURIComponent(runId)}/plans/${planId}/approvals`);
export const login = (inviteCode: string) =>
  api<{ uid: string; expires_at: number }>("/api/auth/session", {
    method: "POST",
    body: JSON.stringify({ invite_code: inviteCode }),
  });

/** Product cases: create returns a prepared case in awaiting_approval. */
export const getProductCases = () => api<CaseFile[]>("/api/product/cases");
export const getProductCase = (caseId: string, signal?: AbortSignal) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}`, { signal });
export type ProductDraft = { kind: string; product_code: string; units: number; due_days: number };
export const previewProductDraft = (body: ProductDraft) =>
  api<{ valid: boolean; scenario: string | null; errors: string[]; writes_nothing: boolean }>("/api/product/cases/draft", {
    method: "POST", body: JSON.stringify(body),
  });
export const createProductFromDraft = (body: ProductDraft) =>
  api<CaseFile>("/api/product/cases/from-draft", {
    method: "POST", body: JSON.stringify(body),
  });
export const createProductCase = (scenario = "A") =>
  api<CaseFile>("/api/product/cases", {
    method: "POST",
    body: JSON.stringify({ scenario }),
  });
export const prepareProductCase = (caseId: string) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/prepare`, {
    method: "POST",
  });
export const selectProductCandidate = (caseId: string, selection: import("./types").ProductSelection) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/select`, {
    method: "POST", body: JSON.stringify(selection),
  });
export const reprepareProductCase = (caseId: string, failedVersion: number, reason: string) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/reprepare`, {
    method: "POST", body: JSON.stringify({ failed_version: failedVersion, reason }),
  });
export const approveProductCase = (caseId: string, planId: string, comment = "") =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/approve`, {
    method: "POST",
    body: JSON.stringify({ plan_id: planId, comment }),
  });
export const rejectProductCase = (caseId: string, planId: string, reason: string) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/reject`, {
    method: "POST", body: JSON.stringify({ plan_id: planId, reason }),
  });
export const reviseProductCase = (caseId: string, planId: string, reason: string, changes: { minimum_margin_pct: number }) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/revision`, {
    method: "POST", body: JSON.stringify({ plan_id: planId, reason, changes }),
  });
export const cancelUnstartedProductCase = (caseId: string, planId: string) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/cancel-unstarted`, { method: "POST", body: JSON.stringify({ plan_id: planId }) });
export const reconcileProductCase = (caseId: string) =>
  api<CaseFile>(`/api/product/cases/${encodeURIComponent(caseId)}/reconcile`, { method: "POST" });
/**
 * Optional case Q&A. The route may not exist yet; callers fall back to a
 * deterministic answer built from the current case fields.
 */
export const askProductCase = (caseId: string, question: string, conversationId?: string, signal?: AbortSignal, lang?: "zh" | "en") =>
  api<ProductAskAnswer>(`/api/product/cases/${encodeURIComponent(caseId)}/ask`, {
    method: "POST",
    body: JSON.stringify({ question, conversation_id: conversationId, ...(lang ? { lang } : {}) }),
    signal,
  });

const MAX_CONVERSATION_ID_CHARS = 64;

let fallbackConversationId: string | null = null;

/** A page-lifetime conversation id for callers that omit one explicitly. */
function generatedConversationId(): string {
  if (fallbackConversationId) return fallbackConversationId;
  let id = "";
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      id = crypto.randomUUID();
    }
  } catch {
    id = "";
  }
  if (!id) {
    id = `conv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }
  fallbackConversationId = id.slice(0, MAX_CONVERSATION_ID_CHARS);
  return fallbackConversationId;
}

function conversationIdValue(conversationId?: string): string {
  const trimmed = conversationId?.trim();
  return (trimmed || generatedConversationId()).slice(0, MAX_CONVERSATION_ID_CHARS);
}

/**
 * Ask the evidence assistant. Prior turns are curated by the caller; an empty
 * history is omitted entirely so the server never sees a no-op array.
 */
export const askAssistant = (
  question: string,
  model?: ModelConfig,
  history?: AssistantHistoryTurn[],
  conversationId?: string,
  lang: "zh" | "en" = "zh",
  signal?: AbortSignal,
) => {
  const body: Record<string, unknown> = {
    question,
    lang,
    conversation_id: conversationIdValue(conversationId),
  };
  if (model) body.model = model;
  if (history && history.length > 0) body.history = history;
  return api<Answer>("/api/assistant/answer", {
    method: "POST",
    signal,
    body: JSON.stringify(body),
  });
};

/** Read the server-side platform default; the response never contains a key. */
export const getModelSettings = () => api<ModelSettingsInfo>("/api/settings/model");

/**
 * Test a browser-local config, or the platform default when none is supplied.
 * The platform test intentionally posts an empty object so the server uses
 * its own hidden key.
 */
export const testModelConnection = (model?: ModelConfig) =>
  api<ModelTestResult>("/api/settings/model/test", {
    method: "POST",
    body: JSON.stringify(model ? { model } : {}),
  });

/**
 * Live DSH trace + answer over SSE. The callback receives each new snapshot so
 * the reasoning row can fill in before the turn completes.
 */
export type StreamTurn = {
  id: string;
  lastEventId: string;
  resume: boolean;
  onCursor: (id: string) => void;
};

export const cancelAssistantTurn = (conversationId: string, turnId: string) =>
  api<void>("/api/assistant/cancel", { method: "POST", body: JSON.stringify({ conversation_id: conversationId, turn_id: turnId }), signal: AbortSignal.timeout(10_000) });
export const cancelCaseTurn = (caseId: string, conversationId: string, turnId: string) =>
  api<void>(`/api/product/cases/${encodeURIComponent(caseId)}/ask/cancel`, { method: "POST", body: JSON.stringify({ conversation_id: conversationId, turn_id: turnId }), signal: AbortSignal.timeout(10_000) });

export function streamProductCase(caseId: string, question: string, conversationId: string, onTrace: (activity: ActivityState) => void, signal: AbortSignal | undefined, turn: StreamTurn, lang?: "zh" | "en"): Promise<ProductAskAnswer> {
  return streamReply(`/api/product/cases/${encodeURIComponent(caseId)}/ask/stream`, { question, conversation_id: conversationId, ...(lang ? { lang } : {}) }, onTrace, signal, turn);
}

export function streamAssistant(question: string, history: AssistantHistoryTurn[] | undefined, conversationId: string, lang: "zh" | "en", onTrace: (activity: ActivityState) => void, signal?: AbortSignal, turn?: StreamTurn): Promise<Answer> {
  return streamReply("/api/assistant/stream", { question, lang, conversation_id: conversationId, ...(history?.length ? { history } : {}) }, onTrace, signal, turn);
}

/** A transport reconnect only subscribes to the existing turn; it never starts another model call. */
async function streamReply<T>(path: string, body: Record<string, unknown>, onTrace: (activity: ActivityState) => void, signal?: AbortSignal, turn?: StreamTurn): Promise<T> {
  const cursor = { lastEventId: turn?.lastEventId ?? "" };
  try {
    return await readStream<T>(path, body, onTrace, signal, cursor, turn, turn?.resume ?? false);
  } catch (error) {
    if (signal?.aborted || !turn || !(error instanceof TypeError || error instanceof ApiError && error.message === "stream_incomplete")) throw error;
    return readStream<T>(path, body, onTrace, signal, cursor, turn, true);
  }
}

async function readStream<T>(path: string, body: Record<string, unknown>, onTrace: (activity: ActivityState) => void, signal: AbortSignal | undefined, cursor: { lastEventId: string }, turn: StreamTurn | undefined, resume: boolean): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (cursor.lastEventId) headers["Last-Event-ID"] = cursor.lastEventId;
  const response = await fetch(path, { method: "POST", credentials: "same-origin", signal, headers,
    body: JSON.stringify({ ...body, ...(turn ? { turn_id: turn.id, resume } : {}) }),
  });
  if (!response.ok || !response.body) {
    const failure = await response.json().catch(() => null);
    throw new ApiError(typeof failure?.detail === "string" ? failure.detail : "stream_unavailable", response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const startedAt = Date.now();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const lines = part.split("\n");
        const idLine = lines.find(item => item.startsWith("id:"));
        const line = lines.filter(item => item.startsWith("data:")).map(item => item.slice(5).trim()).join("\n");
        if (!line) continue;
        let event: Record<string, unknown>;
        try { event = JSON.parse(line) as Record<string, unknown>; } catch { continue; }
        // A final is acknowledged only when its answer is saved. If the page
        // unloads between receiving it and React persistence, replay it again.
        if (event.type !== "final" && idLine) { cursor.lastEventId = idLine.slice(3).trim(); turn?.onCursor(cursor.lastEventId); }
        if (event.type === "trace" && Array.isArray(event.activity)) {
          onTrace({ active: true, startedAt, hideDuration: true, steps: event.activity as ActivityState["steps"] });
        } else if (event.type === "final") return event as T;
        else if (event.type === "error") throw new ApiError(String(event.code ?? "stream_failed"), 503);
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  throw new ApiError("stream_incomplete", 503);
}
