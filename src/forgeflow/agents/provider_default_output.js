// Pi supplies an internal maxTokens value even when models.json omits it.
// Remove only the two completion-limit fields at the final provider boundary.
// The checked build-copy provider patch also reaches compaction requests.
// This fixed extension runs alongside --no-extensions (no auto-discovery).
import { appendFileSync } from "node:fs";

export default function providerDefaultOutput(_pi) {
  let requestIndex = 0;
  globalThis.__forgeflowProviderDefaultOutput = (original, model) => {
    if (model?.provider !== "nebius") return original;
    // Fail closed, rather than silently falling back to a capped request.
    if (!original || typeof original !== "object" || Array.isArray(original)) {
      process.stderr.write("ForgeFlow: invalid provider payload for default output policy.\n");
      process.exit(78);
    }
    const payload = { ...original };
    delete payload.max_tokens;
    delete payload.max_completion_tokens;
    requestIndex += 1;
    try {
      appendFileSync(
        process.env.FORGEFLOW_OUTPUT_POLICY_AUDIT_PATH
          || "/home/forgeflow/logs/provider-request-policy.jsonl",
        JSON.stringify({
          request_index: requestIndex,
          policy: "provider_default",
          policy_version: "all-requests-v2",
          max_tokens_present: Object.hasOwn(payload, "max_tokens"),
          max_completion_tokens_present: Object.hasOwn(payload, "max_completion_tokens"),
        }) + "\n",
        { mode: 0o600 },
      );
    } catch {
      process.stderr.write("ForgeFlow: unable to record provider output policy.\n");
      process.exit(78);
    }
    return payload;
  };
}
