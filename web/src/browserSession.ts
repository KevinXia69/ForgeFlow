/** Browser data is usable only by the authenticated session that opened it. */
export const SESSION_CHANGE = "forgeflow.session-change";
const SESSION_EPOCH = "forgeflow.session-epoch";
const TAB_EPOCH = "forgeflow.tab-epoch";
const TAB_SCOPE = "forgeflow.tab-scope";
const clearCallbacks = new Set<() => void>();
const legacyChats = new Map<string, string>();
let workspace = "";
let retired = false;
let fallbackEpoch = "";

function storage(kind: "localStorage" | "sessionStorage"): Storage | null {
  try { return typeof window === "undefined" ? null : window[kind]; }
  catch { return null; }
}

function sharedEpoch(): string {
  try {
    const target = storage("localStorage");
    return target ? target.getItem(SESSION_EPOCH) ?? "" : fallbackEpoch;
  } catch { return fallbackEpoch; }
}

let epoch = sharedEpoch();

function sensitiveKey(key: string): boolean {
  return key.startsWith("forgeflow.chat.") || key === "forgeflow.model"
    || key.startsWith("forgeflow.model.") || key.startsWith("forgeflow.case-view.")
    || key.startsWith("forgeflow.evidence-view.") || key.startsWith("forgeflow-investigation-")
    || key.startsWith("recovery-review.") || key.startsWith("recovery-review-");
}

function clearStorage(kind: "localStorage" | "sessionStorage") {
  try {
    const target = storage(kind);
    if (!target) return;
    for (let index = target.length - 1; index >= 0; index--) {
      const key = target.key(index);
      if (key && sensitiveKey(key)) target.removeItem(key);
    }
  } catch { /* Access may be disabled; memory still retires below. */ }
}

function clearSensitiveData() {
  legacyChats.clear();
  clearStorage("localStorage");
  clearStorage("sessionStorage");
  try { storage("sessionStorage")?.removeItem(TAB_SCOPE); } catch { /* Optional marker. */ }
  for (const clear of clearCallbacks) clear();
}

function markTabEpoch() {
  // A retiring document's late non-chat drafts are discarded again on reload.
  try { storage("sessionStorage")?.setItem(TAB_EPOCH, retired ? "retired:" + epoch : epoch); } catch { /* Optional. */ }
}

// Upgrade preserves only the authenticated owner's existing chats. Hold them
// in memory while auth resolves; never import old plaintext model credentials.
try {
  const previous = storage("localStorage");
  if (previous) for (let index = 0; index < previous.length; index++) {
    const key = previous.key(index);
    if (key?.startsWith("forgeflow.chat.v2.")) {
      const value = previous.getItem(key);
      if (value) legacyChats.set(key, value);
    }
  }
} catch { /* Unavailable legacy storage contains no readable migration source. */ }
clearStorage("localStorage");
try {
  if (storage("sessionStorage")?.getItem(TAB_EPOCH) !== epoch) clearStorage("sessionStorage");
} catch { /* Storage unavailable. */ }
markTabEpoch();

let channel: BroadcastChannel | null = null;
if (typeof window !== "undefined" && typeof BroadcastChannel !== "undefined") {
  try { channel = new BroadcastChannel(SESSION_CHANGE); } catch { /* Storage/focus fallback. */ }
}

export function onBrowserSessionClear(clear: () => void): () => void {
  clearCallbacks.add(clear);
  return () => clearCallbacks.delete(clear);
}

/** Bind only after /auth/me succeeds. A retired document can never re-enable itself. */
export function bindBrowserWorkspace(scope: string): boolean {
  if (!scope) {
    workspace = "";
    clearSensitiveData();
    return false;
  }
  if (retired || sharedEpoch() !== epoch) {
    retireBrowserSession(false);
    return false;
  }
  if (workspace && workspace !== scope) {
    retireBrowserSession(true);
    return false;
  }
  try {
    const previousScope = storage("sessionStorage")?.getItem(TAB_SCOPE);
    if (previousScope && previousScope !== scope) clearSensitiveData();
    storage("sessionStorage")?.setItem(TAB_SCOPE, scope);
  } catch { /* Authentication and in-memory scope still guard access. */ }
  workspace = scope;
  const prefix = "forgeflow.chat.v2." + scope;
  for (const [key, value] of legacyChats) {
    if (key !== prefix && !key.startsWith(prefix + ":")) {
      legacyChats.delete(key);
      continue;
    }
    try {
      const target = storage("sessionStorage");
      if (target) {
        if (!target.getItem(key)) target.setItem(key, value);
        legacyChats.delete(key);
      }
    } catch { /* Retain this owner's migration in memory if storage is full. */ }
  }
  return true;
}

/** A quota-limited tab can still display its own migrated history in memory. */
export function legacyConversation(scope: string): string | null {
  return browserSessionAllows(scope) ? legacyChats.get("forgeflow.chat.v2." + scope) ?? null : null;
}

/** Compare the shared marker synchronously, even before a suspended tab gets its event. */
export function browserSessionAllows(scope: string): boolean {
  if (retired || sharedEpoch() !== epoch) {
    retireBrowserSession(false);
    return false;
  }
  return Boolean(workspace && (scope === workspace || scope.startsWith(workspace + ":")));
}

export function browserSessionRetired(): boolean {
  if (!retired && sharedEpoch() !== epoch) retireBrowserSession(false);
  return retired;
}

function announce(epochValue: string) {
  fallbackEpoch = epochValue;
  try {
    storage("localStorage")?.setItem(SESSION_EPOCH, epochValue);
    storage("localStorage")?.setItem(SESSION_CHANGE, epochValue);
  } catch {
    // A browser may permit reads while rejecting quota-limited writes.
    epoch = sharedEpoch();
  }
  channel?.postMessage({ epoch: epochValue });
}

/** Call after confirmed server logout, or before leaving an invalidated workspace. */
export function retireBrowserSession(broadcast = true): void {
  const first = !retired;
  retired = true;
  workspace = "";
  clearSensitiveData();
  if (broadcast && first) {
    epoch = crypto.randomUUID();
    announce(epoch);
  }
  markTabEpoch();
}

/** A completed login starts a fresh browser session; MFA challenges do not. */
export function announceBrowserLogin(): void {
  clearSensitiveData();
  workspace = "";
  epoch = crypto.randomUUID();
  announce(epoch);
  markTabEpoch();
}

if (typeof window !== "undefined") {
  const changed = () => {
    retireBrowserSession(false);
    window.dispatchEvent(new Event(SESSION_CHANGE));
  };
  window.addEventListener("storage", event => {
    if (event.key === SESSION_EPOCH && event.newValue !== epoch) changed();
  });
  if (channel) channel.onmessage = event => {
    if (typeof event.data?.epoch === "string" && event.data.epoch !== epoch) changed();
  };
}
