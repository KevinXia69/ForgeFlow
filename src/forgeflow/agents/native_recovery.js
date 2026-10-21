// Fixed Pi 0.85.1 recovery interface. Business validation and journaled writes
// remain in recover.py; neither executable nor artifact paths are model inputs.
import * as filesystem from "node:fs/promises";
import { join } from "node:path";
import { REQUEST_SCHEMA, validateRequest } from "./native_planner.js";

const PYTHON = "/opt/forgeflow/planner-env/bin/python";
const ENTRYPOINT = "/opt/forgeflow/planner/recover.py";
const WORKSPACE = "/workspace";
const PREFIX = `${WORKSPACE}/forgeflow-recovery-`;
const MAX_SUMMARY_BYTES = 16 * 1024;
const PLAN_ID = /^[0-9a-f]{64}$/;
const STOP_MESSAGE = "Execution stopped; ERP state may have changed. Preserve and reuse "
  + "the same plan_id for controlled journal/readback recovery. Do not blindly retry, "
  + "replan, or repeat ERP writes. Do not claim recovery or external benchmark success.";

export const EXECUTE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["plan_id"],
  properties: {
    plan_id: {
      type: "string", minLength: 64, maxLength: 64, pattern: "^[0-9a-f]{64}$",
      description: "Exact plan_id returned by prepare_recovery; never a filesystem path.",
    },
  },
};

function exactKeys(value, keys) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value))
    && Reflect.ownKeys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function planId(value) {
  return typeof value === "string" && value.length === 64 && PLAN_ID.test(value);
}

