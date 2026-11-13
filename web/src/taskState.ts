// The seven user-visible task states from the control layer.  Internal states
// (queued, result_ready, waiting_event, closed, ...) never reach the screen: the
// server projects them through `user_state_for`, and this table is the only place
// a state becomes a label.
import type { MessageKey } from "./i18n";

export const USER_STATES = [
  "investigating",
  "needs_input",
  "awaiting_approval",
  "executing",
  "verifying",
  "completed",
  "needs_action",
] as const;

export type UserState = (typeof USER_STATES)[number];

const STATE_KEY = {
  investigating: "task.state.investigating",
  needs_input: "task.state.needsInput",
  awaiting_approval: "task.state.awaitingApproval",
  executing: "task.state.executing",
  verifying: "task.state.verifying",
  completed: "task.state.completed",
  needs_action: "task.state.needsAction",
} as const satisfies Record<UserState, MessageKey>;

const STATE_TONE: Record<UserState, string> = {
  investigating: "neutral",
  needs_input: "attention",
  awaiting_approval: "attention",
  executing: "active",
  verifying: "active",
  completed: "done",
  needs_action: "alert",
};

const NEED_KEY = {
  clarification: "task.need.clarification",
  fact: "task.need.fact",
  approval: "task.need.approval",
  failure: "task.need.failure",
  rule: "task.need.rule",
  budget: "task.need.budget",
  result_unknown: "task.need.resultUnknown",
  system: "task.need.system",
} as const satisfies Record<string, MessageKey>;

const ATTENTION_KEY = {
  open: "task.attention.open",
  answered: "task.attention.answered",
  dismissed: "task.attention.dismissed",
} as const satisfies Record<string, MessageKey>;

export function isUserState(value: unknown): value is UserState {
  return typeof value === "string" && (USER_STATES as readonly string[]).includes(value);
}

/** Label key for a task. Prefers the server's user_state, falls back to a mapping. */
export function taskStateKey(state: string | null | undefined): MessageKey {
  if (isUserState(state)) return STATE_KEY[state];
  return STATE_KEY[legacyUserState(state)];
}

/** Only for older payloads that predate `user_state`. */
function legacyUserState(state: string | null | undefined): UserState {
  switch (state) {
    case "waiting_input":
      return "needs_input";
    case "awaiting_approval":
      return "awaiting_approval";
    case "executing":
      return "executing";
    case "verifying":
      return "verifying";
    case "completed":
    case "cancelled":
    case "closed":
      return "completed";
    case "blocked":
    case "failed":
    case "needs_attention":
      return "needs_action";
    default:
      return "investigating";
  }
}

export function taskStateTone(state: string | null | undefined): string {
  return STATE_TONE[isUserState(state) ? state : legacyUserState(state)];
}


//: The product-case page keeps a richer internal machine (preparation, candidate
//: selection, revision, expiry).  Its **outward** state is still one of the seven
//: task states; the finer value is a phase, not a competing vocabulary.
const CASE_USER_STATE: Record<string, UserState> = {
  preparing: "investigating",
  revision_requested: "needs_input",
  awaiting_approval: "awaiting_approval",
  awaiting_selection: "awaiting_approval",
  executing: "executing",
  execution_unknown: "needs_action",
  succeeded: "completed",
  failed: "needs_action",
  rejected: "needs_action",
  expired: "needs_action",
};

/** Business state of a product case expressed in the shared seven states. */
export function caseUserState(state: string | null | undefined): UserState {
  return CASE_USER_STATE[state ?? ""] ?? "investigating";
}

export function attentionNeedKey(need: string | null | undefined): MessageKey {
  const key = need ? NEED_KEY[need as keyof typeof NEED_KEY] : undefined;
  return key ?? "task.need.fact";
}

export function attentionStateKey(state: string | null | undefined): MessageKey {
  const key = state ? ATTENTION_KEY[state as keyof typeof ATTENTION_KEY] : undefined;
  return key ?? "task.attention.open";
}
