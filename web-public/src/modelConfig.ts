/**
 * Browser-local model API configuration.
 *
 * The key lives only in this browser's localStorage and is sent with each
 * assistant request. There is deliberately no server-side persistence here.
 */

export type ModelProfile =
  | "nemotron"
  | "openai"
  | "openrouter"
  | "gemini"
  | "custom";

export type ModelConfig = {
  profile: ModelProfile;
  base_url: string;
  model: string;
  api_key: string;
};

export type ModelProfilePreset = {
  label: string;
  base_url: string;
  model: string;
};

export const MODEL_STORAGE_KEY = "forgeflow.model";

export const MODEL_PROFILES: Record<ModelProfile, ModelProfilePreset> = {
  nemotron: {
    label: "Nemotron 3.5 Super VL",
    base_url: "https://api.tokenfactory.nebius.com/v1",
    model: "nvidia/Nemotron-3.5-Super-VL",
  },
  openai: {
    label: "OpenAI-compatible",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
  },
  openrouter: {
    label: "OpenRouter",
    base_url: "https://openrouter.ai/api/v1",
    model: "openai/gpt-4o-mini",
  },
  gemini: {
    label: "Gemini-compatible",
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai/",
    model: "gemini-3-flash-preview",
  },
  custom: {
    label: "Custom (OpenAI-compatible)",
    base_url: "",
    model: "",
  },
};

export const PROFILE_ORDER: ModelProfile[] = [
  "nemotron",
  "openai",
  "openrouter",
  "gemini",
  "custom",
];

const PROFILES = Object.keys(MODEL_PROFILES) as ModelProfile[];

export function isModelProfile(value: unknown): value is ModelProfile {
  return typeof value === "string" && (PROFILES as string[]).includes(value);
}

function modelStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function parseStoredConfig(value: unknown): ModelConfig | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  if (
    !isModelProfile(record.profile) ||
    typeof record.base_url !== "string" ||
    typeof record.model !== "string" ||
    typeof record.api_key !== "string"
  ) {
    return null;
  }
  return {
    profile: record.profile,
    base_url: record.base_url,
    model: record.model,
    api_key: record.api_key,
  };
}

/** Read the browser-local config; invalid or unavailable storage yields null. */
export function loadModelConfig(scope = "unscoped"): ModelConfig | null {
  try {
    const raw = modelStorage()?.getItem(`${MODEL_STORAGE_KEY}.${scope}`);
    if (!raw) return null;
    return parseStoredConfig(JSON.parse(raw));
  } catch {
    return null;
  }
}

/** Persist only the four model fields under the single localStorage key. */
export function saveModelConfig(config: ModelConfig, scope = "unscoped"): boolean {
  const profile = isModelProfile(config.profile) ? config.profile : "custom";
  const stored: ModelConfig = {
    profile,
    base_url: config.base_url.trim(),
    model: config.model.trim(),
    api_key: config.api_key.trim(),
  };
  try {
    const storage = modelStorage();
    if (!storage) return false;
    storage.setItem(`${MODEL_STORAGE_KEY}.${scope}`, JSON.stringify(stored));
    return true;
  } catch {
    return false;
  }
}

/** Remove the browser-local config; a missing key is already clear. */
export function clearModelConfig(scope = "unscoped"): void {
  try {
    modelStorage()?.removeItem(`${MODEL_STORAGE_KEY}.${scope}`);
  } catch {
    // The config is unusable either way when storage is unavailable.
  }
}

/** A config is usable only when all three request fields are present. */
export function isModelConfigComplete(
  config: ModelConfig | null | undefined,
): config is ModelConfig {
  return Boolean(
    config &&
      config.base_url.trim() &&
      config.model.trim() &&
      config.api_key.trim(),
  );
}

/** Render a saved key without revealing it, e.g. ``sk-…1234``. */
export function maskApiKey(apiKey: string): string {
  const key = apiKey.trim();
  if (!key) return "";
  if (key.length <= 4) return `…${key}`;
  const prefix = key.startsWith("sk-") ? "sk-" : "";
  return `${prefix}…${key.slice(-4)}`;
}