function count(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

class RecoveryFailure extends Error {}

function failure(phase, dispatched = false, identity) {
  // The two locally defined codes are the complete exposed error allowlist.
  // A started apply may have committed before the response was lost. Its true
  // flag conservatively denotes possible writes, not confirmed successful work.
  const safe = {
    ok: false,
    error_code: phase === "prepare" ? "recovery_prepare_failed" : "recovery_execute_stopped",
    odoo_writes: phase === "apply" && dispatched,
  };
  if (phase === "apply") {
    if (planId(identity)) safe.plan_id = identity;
    safe.message = dispatched ? STOP_MESSAGE : "Execution did not start through this call. "
      + "Preserve any existing plan_id; do not blindly retry or replan after an earlier stop.";
  }
  return new RecoveryFailure(JSON.stringify(safe));
}

function prepareParams(value) {
  try {
    return validateRequest(value);
  } catch {
    throw failure("prepare");
  }
}

function executeParams(value) {
  try {
    if (!exactKeys(value, ["plan_id"]) || !planId(value.plan_id)) throw Error();
    return { plan_id: value.plan_id };
  } catch {
    throw failure("apply");
  }
}

function parseSummary(stdout) {
  // Parse one entire, bounded JSON object. Never salvage a final log line,
  // forward a child exception, or expose unrecognized fields from stdout.
  if (typeof stdout !== "string" || Buffer.byteLength(stdout, "utf8") > MAX_SUMMARY_BYTES
    || !/^[\x09\x0a\x0d\x20-\x7e]*$/.test(stdout)) throw Error();
  return JSON.parse(stdout);
}

const PREPARE_ERROR_CODES = new Set([
  "plan_rejected", "invalid_field", "scope_held", "executor_busy", "store_corrupt",
  "internal_error", "unavailable", "bad_response", "plan_binding_changed", "prepare_rejected",
]);
const CHANGE_SET_REASONS = new Set([
  "change_set_binding_mismatch", "change_set_supply_quantity_mismatch",
  "change_set_existing_offer_ambiguous", "change_set_mapping_unproven",
  "change_set_origin_capacity_mismatch",
]);
const PREPARE_REASONS = new Set([
  "task_contract_mismatch", "plan_contract_violation", "change_set_rejected",
  "live_input_rejected", "unselected_active_demand", "broken_offer_ambiguous",
  "broken_offer_not_found", "unfinished_execution_requires_original_plan",
  "another_recovery_operation_running", "recovery_bundle_required",
  "policy_file_untrusted", "source_bytes_changed", "source_file_missing",
  "invalid_source_manifest", "details_unavailable",
  "change_set_binding_mismatch", "change_set_supply_quantity_mismatch",
  "change_set_existing_offer_ambiguous", "change_set_mapping_unproven",
  "change_set_origin_capacity_mismatch",
]);

function preparedFailure(stdout) {
  const value = parseSummary(stdout);
  if (!exactKeys(value, ["ok", "error_code", "odoo_writes", "feedback"])
    || value.ok !== false || value.error_code !== "recovery_prepare_failed"
    || value.odoo_writes !== false
    || !exactKeys(value.feedback, ["code", "reason", "missing_sales_order_ids"])) throw Error();
  const feedback = value.feedback, ids = feedback.missing_sales_order_ids;
  if (!PREPARE_ERROR_CODES.has(feedback.code) || !PREPARE_REASONS.has(feedback.reason)
    || (CHANGE_SET_REASONS.has(feedback.reason) && feedback.code !== "plan_rejected")
    || !Array.isArray(ids) || ids.length > 20 || new Set(ids).size !== ids.length
    || !ids.every(id => Number.isSafeInteger(id) && id > 0)
    || (ids.length && (feedback.code !== "plan_rejected"
      || feedback.reason !== "unselected_active_demand"))) throw Error();
  return new RecoveryFailure(JSON.stringify(value));
}

function preparedSummary(stdout) {
  const value = parseSummary(stdout);
  if (!exactKeys(value, ["ok", "odoo_writes", "plan_id", "operation_counts",
    "step_count", "expected_new_spend"])
    || value.ok !== true || value.odoo_writes !== false || !planId(value.plan_id)
    || !exactKeys(value.operation_counts, ["keep", "update", "cancel", "create"])
    || !Object.values(value.operation_counts).every(count) || !count(value.step_count)
    || typeof value.expected_new_spend !== "number" || !Number.isFinite(value.expected_new_spend)
    || value.expected_new_spend < 0) throw Error();
  return value;
}

function appliedSummary(stdout, identity) {
  const value = parseSummary(stdout);
  if (!exactKeys(value, ["ok", "odoo_writes", "plan_id", "verified_steps",
    "execution_validated", "benchmark_success"])
    || value.ok !== true || value.odoo_writes !== true || value.plan_id !== identity
    || !count(value.verified_steps) || value.execution_validated !== true
    || value.benchmark_success !== null) throw Error();
  return value;
}

function result(summary) {
  return { content: [{ type: "text", text: JSON.stringify(summary) }], details: summary };
}

// Filesystem substitution exists only for deterministic tests. Registration
// always uses Node's filesystem; both tools use the same fixed Python entrypoint.
export function createRecoveryTools(pi, io = filesystem) {
  const prepare = {
    name: "prepare_recovery",
    label: "Prepare recovery change set (read only)",
    description: "Prepare a recovery plan from current Odoo records and explicit business "
      + "policy for supplier cancellation or workcenter outage. The solver produces a total "
      + "target state; the fixed executor computes its difference from existing commitments, "
      + "binds a read-only execution baseline, and returns a plan_id and change counts. "
      + "This tool makes no ERP changes. Use execute_recovery with that exact plan_id next.",
    promptSnippet: "Prepare a bound recovery change set, then execute its exact plan_id.",
    promptGuidelines: [
      "Call prepare_recovery with explicit task policy and live identities, then "
        + "execute_recovery with its returned plan_id to perform the autonomous repair.",
      "Solver output describes the total target state, not additive order creation. "
        + "The fixed executor derives changes and preserves valid existing commitments; "
        + "do not duplicate its actions with manual ERP writes.",
      "Recovery changes planning documents only. Do not fabricate receipts, delivered "
        + "goods, manufacturing completion, or other physical events.",
      "Execution validation is a live readback check; only the external verifier can "
        + "establish benchmark success. Do not claim it passed from these tools alone.",
    ],
    parameters: REQUEST_SCHEMA,
    executionMode: "sequential",
    prepareArguments: prepareParams,
    async execute(_toolCallId, params, signal) {
      const request = prepareParams(params);
      if (signal?.aborted) throw failure("prepare");
      try {
        const directory = await io.mkdtemp(PREFIX);
        if (typeof directory !== "string" || !directory.startsWith(PREFIX)) throw Error();
        const suffix = directory.slice(PREFIX.length);
        if (suffix.length < 6 || suffix.length > 64 || /[^A-Za-z0-9_-]/.test(suffix)) throw Error();
        if (signal?.aborted) throw failure("prepare");
        const stat = await io.lstat(directory);
        if (!stat.isDirectory() || stat.isSymbolicLink()) throw Error();
        await io.chmod(directory, 0o700);
        if (signal?.aborted) throw failure("prepare");
        const path = join(directory, "request.json");
        await io.writeFile(path, `${JSON.stringify(request)}\n`, {
          encoding: "utf8", mode: 0o600, flag: "wx",
        });
        if (signal?.aborted) throw failure("prepare");
        const response = await pi.exec(PYTHON, [
          "-B", ENTRYPOINT, "prepare", "--request", path,
        ], { cwd: WORKSPACE, signal, timeout: 120_000 });
        if (signal?.aborted || response.killed !== false) throw failure("prepare");
        if (response.code === 2) throw preparedFailure(response.stdout);
        if (response.code !== 0) throw failure("prepare");
        return result(preparedSummary(response.stdout));
      } catch (error) {
        if (error instanceof RecoveryFailure) throw error;
        throw failure("prepare");
      }
    },
  };
  const apply = {
    name: "execute_recovery",
    label: "Execute prepared recovery change set",
    description: "Apply the exact plan_id returned by prepare_recovery through the fixed "
      + "journaled ERP executor and whole-scope readback guards. Modifies planning documents "
      + "only. A stop can leave partial changes: preserve that plan_id for controlled "
      + "recovery; do not blindly retry, replan, or manually repeat writes. A successful "
      + "result validates execution readback and does not assert external benchmark success.",
    promptSnippet: "Execute a prepared plan_id using the fixed journal and readback guards.",
    promptGuidelines: [
      "Use only the exact plan_id returned by prepare_recovery. On a stop, preserve it "
        + "for controlled journal/readback recovery; do not blindly retry or create a new plan.",
      "Do not mark receipts or manufacturing done to make a plan appear fulfilled. "
        + "Do not claim external benchmark success from execution_validated alone.",
    ],
    parameters: EXECUTE_SCHEMA,
    executionMode: "sequential",
    prepareArguments: executeParams,
    async execute(_toolCallId, params, signal) {
      const { plan_id: identity } = executeParams(params);
      if (signal?.aborted) throw failure("apply", false, identity);
      let dispatched = false;
      try {
        dispatched = true;
        const response = await pi.exec(PYTHON, [
          "-B", ENTRYPOINT, "apply", "--autonomous", "--plan-id", identity,
        ], { cwd: WORKSPACE, signal, timeout: 300_000 });
        if (signal?.aborted || response.killed !== false || response.code !== 0) {
          throw failure("apply", true, identity);
        }
        return result(appliedSummary(response.stdout, identity));
      } catch (error) {
        if (error instanceof RecoveryFailure) throw error;
        throw failure("apply", dispatched, identity);
      }
    },
  };
  return [prepare, apply];
}

export default function nativeRecovery(pi) {
  for (const tool of createRecoveryTools(pi)) pi.registerTool(tool);
}
