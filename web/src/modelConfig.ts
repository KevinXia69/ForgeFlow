/**
 * Browser-local model API configuration.
 *
 * The key lives only in this document's memory. Refresh and logout discard it.
 * Optional overrides leave the server's default model unchanged.
 */
import { browserSessionAllows, onBrowserSessionClear } from "./browserSession";
import presets from "./modelProfiles.json";

export type ModelProfile = keyof typeof presets | "gemini";
export type ModelApi = "openai-completions" | "openai-responses" | "anthropic-messages" | "google-generative-ai";
export type ModelInput = "text" | "image";

export type ModelConfig = {
  profile: ModelProfile;
  base_url: string;
  model: string;
  api_key: string;
  api?: ModelApi;
  provider?: string;
  input?: ModelInput[];
  image_input?: boolean;
};

export type ModelProfilePreset = {
  label: string;
  base_url: string;
  model: string;
  api: ModelApi;
  provider: string;
  input: ModelInput[];
  endpoints: Partial<Record<ModelApi, string>>;
};

export const MODEL_STORAGE_KEY = "forgeflow.model";

export const MODEL_PROFILES: Record<ModelProfile, ModelProfilePreset> = {
  ...presets,
  // Preserve an already open tab's older compatible-Gemini selection.
  gemini: { ...presets.google, api: "openai-completions",
    base_url: presets.google.endpoints["openai-completions"] },
} as Record<ModelProfile, ModelProfilePreset>;

export const PROFILE_ORDER: ModelProfile[] = [
  "deepseek_flash",
  "nemotron",
  "openai",
  "anthropic",
  "google",
  "openrouter",
  "dashscope",
  "siliconflow",
  "custom",
];

export const MODEL_APIS: Record<ModelApi, string> = {
  "openai-completions": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages",
  "google-generative-ai": "Google Generative AI",
};

export function modelPreset(profile: ModelProfile = "deepseek_flash"): ModelConfig {
  const preset = MODEL_PROFILES[profile];
  return { profile, base_url: preset.base_url, model: preset.model, api_key: "",
    api: preset.api, provider: preset.provider, input: [...preset.input],
    image_input: preset.input.includes("image") };
}

/** Browser requests always target public HTTPS; local engines use local setup. */
export function isPublicModelEndpoint(value: string): boolean {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash
      || /\s|\\/.test(value) || !host.includes(".") || host.endsWith(".localhost")
      || host.endsWith(".local") || host.endsWith(".internal")) return false;
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
      const [a, b] = host.split(".").map(Number);
      if ([0, 10, 127].includes(a) || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
        || (a === 192 && b === 168) || a >= 224) return false;
    }
    return true;
  } catch { return false; }
}

export function supportsModelImages(config: ModelConfig): boolean {
  return config.image_input ?? config.input?.includes("image") ?? true;
}

const PROFILES = Object.keys(MODEL_PROFILES) as ModelProfile[];
const configs = new Map<string, ModelConfig>();
onBrowserSessionClear(() => configs.clear());

export function isModelProfile(value: unknown): value is ModelProfile {
  return typeof value === "string" && (PROFILES as string[]).includes(value);
}

/** Return a copy so callers cannot mutate the session's saved configuration. */
export function loadModelConfig(scope = "unscoped", _deployment = "local"): ModelConfig | null {
  if (!browserSessionAllows(scope)) return null;
  const config = configs.get(scope);
  return config ? { ...config, ...(config.input ? { input: [...config.input] } : {}) } : null;
}

/** Use only for the current tab until refresh or logout; never write browser storage. */
export function saveModelConfig(config: ModelConfig, scope = "unscoped", _deployment = "local"): boolean {
  if (!browserSessionAllows(scope) || !isPublicModelEndpoint(config.base_url)) return false;
  const profile = isModelProfile(config.profile) ? config.profile : "custom";
  const stored: ModelConfig = {
    profile,
    base_url: config.base_url.trim(),
    model: config.model.trim(),
    api_key: config.api_key.trim(),
    api: config.api ?? "openai-completions",
    provider: config.provider ?? "openai-compatible",
    input: supportsModelImages(config) ? ["text", "image"] : ["text"],
    image_input: supportsModelImages(config),
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
