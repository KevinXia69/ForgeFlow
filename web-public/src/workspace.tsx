import { createContext, useContext } from "react";
export type WorkspaceIdentity = { scope_id: string; display_name: string; deployment: string };
export const WorkspaceContext = createContext<WorkspaceIdentity>({ scope_id: "", display_name: "", deployment: "local" });
export const useWorkspace = () => useContext(WorkspaceContext);
/** URL state supports browser history, direct links, and reload. */
export function navigate(hash: string) { if (window.location.hash !== hash) window.location.hash = hash; }
export function caseRoute(id: string, mode?: "chat" | "evidence") {
  const query = new URLSearchParams({ case: id });
  if (mode) query.set(mode, "1");
  return `#${query}`;
}
