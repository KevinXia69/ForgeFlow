// Pi 0.85.1 / 08dc60bc52d89d6823a9738cc90b1916e5e446e5 public ExtensionAPI.
// Observe the bound registry, not this extension's files or anticipated tools.
import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, openSync, writeFileSync } from "node:fs";

export const AUDIT_PATH = "/home/forgeflow/logs/tool-audit.jsonl";
const VERSION = "pi-runtime-tools-v1";
const KNOWN_NAMES = new Set([
  "read", "bash", "edit", "write", "grep", "find", "ls", "powershell",
  "plan_recovery", "prepare_recovery", "execute_recovery",
]);
const EXTENSIONS = new Map([
  ["/opt/forgeflow/native_planner.js", "native_planner"],
  ["/opt/forgeflow/native_planner_feedback.js", "native_planner_feedback"],
  ["/opt/forgeflow/native_recovery.js", "native_recovery"],
]);

function canonical(value, seen = new Set(), depth = 0) {
  if (depth > 32) throw Error();
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "object" || seen.has(value)) throw Error();
  seen.add(value);
  let result;
  if (Array.isArray(value)) {
    if (value.length > 10000) throw Error();
    result = value.map(item => canonical(item, seen, depth + 1));
  } else {
    if (![Object.prototype, null].includes(Object.getPrototypeOf(value))) throw Error();
    result = Object.create(null);
    for (const key of Object.keys(value).sort()) result[key] = canonical(value[key], seen, depth + 1);
  }
  seen.delete(value);
  return result;
}

export function schemaHash(value) {
  try {
    const serialized = JSON.stringify(canonical(value));
    if (Buffer.byteLength(serialized) > 1024 * 1024) return null;
    return createHash("sha256").update(serialized).digest("hex");
  } catch { return null; }
}

function textHash(value) {
  if (typeof value !== "string" || Buffer.byteLength(value) > 1024 * 1024) return null;
  return createHash("sha256").update(value).digest("hex");
}

function nameRecord(name) {
  if (typeof name !== "string" || !name || name.length > 256) throw Error();
  return { name: KNOWN_NAMES.has(name) ? name : null, name_sha256: textHash(name) };
}

function sourceLabel(tool) {
  const source = tool.sourceInfo;
  if (source?.source === "builtin" && source.path === `<builtin:${tool.name}>`
    && KNOWN_NAMES.has(tool.name)) return "pi_builtin";
  return EXTENSIONS.get(source?.path) ?? "unrecognized";
}

export function registrySnapshot(pi) {
  try {
    const all = pi.getAllTools(), active = pi.getActiveTools();
    if (!Array.isArray(all) || !Array.isArray(active) || all.length > 128 || active.length > 128) throw Error();
    const names = all.map(tool => tool.name);
    if (new Set(names).size !== names.length || new Set(active).size !== active.length) throw Error();
    const registered = all.map(tool => ({
      ...nameRecord(tool.name), schema_sha256: schemaHash(tool.parameters),
      definition_sha256: schemaHash({
        parameters: tool.parameters, description: tool.description,
        promptGuidelines: tool.promptGuidelines ?? [],
      }),
      source: sourceLabel(tool), active: active.includes(tool.name),
    }));
    const consistent = active.every(name => names.includes(name))
      && registered.every(tool => tool.schema_sha256 && tool.definition_sha256);
    return {
      status: consistent ? "complete" : "incomplete", reason: consistent ? null : "registry_inconsistent",
      registered_tools: registered, active_tools: active.map(nameRecord),
      registered_count: all.length, active_count: active.length,
    };
  } catch {
    return { status: "unavailable", reason: "registry_unavailable", registered_tools: null,
      active_tools: null, registered_count: null, active_count: null };
  }
}

function writeAudit(record) {
  // Fixed destination, bounded size, no symlink following or arbitrary output text.
  const descriptor = openSync(AUDIT_PATH,
    constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW, 0o600);
  try {
    const stat = fstatSync(descriptor), line = JSON.stringify(record) + "\n";
    if (!stat.isFile() || stat.nlink !== 1 || stat.size + Buffer.byteLength(line) > 4 * 1024 * 1024) throw Error();
    writeFileSync(descriptor, line);
  } finally { closeSync(descriptor); }
}

// Writer/clock substitution is only for controlled tests; registration uses the
// fixed sink above. Nothing is sent to the model or returned to event handlers.
export function installToolAudit(pi, write = writeAudit, now = () => new Date().toISOString()) {
  let index = 0, inputSnapshotIndex = null;
  function capture(phase, input) {
    index += 1;
    const record = {
      schema_version: 1, version: VERSION, index, phase, observed_at: now(),
      observation: "pi_runtime_registry", provider_receipt_confirmed: false,
      ...registrySnapshot(pi), input_snapshot_index: inputSnapshotIndex,
    };
    if (input) {
      inputSnapshotIndex = index;
      record.input_snapshot_index = index;
      record.input = {
        observation: "before_agent_start", prompt_sha256: textHash(input.prompt),
        system_prompt_sha256: textHash(input.systemPrompt),
        context_files_count: Array.isArray(input.systemPromptOptions?.contextFiles)
          ? input.systemPromptOptions.contextFiles.length : null,
        skills_count: Array.isArray(input.systemPromptOptions?.skills)
          ? input.systemPromptOptions.skills.length : null,
      };
    }
    try { write(record); } catch {
      process.stderr.write("ForgeFlow: unable to record runtime tool audit.\n");
      process.exit(78);
    }
  }
  pi.on("session_start", () => { capture("session_start"); });
  pi.on("before_agent_start", event => { capture("before_agent_start", event); });
  pi.on("before_provider_request", () => { capture("before_provider_request"); });
}

export default function toolAudit(pi) { installToolAudit(pi); }
