// Pi 0.85.1 registerTool / exec API. The Python CLI remains the sole planner.
// No model-controlled filesystem path or executable is accepted.
import * as filesystem from "node:fs/promises";
import { join } from "node:path";

const PYTHON = "/opt/forgeflow/planner-env/bin/python";
const ENTRYPOINT = "/opt/forgeflow/planner/plan.py";
const WORKSPACE = "/workspace";
const PREFIX = `${WORKSPACE}/forgeflow-planner-`;
const TIMEOUT_MS = 120_000;
const MAX_FILE_BYTES = 16 * 1024 * 1024;
const MAX_ERROR_BYTES = 16 * 1024;
const SCOPE_REVIEW_MESSAGE = "Selected sales orders omit active demand sharing the planning "
  + "products. Inspect the listed orders and reconcile them with the task scope. For any "
  + "additional task-authorized target-product orders, provide explicit budget caps before "
  + "retrying. Do not ignore other demand or change orders to bypass this check.";
const OUTPUT_FILES = ["request.json", "snapshot.json", "blueprint.json", "plan.json"];
const REQUIRED = [
  "target_product_code", "sales_order_ids", "anchor_date", "margin",
  "sales_order_budget_caps", "disruption",
];

// Plain JSON Schema is supported by the pinned Pi argument validator. Keeping
// the schema local avoids an extension dependency or a second solver runtime.
export const REQUEST_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: REQUIRED,
  properties: {
    target_product_code: { type: "string", minLength: 1 },
    sales_order_ids: {
      type: "array", minItems: 1, uniqueItems: true,
      items: { type: "integer", minimum: 1 },
      description: "Explicit existing Odoo sales order IDs in the repair scope.",
    },
    anchor_date: {
      type: "string", pattern: "^\\d{4}-\\d{2}-\\d{2}$",
      description: "YYYY-MM-DD planning origin established from the task and live dates.",
    },
    margin: {
      type: "number", minimum: 0, exclusiveMaximum: 1,
      description: "Required new-spend margin as a fraction, from the task policy.",
    },
    sales_order_budget_caps: {
      type: "object", minProperties: 1,
      additionalProperties: { type: "number", exclusiveMinimum: 0 },
      description: "Positive budget for every selected SO, keyed by its decimal ID.",
    },
    disruption: {
      anyOf: [
        {
          type: "object", additionalProperties: false,
          required: ["kind", "vendor_ref"],
          properties: {
            kind: { type: "string", const: "supplier_cancellation" },
            vendor_ref: { type: "string", minLength: 1 },
            offer_key: { type: "string", minLength: 1 },
          },
        },
        {
          type: "object", additionalProperties: false,
          required: ["kind", "workcenter_code"],
          properties: {
            kind: { type: "string", const: "workcenter_outage" },
            workcenter_code: { type: "string", minLength: 1 },
          },
        },
      ],
    },
  },
};

function need(condition, message) {
  if (!condition) throw new Error(`Invalid planning request: ${message}`);
}

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}

