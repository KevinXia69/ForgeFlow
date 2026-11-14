import type { AssistantHistoryTurn, ChatImage, ConversationAttachment } from "./types";
import { browserSessionAllows, legacyConversation } from "./browserSession";

/** Tab-session, owner/environment-scoped UI history; cleared when the session ends. */
export type StoredChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  meta?: unknown;
  image?: ChatImage;
  at?: number;
  turnId?: string;
};

const MAX_HISTORY_TURNS = 6;
const MAX_HISTORY_CHARS = 1000;

/** Keep the request context identical when a saved pending turn is resumed. */
export function curateHistory(
  messages: readonly Pick<StoredChatMessage, "role" | "text" | "turnId">[],
  question: string,
  pendingTurnId?: string,
  resume = false,
): AssistantHistoryTurn[] {
  const hasPendingTurn = Boolean(pendingTurnId && messages.some(message => message.turnId === pendingTurnId));
  let prior = hasPendingTurn ? messages.filter(message => message.turnId !== pendingTurnId) : messages;
  // Older saved questions have no turn id. Compare their complete text before
  // clipping; remove only the trailing pending question, never a named old turn.
  const last = prior[prior.length - 1];
  if (resume && !hasPendingTurn && last?.role === "user" && !last.turnId
      && last.text.trim() === question.trim()) {
    prior = prior.slice(0, -1);
  }
  const turns: AssistantHistoryTurn[] = [];
  for (const message of prior) {
    const content = message.text.slice(0, MAX_HISTORY_CHARS).trim();
    if (content) turns.push({ role: message.role, content });
  }
  return turns.slice(-MAX_HISTORY_TURNS);
}

export type PendingChatTurn = { id: string; question: string; lastEventId: string; image?: ChatImage; attachmentText?: string; attachments?: ConversationAttachment[] };
export type SavedConversation = {
  id: string;
  title: string;
  messages: StoredChatMessage[];
  pending: boolean;
  pendingTurn?: PendingChatTurn | null;
  completedTurns?: string[];
  updated: number;
};
export type ConversationStore = { active: string; sessions: SavedConversation[] };
export function newConversationId(): string { return crypto.randomUUID(); }
const key = (scope: string) => `forgeflow.chat.v2.${scope}`;

function parseStored(scope: string): ConversationStore | null {
  if (!browserSessionAllows(scope)) return null;
  try {
    let raw: string | null = null;
    try { raw = sessionStorage.getItem(key(scope)); } catch { /* Memory migration remains available. */ }
    const parsed = JSON.parse(raw || legacyConversation(scope) || "null");
    if (parsed && typeof parsed.active === "string" && Array.isArray(parsed.sessions)) {
      const sessions = parsed.sessions.filter((item: SavedConversation) => item && /^[A-Za-z0-9-]{1,64}$/.test(item.id) && typeof item.title === "string" && Array.isArray(item.messages));
      if (sessions.some((item: SavedConversation) => item.id === parsed.active)) return { active: parsed.active, sessions };
    }
  } catch { /* Invalid or unavailable browser storage starts a new scope. */ }
  return null;
}

function messageUnion(primary: SavedConversation, secondary: SavedConversation): StoredChatMessage[] {
  const seen = new Set<string>();
  const result: StoredChatMessage[] = [];
  for (const message of [...primary.messages, ...secondary.messages]) {
    if (!message || typeof message.id !== "string" || seen.has(message.id)) continue;
    seen.add(message.id);
    result.push(message);
  }
  // New messages carry a wall-clock ordering key and a turn id. This keeps a
  // stale tab's question from appearing between another tab's question/answer.
  return result.sort((left, right) => {
    const lt = typeof left.at === "number" ? left.at : null;
    const rt = typeof right.at === "number" ? right.at : null;
    if (lt !== null && rt !== null && lt !== rt) return lt - rt;
    if (left.turnId && left.turnId === right.turnId && left.role !== right.role) {
      return left.role === "user" ? -1 : 1;
    }
    return 0;
  });
}

export function mergeSession(primary: SavedConversation, secondary: SavedConversation): SavedConversation {
  const completedTurns = [...new Set([
    ...(primary.completedTurns ?? []),
    ...(secondary.completedTurns ?? []),
  ])].filter(id => typeof id === "string" && id.length > 0).slice(-64);
  const pendingCandidates = [primary.pendingTurn, secondary.pendingTurn]
    .filter((turn): turn is PendingChatTurn => turn != null && !completedTurns.includes(turn.id));
  const pendingTurn = pendingCandidates[0] ?? null;
  const title = secondary.updated > primary.updated && secondary.title ? secondary.title : primary.title;
  return {
    id: primary.id,
    title,
    messages: messageUnion(primary, secondary),
    // Once a turn is recorded complete, stale pending flags from another tab
    // must not make a refreshed single-tab conversation look interrupted.
    pending: Boolean(pendingTurn) || (completedTurns.length === 0 && (primary.pending || secondary.pending)),
    pendingTurn,
    completedTurns,
    updated: Math.max(primary.updated, secondary.updated),
  };
}

export function mergeConversations(primary: ConversationStore, secondary: ConversationStore): ConversationStore {
  const secondaryById = new Map(secondary.sessions.map(session => [session.id, session]));
  const merged: SavedConversation[] = [];
  for (const session of primary.sessions) {
    const other = secondaryById.get(session.id);
    merged.push(other ? mergeSession(session, other) : session);
  }
  for (const session of secondary.sessions) {
    if (!primary.sessions.some(item => item.id === session.id)) merged.push(session);
  }
  const sessions = merged.sort((a, b) => b.updated - a.updated);
  const active = sessions.some(item => item.id === primary.active) ? primary.active
    : sessions.some(item => item.id === secondary.active) ? secondary.active
      : sessions[0]?.id ?? primary.active;
  return { active, sessions };
}

export function readConversations(scope: string): ConversationStore {
  const stored = parseStored(scope);
  if (stored) return stored;
  const id = newConversationId();
  return { active: id, sessions: [{ id, title: "", messages: [], pending: false, completedTurns: [], updated: Date.now() }] };
}

export function writeConversations(scope: string, value: ConversationStore): boolean {
  if (!browserSessionAllows(scope)) return false;
  try {
    const existing = parseStored(scope);
    const merged = existing ? mergeConversations(value, existing) : value;
    sessionStorage.setItem(key(scope), JSON.stringify(merged));
    return true;
  } catch { return false; }
}
/** Compatibility for the small entry component; it shares the scoped knowledge session. */
export function conversationId(scope = "unscoped"): string { const store = readConversations(scope); writeConversations(scope, store); return store.active; }
