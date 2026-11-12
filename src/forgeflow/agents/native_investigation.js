// A single-agent tool surface. Trusted Python services own observations and admission.
import { REQUEST_SCHEMA } from "./native_planner.js";
import { randomUUID } from "node:crypto";
import { openSync, writeFileSync, closeSync, constants } from "node:fs";
const PYTHON = "/opt/forgeflow/planner-env/bin/python";
const ENTRY = "/opt/forgeflow/planner/investigate.py";
const object = (properties, required = Object.keys(properties)) => ({
  type: "object", additionalProperties: false, properties, required,
});
const string = { type: "string" };
const databaseId = {
  anyOf: [{ type: "integer", minimum: 1, maximum: 9007199254740991 },
    { type: "string", pattern: "^[1-9][0-9]*$" }],
  description: "Internal numeric database id from a returned record.id. Never use a business name (SO-1/PO-20) or observation_id.",
};
const queryArguments = object({
  entity: { type: "string", enum: ["supplier", "product", "purchase_order", "sales_order",
    "purchase_order_line", "sales_order_line", "supplier_offer", "bom", "bom_line", "order_policy"] },
  id: databaseId, order_id: databaseId,
  query: { type: "string", minLength: 1, maxLength: 128 },
  limit: { type: "integer", minimum: 1, maximum: 50 },
  cursor: { anyOf: [{ type: "string" }, { type: "null" }] },
}, []);
const reference = object({ field: string,
  observation_id: { type: "string", description: "Copy the exact observation_id from a query result, not its numeric id or business name." },
  source_field: { type: "string", enum: ["default_code", "id", "anchor_date", "margin", "task_type", "ref", "offer_key", "budget_cap"] } });
const draftRequest = { ...REQUEST_SCHEMA, required: [] };

export const TOOLS = [
  {
    name: "investigation_query", action: "query",
    description: "Read bounded business records or task policy. kinds: entities with entity "
      + "supplier/product/purchase_order/sales_order and query text; relations with entity and id; "
      + "record with entity and id; policy with optional order_id. Pagination uses limit/cursor. "
      + "id and order_id are numeric database IDs, never names such as SO-1 or PO-CANCEL. "
      + "Find names with entities+query, then use the returned id. "
      + "To find supplier_offer records, use relations on a supplier or product; offers cannot be searched with entities. "
      + "record also supports bom/bom_line; relations supports bom. Product relations include "
      + "active BoMs and component-use bom_line records; bom relations return lines and parent products. "
      + "Keep returned observation_id for field evidence. Query kind=policy with arguments={} to read "
      + "the global anchor_date, margin and task_type and their policy observation_id. "
      + "Query policy with order_id separately for that order's authorization and budget_cap; "
      + "an order_policy observation cannot prove global policy fields. Never infer anchor_date from "
      + "an order deadline or margin from prices. Re-read global policy in every new episode. "
      + "Read each order's policy for its authorized budget. For an authorized order, budget_cap=null or missing "
      + "means the spending authorization is missing, not unlimited. Stop with needs_clarification and ask "
      + "the authorized decision-maker for that order's budget cap. Never derive or recommend a budget cap "
      + "from supplier quotations, sales revenue or estimated recovery costs, and never remove an affected "
      + "order to avoid its missing budget. Do not keep guessing values in draft validation.",
    parameters: object({ kind: { type: "string", enum: ["entities", "relations", "record", "policy"] },
      arguments: queryArguments }),
  },
  {
    name: "validate_investigation_draft", action: "validate",
    description: "Check a draft before planning. request uses target_product_code, sales_order_ids, "
      + "anchor_date, margin, sales_order_budget_caps, disruption. Evidence is one reference per "
      + "leaf. sales_order_ids are integer database IDs; budget keys are those numeric IDs as strings. "
      + "Mappings: target_product_code->product.default_code; sales_order_ids.0->sales_order.id; "
      + "sales_order_budget_caps.<id>->order_policy.budget_cap; anchor_date/margin->policy fields; "
      + "disruption.kind->policy.task_type; disruption.vendor_ref->supplier.ref; optional "
      + "disruption.offer_key->supplier_offer.offer_key. supply_observation_id references the "
      + "disrupted purchase_order. vendor_ref/offer_key must identify the CANCELED supply, never replacement supply; "
      + "the solver chooses replacements. Pass draft and request as JSON objects, not JSON-encoded strings. "
      + "Omit optional candidates/unresolved when submitting a resolved draft. "
      + "Incomplete drafts return safe field issues. Read and correct "
      + "facts inside the original authorization. Never weaken policy to pass.",
    parameters: object({ draft: object({ request: draftRequest,
      evidence: { type: "array", items: reference, maxItems: 256 },
      supply_observation_id: string, reason: string,
      candidates: { type: "object",
        description: "Optional unresolved request-field paths mapped to arrays of observed observation_id values. Not vendor references or offer keys. Omit when resolved.",
        additionalProperties: { type: "array", items: string, maxItems: 20 } },
      unresolved: { type: "array", maxItems: 20, items: object({ field: string, reason: string }) },
    }, ["request", "evidence"]) }),
  },
  {
    name: "prepare_investigated_recovery", action: "prepare",
    description: "Prepare only the latest validated draft_id. Reads fresh ERP facts, checks "
      + "the unchanged contract, solves and binds a reviewable plan. Makes no ERP writes. "
      + "At most three attempts; only revise with new relevant evidence. prepared is not ERP acceptance.",
    parameters: object({ draft_id: string }),
  },
  {
    name: "investigation_status", action: "status",
    description: "Read the durable investigation status, issues and resource usage.",
    parameters: object({}),
  },
  {
    name: "finish_investigation", action: "finish",
    description: "Stop with a specific unresolved business question, unsupported scope, or technical "
      + "failure. Do not ask for facts the read tools can supply. Only the trusted prepare service "
      + "can declare prepared or proven infeasible. Clarification requires a concrete question. "
      + "An authorized order with null or missing budget_cap lacks spending authorization; it is not unlimited. "
      + "Ask the authorized decision-maker to provide that order's budget cap, without deriving or recommending "
      + "caps from quotes, revenue or estimated plan costs. Do not remove the order to bypass authorization. "
      + "After a trusted repeated-invalid-draft stop, one valid needs_clarification finish is allowed "
      + "within the original deadline; query, validate and prepare stay stopped. Keep reason within 200 "
      + "characters and include the specific question; a terminal clarification cannot be revised.",
    parameters: object({ status: { type: "string",
      enum: ["needs_clarification", "unsupported", "technical_failure"] },
    reason: { type: "string", minLength: 1, maxLength: 200,
      description: "Brief stop reason, at most 200 characters. Put the specific business question in question." },
    question: { type: "string", maxLength: 1200,
      description: "Specific clarification question, at most 1200 characters; empty for other stop statuses." } }),
  },
];

