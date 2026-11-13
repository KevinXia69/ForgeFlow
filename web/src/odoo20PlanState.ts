type ApprovalAttention = { attention: string; need: string };
type PlanTaskState = ApprovalAttention & { state: string; user_state?: string };

export type PlanActionState = "loading" | "awaiting_approval" | "executing" | "completed" | "closed" | "needs_action" | "read_only";

/** Persisted task state takes precedence over the page's last approval click. */
export function planActionState(
  attention: ApprovalAttention | undefined,
  task: PlanTaskState | undefined,
  approvedHere: boolean,
  tasksLoaded: boolean,
): PlanActionState {
  if (task?.state === "completed") return "completed";
  if (["cancelled", "closed"].includes(task?.state ?? "")) return "closed";
  if (["blocked", "failed"].includes(task?.state ?? "") || task?.user_state === "needs_action") return "needs_action";
  if (approvedHere || ["executing", "verifying"].includes(task?.state ?? "")) return "executing";
  if (!tasksLoaded) return "loading";
  const currentAttention = task ?? attention;
  if (currentAttention?.attention === "open" && currentAttention.need === "approval") return "awaiting_approval";
  return "read_only";
}
