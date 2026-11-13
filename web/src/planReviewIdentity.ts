export type PlanAction = {
  action_id?: string; type?: string; target_model?: string; target_id?: number;
  quantity?: number; unit?: string; currency?: string; summary?: string;
  params?: Record<string, unknown>; preconditions?: unknown[];
  [key: string]: unknown;
};
export type PreparedPlan = {
  plan_id: number; plan_version?: number; action_digest?: string; plan_digest?: string;
  plan_hash?: string; snapshot_hash?: string; plan_state?: string; expires_at?: string;
  order: Record<string, unknown> & { name?: string; state?: string };
  task?: { attention: string; need: string };
  action_count?: number; line_count?: number; actions?: PlanAction[];
  lines?: Record<string, unknown>[]; facts?: Record<string, unknown>;
};

export function snapshotComplete(facts: Record<string, unknown> | undefined): boolean {
  if (!facts || facts.completeness === undefined || facts.completeness === null) return true;
  const completeness = facts.completeness;
  if (typeof completeness !== "object" || Array.isArray(completeness)) return false;
  const value = completeness as Record<string, unknown>;
  return value.complete !== false && value.truncated !== true && value.lines_complete !== false
    && !(Array.isArray(value.query_failures) ? value.query_failures.length : value.query_failures)
    && !(Array.isArray(value.missing_relations) ? value.missing_relations.length : value.missing_relations);
}

export function completeReview(plan: PreparedPlan | null): boolean {
  return !!plan && Number.isInteger(plan.plan_version) && Number(plan.plan_version) > 0
    && !!plan.action_digest && !!plan.plan_digest && !!plan.snapshot_hash
    && !!plan.order && typeof plan.order === "object"
    && Array.isArray(plan.actions) && plan.actions.length > 0
    && plan.actions.every(action => !!action && typeof action === "object" && !!action.action_id)
    && new Set(plan.actions.map(action => action.action_id)).size === plan.actions.length
    && plan.action_count === plan.actions.length
    && Array.isArray(plan.lines) && plan.line_count === plan.lines.length
    && plan.lines.every(line => !!line && typeof line === "object" && !Array.isArray(line))
    && !!plan.facts && typeof plan.facts === "object" && !Array.isArray(plan.facts) && snapshotComplete(plan.facts);
}

export function reviewCanApprove(plan: PreparedPlan | null): boolean {
  if (!completeReview(plan) || !plan || !["draft", "awaiting_approval"].includes(plan.plan_state ?? "")) return false;
  const expiry = Date.parse(plan.expires_at ?? "");
  return Number.isFinite(expiry) && expiry > Date.now();
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(
    Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, canonical(item)]));
  return value;
}

/** Compare every displayed fact too: unchanged action counts do not mean the reviewed plan is unchanged. */
export function reviewIdentity(plan: PreparedPlan): string {
  return JSON.stringify(canonical({ plan_id: plan.plan_id, plan_version: plan.plan_version,
    action_digest: plan.action_digest, plan_digest: plan.plan_digest, snapshot_hash: plan.snapshot_hash,
    plan_state: plan.plan_state, expires_at: plan.expires_at,
    actions: plan.actions, action_count: plan.action_count, order: plan.order,
    lines: plan.lines, line_count: plan.line_count, facts: plan.facts }));
}