function exactKeys(value, keys) {
  return object(value) && Reflect.ownKeys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function text(value) {
  return typeof value === "string" && value.length > 0;
}

function finite(value) {
  return typeof value === "number" && Number.isFinite(value);
}

export function validateRequest(request) {
  // Independent of Pi schema validation: direct execute calls must reject
  // unknown fields before any directory, request file, or child process exists.
  need(exactKeys(request, REQUIRED), "use exactly the six documented fields.");
  need(text(request.target_product_code), "target_product_code must be nonempty text.");
  const ids = request.sales_order_ids;
  need(Array.isArray(ids) && ids.length > 0
    && ids.every((id) => Number.isSafeInteger(id) && id > 0)
    && new Set(ids).size === ids.length, "sales_order_ids must be unique positive integers.");
  const anchor = request.anchor_date;
  need(typeof anchor === "string" && /^\d{4}-\d{2}-\d{2}$/.test(anchor)
    && !anchor.startsWith("0000-"), "anchor_date must use YYYY-MM-DD.");
  const date = new Date(`${anchor}T00:00:00Z`);
  need(Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === anchor,
    "anchor_date must be a valid calendar date.");
  need(finite(request.margin) && request.margin >= 0 && request.margin < 1,
    "margin must be a number from zero up to, but excluding, one.");
  const caps = request.sales_order_budget_caps;
  need(exactKeys(caps, ids.map(String)),
    "provide one budget cap for every selected sales order, with no other keys.");
  need(Object.values(caps).every((cap) => finite(cap) && cap > 0),
    "budget caps must be positive finite numbers.");
  const disruption = request.disruption;
  need(object(disruption), "disruption must be an object.");
  if (disruption.kind === "supplier_cancellation") {
    const keys = Object.hasOwn(disruption, "offer_key")
      ? ["kind", "vendor_ref", "offer_key"] : ["kind", "vendor_ref"];
    need(exactKeys(disruption, keys) && text(disruption.vendor_ref)
      && (!Object.hasOwn(disruption, "offer_key") || text(disruption.offer_key)),
    "supplier disruption accepts vendor_ref and optional offer_key only.");
  } else {
    need(disruption.kind === "workcenter_outage"
      && exactKeys(disruption, ["kind", "workcenter_code"])
      && text(disruption.workcenter_code),
    "workcenter disruption requires kind and workcenter_code only.");
  }
  const encoded = JSON.stringify(request);
  need(Buffer.byteLength(encoded, "utf8") <= 64 * 1024, "request exceeds 64 KiB.");
  return JSON.parse(encoded);
}

class PlannerFailure extends Error {}

function failure(code, workspacePath, missingSalesOrderIds) {
  // Never include child stdout/stderr, arbitrary exception text, or user values.
  const result = { ok: false, error: code, odoo_writes: false };
  if (workspacePath) result.workspace_path = workspacePath;
  if (missingSalesOrderIds) {
    result.details = { missing_sales_order_ids: missingSalesOrderIds };
    result.message = SCOPE_REVIEW_MESSAGE;
  }
  return new PlannerFailure(JSON.stringify(result));
}

function scopeFeedback(stdout) {
  // Accept only one bounded, exact protocol object from the fixed Python CLI.
  // Never recover JSON from surrounding logs or forward a Python error string.
  if (typeof stdout !== "string" || Buffer.byteLength(stdout, "utf8") > MAX_ERROR_BYTES) {
    return undefined;
  }
  let summary;
  try {
    summary = JSON.parse(stdout);
  } catch {
    return undefined;
  }
  if (!exactKeys(summary, ["schema_version", "ok", "odoo_writes", "error_code", "details"])
    || summary.schema_version !== 1 || summary.ok !== false || summary.odoo_writes !== false
    || summary.error_code !== "unselected_active_demand"
    || !exactKeys(summary.details, ["missing_sales_order_ids"])) {
    return undefined;
  }
  const ids = summary.details.missing_sales_order_ids;
  if (!Array.isArray(ids) || ids.length === 0 || ids.length > 500
    || !ids.every((id, index) => Number.isSafeInteger(id) && id > 0
      && (index === 0 || id > ids[index - 1]))) {
    return undefined;
  }
  return [...ids];
}

async function checkedArtifacts(io, output) {
  const directory = await io.lstat(output);
  if (!directory.isDirectory() || directory.isSymbolicLink()) throw Error("Invalid output");
  const paths = {};
  for (const name of OUTPUT_FILES) {
    const path = join(output, name);
    const stat = await io.lstat(path);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_FILE_BYTES) {
      throw Error("Invalid artifact");
    }
    paths[name.replace(".json", "_path")] = path;
  }
  return paths;
}