const TERMINAL = new Set(["prepared", "needs_clarification", "unsupported", "infeasible",
  "budget_exhausted", "technical_failure"]);

// Diagnostic only: business authority remains in service-owned state/artifacts.
export function createTerminalController({ save = () => {}, now = () => Date.now(), usage = () => null } = {}) {
  const state = { schema_version: 1, phase: "ordinary", terminal: null,
    first_terminal_at_ms: null, completed_at_ms: null, closing_provider_requests: 0,
    closing_finish_attempts: 0, usage_at_terminal: null, usage_at_stop: null };
  const emit = () => save(structuredClone(state));
  return {
    observe(value) {
      if (!TERMINAL.has(value?.status)) return;
      if (state.first_terminal_at_ms === null) {
        state.first_terminal_at_ms = now(); state.usage_at_terminal = usage();
      }
      state.terminal = { status: value.status, terminal: value.terminal, run_id: value.run_id };
      const eligible = state.phase === "ordinary" && value.closing_allowed === true
        && value.status === "technical_failure" && value.terminal?.stop_origin === "invalid_draft_guard"
        && value.terminal?.reason === "repeated_invalid_draft_without_new_evidence";
      state.phase = eligible ? "closing_pending" : "stopped";
      emit();
    },
    beforeProvider(admit = true) {
      if (state.phase === "ordinary") return true;
      if (state.phase === "closing_pending" && state.closing_provider_requests === 0) {
        if (admit) { state.closing_provider_requests = 1; state.phase = "closing_response"; emit(); }
        return true;
      }
      return false;
    },
    beforeTool(name, input) {
      if (state.phase === "ordinary") return true;
      if (["closing_pending", "closing_response"].includes(state.phase)
          && name === "finish_investigation" && input?.status === "needs_clarification"
          && state.closing_finish_attempts === 0) {
        state.closing_finish_attempts++; emit(); return true;
      }
      return false;
    },
    turnEnd() {
      if (state.phase === "ordinary" || state.phase === "closing_pending") return false;
      state.phase = "stopped"; state.completed_at_ms = now(); state.usage_at_stop = usage(); emit();
      return true;
    },
    snapshot() { return structuredClone(state); },
  };
}

