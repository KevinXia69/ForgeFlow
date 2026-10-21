/** Browser-local, owner/environment-scoped UI history. No credential is stored here. */
export type StoredChatMessage = { id: string; role: "user" | "assistant"; text: string; meta?: unknown };
export type PendingChatTurn = { id: string; question: string; lastEventId: string };
export type SavedConversation = { id: string; title: string; messages: StoredChatMessage[]; pending: boolean; pendingTurn?: PendingChatTurn | null; updated: number };
export type ConversationStore = { active: string; sessions: SavedConversation[] };
export function newConversationId(): string { return crypto.randomUUID(); }
const key = (scope: string) => `forgeflow.chat.v2.${scope}`;
export function readConversations(scope: string): ConversationStore {
  if (scope) try {
    const parsed = JSON.parse(localStorage.getItem(key(scope)) || "null");
    if (parsed && typeof parsed.active === "string" && Array.isArray(parsed.sessions)) {
      const sessions = parsed.sessions.filter((item: SavedConversation) => item && /^[A-Za-z0-9-]{1,64}$/.test(item.id) && typeof item.title === "string" && Array.isArray(item.messages));
      if (sessions.some((item: SavedConversation) => item.id === parsed.active)) return { active: parsed.active, sessions };
    }
  } catch { /* Invalid or unavailable browser storage starts a new scope. */ }
  const id = newConversationId();
  return { active: id, sessions: [{ id, title: "", messages: [], pending: false, updated: Date.now() }] };
}
export function writeConversations(scope: string, value: ConversationStore): boolean {
  if (!scope) return false;
  try { localStorage.setItem(key(scope), JSON.stringify(value)); return true; } catch { return false; }
}
/** Compatibility for the small entry component; it shares the scoped knowledge session. */
export function conversationId(scope = "unscoped"): string { const store = readConversations(scope); writeConversations(scope, store); return store.active; }