// The optional I/O implementation is for deterministic tests; registration
// always uses Node's filesystem and the fixed paths above.
export function createPlannerTool(pi, io = filesystem, feedbackMode = "generic") {
  if (feedbackMode !== "generic" && feedbackMode !== "structured-v1") {
    throw new Error("Unsupported planner feedback mode.");
  }
  return {
    name: "plan_recovery",
    label: "Plan recovery (read only)",
    description: "Compute a recovery plan for explicitly selected existing sales orders after "
      + "a supplier cancellation or workcenter outage. Reads current Odoo facts and runs the "
      + "fixed CP-SAT solver. Supply product code, order IDs, date anchor, margin, budgets and "
      + "disruption identity from the task or live records. Returns a compact objective/spend "
      + "summary and paths to the request, snapshot, blueprint and plan. Does not change ERP.",
    promptSnippet: "Read live Odoo data and compute a read-only recovery plan with CP-SAT.",
    promptGuidelines: [
      "Use explicit task policy and live identities for plan_recovery; inspect its plan before "
        + "applying ERP changes, then verify the resulting business state.",
      "The plan file also contains solver trace: parse only its top-level .plan field and "
        + "the snapshot IDs needed for execution instead of reading the entire trace.",
    ],
    parameters: REQUEST_SCHEMA,
    executionMode: "sequential",
    prepareArguments: validateRequest,
    async execute(_toolCallId, params, signal) {
      const request = validateRequest(params);
      if (signal?.aborted) throw failure("aborted");
      let workspacePath;
      try {
        workspacePath = await io.mkdtemp(PREFIX);
        // mkdtemp creates a private directory; reject a substituted path.
        if (!workspacePath.startsWith(PREFIX) || workspacePath.includes("/../")
          || workspacePath.slice(PREFIX.length).includes("/")) {
          throw Error("Invalid workspace");
        }
        await io.chmod(workspacePath, 0o700);
        const requestPath = join(workspacePath, "request.json");
        const output = join(workspacePath, "output");
        await io.writeFile(requestPath, `${JSON.stringify(request)}\n`, {
          encoding: "utf8", mode: 0o600, flag: "wx",
        });
        if (signal?.aborted) throw failure("aborted", workspacePath);
        const result = await pi.exec(PYTHON, [
          "-B", ENTRYPOINT, "--request", requestPath, "--output", output,
        ], { cwd: WORKSPACE, signal, timeout: TIMEOUT_MS });
        if (signal?.aborted) throw failure("aborted", workspacePath);
        if (result.killed) throw failure("planner_timed_out_or_cancelled", workspacePath);
        if (result.code !== 0) {
          const missing = feedbackMode === "structured-v1" && result.code === 2
            ? scopeFeedback(result.stdout) : undefined;
          if (missing) throw failure("unselected_active_demand", workspacePath, missing);
          throw failure("planner_failed", workspacePath);
        }
        // The fixed CLI prints one JSON summary. Never copy arbitrary stdout.
        if (typeof result.stdout !== "string" || result.stdout.length > 65_536) {
          throw Error("Invalid planner summary");
        }
        const lines = result.stdout.trim().split("\n");
        const summary = JSON.parse(lines.at(-1));
        if (summary.ok !== true || summary.odoo_writes !== false
          || summary.plan_path !== join(output, "plan.json")
          || !finite(summary.objective_value) || summary.objective_value < 0
          || !finite(summary.expected_new_spend) || summary.expected_new_spend < 0) {
          throw Error("Invalid planner summary");
        }
        const paths = await checkedArtifacts(io, output);
        const safe = {
          ok: true, odoo_writes: false, execution_validated: false,
          objective_value: summary.objective_value,
          expected_new_spend: summary.expected_new_spend,
          plan_json_pointer: "/plan",
          inspection_hint: "Parse plan.json and extract its .plan field; use only required "
            + "snapshot IDs. The full plan file also contains solver trace.",
          ...paths,
        };
        return { content: [{ type: "text", text: JSON.stringify(safe) }], details: safe };
      } catch (error) {
        // Preserve only our own fixed errors, never Python/RPC/OS exception text.
        if (error instanceof PlannerFailure) throw error;
        throw failure("planner_unavailable_or_invalid_result", workspacePath);
      }
    },
  };
}

export default function nativePlanner(pi) {
  pi.registerTool(createPlannerTool(pi));
}