function terminalAudit(value) {
  const fd = openSync("/home/forgeflow/logs/terminal-control.json",
    constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | constants.O_NOFOLLOW, 0o600);
  try { writeFileSync(fd, JSON.stringify(value) + "\n"); } finally { closeSync(fd); }
}

export function installInvestigationTerminal(pi, { save = terminalAudit,
  stop = () => new Promise(() => process.stdout.write("", () => process.exit(0))) } = {}) {
  const control = createTerminalController({ save, usage: () => globalThis.__forgeflowTokenSnapshot?.() ?? null });
  // Budget calls this before counting dispatch, independently of extension order.
  globalThis.__forgeflowInvestigationTerminal = control;
  pi.on("tool_call", event => {
    if (!control.beforeTool(event.toolName, event.input)) return {
      block: true, terminate: true, reason: "Trusted investigation has stopped; business actions stay closed.",
    };
  });
  pi.on("turn_end", async () => {
    if (control.snapshot().phase === "closing_pending") pi.setActiveTools(["finish_investigation"]);
    if (control.turnEnd()) await stop();
  });
  return control;
}

export async function invokeInvestigation(pi, action, params, signal, now = () => Date.now()) {
  const encoded = Buffer.from(JSON.stringify(params), "utf8").toString("base64");
  if (encoded.length > 80000 || signal?.aborted) throw Error("investigation_call_rejected");
  const deadline = now() + 180000;
  // Only an observation operation may retry transport loss. Its operation_id
  // and bytes stay identical, so the trusted service returns the original receipt.
  for (let attempt = 0; attempt < (action === "query" ? 2 : 1); attempt++) {
    let response;
    try {
      if (signal?.aborted || now() >= deadline) throw Error("investigation_response_unavailable");
      response = await pi.exec(PYTHON, ["-B", ENTRY, action, "--payload-base64", encoded],
        { cwd: "/workspace", signal, timeout: Math.max(1, deadline - now()) });
      if (signal?.aborted || response.killed !== false || typeof response.stdout !== "string" || !response.stdout.trim()
          || Buffer.byteLength(response.stdout, "utf8") > 65536) throw Error("investigation_response_unavailable");
    } catch {
      if (action === "query" && attempt === 0 && !signal?.aborted && now() < deadline) continue;
      throw Error("investigation_response_unavailable");
    }
    let value;
    try { value = JSON.parse(response.stdout); } catch { throw Error("invalid_investigation_response"); }
    if (!value || typeof value !== "object" || Array.isArray(value)
        || typeof value.status !== "string") throw Error("invalid_investigation_response");
    if (response.code !== 0) {
      if (action === "query" && attempt === 0 && !signal?.aborted && now() < deadline
          && value.status === "tool_error" && ["unavailable", "bad_response"].includes(value.code)) continue;
      throw Error(JSON.stringify(value));
    }
    return value;
  }
  throw Error("investigation_response_unavailable");
}

export function createInvestigationTools(pi, control = null) {
  return TOOLS.map(spec => ({
    name: spec.name, label: spec.name, description: spec.description,
    parameters: spec.parameters, executionMode: "sequential",
    async execute(_id, params, signal) {
      if (spec.action === "query") params = { ...params, operation_id: randomUUID() };
      const value = await invokeInvestigation(pi, spec.action, params, signal);
      control?.observe(value);
      return { content: [{ type: "text", text: JSON.stringify(value) }], details: value };
    },
  }));
}

export default function nativeInvestigation(pi) {
  const control = installInvestigationTerminal(pi);
  pi.on("session_start", async (_event, context) => {
    // A fresh Pi process on an already terminal episode must not dispatch even
    // one model request. Failure to read the trusted service is fail-closed.
    try { control.observe(await invokeInvestigation(pi, "status", {}, context?.signal)); }
    catch { process.exit(78); }
    if (control.snapshot().phase === "stopped") {
      control.turnEnd();
      await new Promise(() => process.stdout.write("", () => process.exit(0)));
    }
  });
  for (const tool of createInvestigationTools(pi, control)) pi.registerTool(tool);
}
