/**
 * Browser-local model API configuration.
 *
 * The key lives only in this document's memory. Refresh and logout discard it.
 * Hosted workspaces do not accept client model keys.
 */
import { browserSessionAllows, onBrowserSessionClear } from "./browserSession";

export type ModelProfile =
  | "deepseek_flash"
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
  deepseek_flash: {
    label: "DeepSeek V4.1 Flash 1017",
    base_url: "https://api.deepseek.com/v1",
    model: "deepseek-flash",
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
  "deepseek_flash",
  "openai",
  "openrouter",
  "gemini",
  "custom",
];

const PROFILES = Object.keys(MODEL_PROFILES) as ModelProfile[];
const configs = new Map<string, ModelConfig>();
onBrowserSessionClear(() => configs.clear());

export function isModelProfile(value: unknown): value is ModelProfile {
  return typeof value === "string" && (PROFILES as string[]).includes(value);
}

/** Return a copy so callers cannot mutate the session's saved configuration. */
export function loadModelConfig(scope = "unscoped", deployment = "local"): ModelConfig | null {
  if (deployment === "hosted" || !browserSessionAllows(scope)) return null;
  const config = configs.get(scope);
  return config ? { ...config } : null;
}

/** Use only for the current tab until refresh or logout; never write browser storage. */
export function saveModelConfig(config: ModelConfig, scope = "unscoped", deployment = "local"): boolean {
  if (deployment === "hosted" || !browserSessionAllows(scope)) return false;
  const profile = isModelProfile(config.profile) ? config.profile : "custom";
  const stored: ModelConfig = {
    profile,
    base_url: config.base_url.trim(),
    model: config.model.trim(),
    api_key: config.api_key.trim(),
  };
  configs.set(scope, stored);
  return true;
}

/** Forget the in-memory key. Legacy storage is removed by session initialization. */
export function clearModelConfig(scope = "unscoped"): void {
  configs.delete(scope);
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
