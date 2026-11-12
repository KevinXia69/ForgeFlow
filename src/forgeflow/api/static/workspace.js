// Pin every private request to the identity that opened this document.
const sessionChange = "forgeflow.session-change";
let scope = "", retired = false, verification = 0;

function retire() {
  if (retired) return;
  retired = true;
  // Hide private data even when offline navigation cannot finish. A fresh
  // document discards in-flight callbacks, event streams and unsent inputs.
  document.body.hidden = true;
  document.body.inert = true;
  history.replaceState(null, "", location.pathname);
  location.reload();
}

export async function workspaceFetch(path, options = {}) {
  if (retired) throw new Error("workspace_retired");
  const login = path === "/api/auth/session" && options.method === "POST";
  const bound = !login && path !== "/api/config";
  if (bound && !scope && path !== "/api/auth/me") {
    throw Object.assign(new Error("login_required"), {status: 401});
  }
  const headers = new Headers(options.headers);
  if (bound && scope) headers.set("X-ForgeFlow-Workspace", scope);
  const response = await fetch(path, {...options, credentials: "same-origin", headers});
  if (bound && scope && (response.status === 401 || response.headers.get("X-ForgeFlow-Workspace-Changed") === "1")) {
    retire();
    throw Object.assign(new Error("workspace_retired"), {status: response.status});
  }
  if (retired) throw new Error("workspace_retired");
  if (login && response.ok) {
    try { localStorage.setItem(sessionChange, crypto.randomUUID()); } catch { /* Focus also rechecks. */ }
    if (scope) { retire(); throw new Error("workspace_retired"); }
  }
  return response;
}

export async function bindWorkspace() {
  const response = await workspaceFetch("/api/auth/me");
  if (!response.ok) throw Object.assign(new Error("login_required"), {status: response.status});
  const identity = await response.json();
  if (scope && scope !== identity.scope_id) retire();
  if (!identity.scope_id || retired) throw new Error("workspace_unavailable");
  scope = identity.scope_id;
  return scope;
}

export function workspaceEvents(path) {
  if (!scope || retired) throw new Error("workspace_unavailable");
  return path + "?workspace_scope=" + encodeURIComponent(scope);
}

async function verify(changed = false) {
  if (!scope || retired) return;
  if (changed) {
    // The signal does not say who signed in. Conceal the old page until its
    // own cookie/scope pair verifies, even in a background or offline tab.
    document.body.hidden = true;
    document.body.inert = true;
  }
  const sequence = ++verification;
  try {
    const response = await workspaceFetch("/api/auth/me");
    if (!response.ok) return;
    const identity = await response.json();
    if (retired || sequence !== verification) return;
    if (identity.scope_id !== scope) { retire(); return; }
    document.body.hidden = false;
    document.body.inert = false;
  } catch { /* Stay concealed after a signal until identity can be verified. */ }
}
function verifyVisible() { if (!document.hidden) void verify(); }
window.addEventListener("storage", event => {
  if (event.key === sessionChange) void verify(true);
});
window.addEventListener("online", () => { void verify(); });
window.addEventListener("focus", verifyVisible);
window.addEventListener("pageshow", verifyVisible);
document.addEventListener("visibilitychange", verifyVisible);
