import { useEffect, useState } from "react";
import { Button, Input, Modal, Switch } from "@forgeflow/dsh-primitives";
import { api, getModelSettings, testModelConnection } from "./api";
import { useWorkspace } from "./workspace";
import { SelectMenu } from "./SelectMenu";
import { useI18n, type MessageKey } from "./i18n";
import {
  MODEL_PROFILES,
  PROFILE_ORDER,
  clearModelConfig,
  isModelConfigComplete,
  isModelProfile,
  loadModelConfig,
  maskApiKey,
  saveModelConfig,
  type ModelConfig,
  type ModelProfile,
} from "./modelConfig";
import type { ModelSettingsInfo } from "./types";

type Props = {
  onClose: () => void;
};

type BusyState = "save" | "test" | null;

type Status = {
  kind: "info" | "success" | "error";
  key: MessageKey;
  vars?: Record<string, string | number>;
};

function defaultConfig(): ModelConfig {
  const profile: ModelProfile = "nemotron";
  return {
    profile,
    base_url: MODEL_PROFILES[profile].base_url,
    model: MODEL_PROFILES[profile].model,
    api_key: "",
  };
}

function formConfig(config: ModelConfig): ModelConfig {
  return {
    profile: config.profile,
    base_url: config.base_url.trim(),
    model: config.model.trim(),
    api_key: config.api_key.trim(),
  };
}

function platformProfileLabel(profile: string | null | undefined): string {
  if (!profile) return "—";
  return isModelProfile(profile) ? MODEL_PROFILES[profile].label : profile;
}

export function SettingsDialog({ onClose }: Props) {
  const { t } = useI18n();
  const { scope_id } = useWorkspace();
  const [initialSaved] = useState<ModelConfig | null>(() => loadModelConfig(scope_id));
  const [saved, setSaved] = useState<ModelConfig | null>(initialSaved);
  const [config, setConfig] = useState<ModelConfig>(() => initialSaved ?? defaultConfig());
  const [ownKey, setOwnKey] = useState(() => isModelConfigComplete(initialSaved));
  const [platform, setPlatform] = useState<ModelSettingsInfo | null>(null);
  const [platformLoading, setPlatformLoading] = useState(true);
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState<BusyState>(null);
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  // The platform default is read-only and never includes a key. It is shown
  // on both local and review deployments.
  useEffect(() => {
    let active = true;
    getModelSettings()
      .then((info) => {
        if (!active) return;
        setPlatform(info);
        setPlatformLoading(false);
      })
      .catch(() => {
        if (!active) return;
        setPlatform(null);
        setPlatformLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  function update<K extends keyof ModelConfig>(key: K, value: ModelConfig[K]) {
    setConfig((current) => ({ ...current, [key]: value }));
    setStatus(null);
  }

  function selectProfile(profile: ModelProfile) {
    const preset = MODEL_PROFILES[profile];
    setConfig((current) => ({
      ...current,
      profile,
      base_url: profile === "nemotron" ? preset.base_url : "",
      model: profile === "nemotron" ? preset.model : "",
      api_key: "",
    }));
    setShowKey(false);
    setStatus(null);
  }

  function toggleOwnKey(next: boolean) {
    setOwnKey(next);
    setStatus(next ? null : { kind: "info", key: "settings.statusPlatform" });
    if (!next) {
      // Off means requests must omit the model payload. Presence of the
      // localStorage entry is the only persisted "own key active" signal, so
      // switching off removes it instead of keeping a stale active config.
      clearModelConfig(scope_id);
      setSaved(null);
      setShowKey(false);
    }
  }

  function save() {
    if (!ownKey) {
      setStatus({ kind: "success", key: "settings.statusPlatform" });
      return;
    }
    const candidate = formConfig(config);
    if (!candidate.base_url || !candidate.model || !candidate.api_key) {
      setStatus({ kind: "error", key: "settings.statusRequired" });
      return;
    }
    setBusy("save");
    const stored = saveModelConfig(candidate, scope_id);
    setBusy(null);
    if (!stored) {
      setStatus({ kind: "error", key: "settings.statusSaveFailed" });
      return;
    }
    setConfig(candidate);
    setSaved(candidate);
    setStatus({ kind: "success", key: "settings.statusSaved" });
  }

  async function test() {
    const candidate = formConfig(config);
    if (ownKey && (!candidate.base_url || !candidate.model || !candidate.api_key)) {
      setStatus({ kind: "error", key: "settings.statusRequired" });
      return;
    }
    setBusy("test");
    setStatus({ kind: "info", key: "settings.testing" });
    try {
      // Off: POST {} so the server tests its platform default with its own
      // hidden key. Never send a stale browser config in that mode.
      const result = await testModelConnection(ownKey ? candidate : undefined);
      if (result.ok) {
        setStatus({
          kind: "success",
          key: "settings.testOk",
          vars: { latency: result.latency_ms },
        });
      } else {
        setStatus({
          kind: "error",
          key: "settings.testFailed",
          vars: { error: result.error ?? "unknown" },
        });
      }
    } catch (error) {
      setStatus({
        kind: "error",
        key: "settings.testFailed",
        vars: { error: error instanceof Error && error.message ? error.message : "unknown" },
      });
    } finally {
      setBusy(null);
    }
  }

  function clear() {
    clearModelConfig(scope_id);
    setSaved(null);
    setOwnKey(false);
    const preset = MODEL_PROFILES[config.profile];
    setConfig({
      profile: config.profile,
      base_url: preset.base_url,
      model: preset.model,
      api_key: "",
    });
    setShowKey(false);
    setStatus({ kind: "success", key: "settings.statusCleared" });
  }

  const savedLine = saved
    ? saved.api_key
      ? t("settings.savedWithKey", { key: maskApiKey(saved.api_key) })
      : t("settings.saved")
    : t("settings.notSaved");
  // A platform credential is never rendered into this form. Any key in this
  // field is therefore user-supplied (BYOK), including a user-owned Nemotron
  // key, so it may use the normal reveal control.


  // DSH Modal is used headless so ForgeFlow keeps its serif header and wide
  // surface. The primitive still owns the body portal, mask click, Escape,
  // focus trap and focus restore; the package imports its own CSS modules and
  // only the --dsw-* variables are mapped in styles.css.
  return (
    <Modal
      open
      onClose={onClose}
      title={t("settings.title")}
      headless
      className="settings-dialog settings-dialog-wide"
    >
      <div className="settings-head">
        <h2 id="settings-title">{t("settings.title")}</h2>
        <button
          className="settings-close"
          type="button"
          aria-label={t("settings.close")}
          onClick={onClose}
          data-modal-autofocus="true"
        >
          ×
        </button>
      </div>

      <div className="settings-shell settings-shell-single">
        <div className="settings-content">
          <div id="settings-panel-model">
            <section className="settings-section" aria-labelledby="settings-model-heading">
              <h3 id="settings-model-heading">{t("settings.modelSection")}</h3>
              <p className="settings-note">{t("v9.modelScope")}</p>
              <dl className="settings-scope" data-model-scope="true">
                <div><dt>{t("settings.scopeAsk")}</dt><dd>{t("settings.scopeAskBody")}</dd></div>
                <div><dt>{t("settings.scopeCase")}</dt><dd>{t("settings.scopeCaseBody")}</dd></div>
                <div><dt>{t("settings.scopePlan")}</dt><dd>{t("settings.scopePlanBody")}</dd></div>
                <div><dt>{t("settings.scopeResearch")}</dt><dd>{t("settings.scopeResearchBody")}</dd></div>
              </dl>
              <p className="settings-note">{t("sidebar.dataStaysLocal")}</p>
              <button type="button" className="secondary" onClick={() => { void api("/api/auth/session", { method: "DELETE" }).then(() => location.reload()).catch(() => setStatus({ kind: "error", key: "login.errorFailed" })); }}>{t("v9.exit")}</button>

              <div className="settings-platform">
                <div className="settings-platform-head">
                  <strong>{t("settings.platformTitle")}</strong>
                  <span className="settings-platform-tag">{t("settings.platformTag")}</span>
                </div>
                {platformLoading ? (
                  <p className="settings-platform-empty">{t("settings.platformLoading")}</p>
                ) : platform?.configured ? (
                  <dl className="settings-platform-facts">
                    <dt>{t("settings.profile")}</dt>
                    <dd>{platformProfileLabel(platform.profile)}</dd>
                    {platform.base_url ? (
                      <>
                        <dt>{t("settings.baseUrl")}</dt>
                        <dd>{platform.base_url}</dd>
                      </>
                    ) : null}
                    {platform.model ? (
                      <>
                        <dt>{t("settings.model")}</dt>
                        <dd>{platform.model}</dd>
                      </>
                    ) : null}
                  </dl>
                ) : (
                  <p className="settings-platform-empty">{t("settings.platformUnavailable")}</p>
                )}
              </div>

              <div className="settings-toggle">
                <Switch
                  checked={ownKey}
                  onChange={toggleOwnKey}
                  label={t("settings.useOwnKey")}
                />
                <span aria-hidden="true">{t("settings.useOwnKey")}</span>
              </div>

              {ownKey ? (
                <>
                  <div className="settings-field">
                    <label htmlFor="settings-profile">{t("settings.profile")}</label>
                    <SelectMenu
                      id="settings-profile"
                      value={config.profile}
                      ariaLabel={t("settings.profile")}
                      options={PROFILE_ORDER.map((profile) => ({
                        value: profile,
                        label: MODEL_PROFILES[profile].label,
                      }))}
                      onChange={(value) => selectProfile(value as ModelProfile)}
                    />
                  </div>

                  <div className="settings-field">
                    <label htmlFor="settings-base-url">{t("settings.baseUrl")}</label>
                    <Input
                      id="settings-base-url"
                      type="url"
                      inputMode="url"
                      autoComplete="off"
                      spellCheck={false}
                      placeholder="https://api.example.com/v1"
                      value={config.base_url}
                      onChange={(event) => update("base_url", event.target.value)}
                      className="settings-input"
                    />
                  </div>

                  <div className="settings-field">
                    <label htmlFor="settings-model">{t("settings.model")}</label>
                    <Input
                      id="settings-model"
                      type="text"
                      autoComplete="off"
                      spellCheck={false}
                      placeholder="model-name"
                      value={config.model}
                      onChange={(event) => update("model", event.target.value)}
                      className="settings-input"
                    />
                  </div>

                  <div className="settings-field">
                    <label htmlFor="settings-api-key">{t("settings.apiKey")}</label>
                    <div className="settings-key-field">
                      <Input
                        id="settings-api-key"
                        type={showKey ? "text" : "password"}
                        autoComplete="new-password"
                        spellCheck={false}
                        value={config.api_key}
                        onChange={(event) => update("api_key", event.target.value)}
                        className="settings-input settings-key-input"
                      />
                      <button
                        className="settings-key-eye"
                        type="button"
                        aria-label={showKey ? t("settings.hideKey") : t("settings.showKey")}
                        title={showKey ? t("settings.hideKey") : t("settings.showKey")}
                        aria-pressed={showKey}
                        onClick={() => setShowKey((value) => !value)}
                      >
                        {showKey ? (
                          <svg viewBox="0 0 20 20" width="17" height="17" fill="none" aria-hidden="true">
                            <path d="M2 10s3-5 8-5 8 5 8 5-3 5-8 5-8-5-8-5Z" stroke="currentColor" strokeWidth="1.4"/>
                            <circle cx="10" cy="10" r="2.3" stroke="currentColor" strokeWidth="1.4"/>
                            <path d="m3 3 14 14" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"/>
                          </svg>
                        ) : (
                          <svg viewBox="0 0 20 20" width="17" height="17" fill="none" aria-hidden="true">
                            <path d="M2 10s3-5 8-5 8 5 8 5-3 5-8 5-8-5-8-5Z" stroke="currentColor" strokeWidth="1.4"/>
                            <circle cx="10" cy="10" r="2.3" stroke="currentColor" strokeWidth="1.4"/>
                          </svg>
                        )}
                      </button>
                    </div>
                  </div>

                  <p className="settings-note">{t("settings.keyNote")}</p>
                  <p className="settings-saved">{savedLine}</p>
                </>
              ) : (
                <p className="settings-note">{t("settings.platformNote")}</p>
              )}

              <p
                className={`settings-status${status ? ` is-${status.kind}` : ""}`}
                role="status"
                aria-live="polite"
              >
                {status ? t(status.key, status.vars) : ""}
              </p>

              <div className="settings-actions">
                <Button
                  variant="primary"
                  onClick={save}
                  disabled={busy !== null || !ownKey}
                >
                  {t("settings.save")}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    void test();
                  }}
                  disabled={busy !== null}
                >
                  {busy === "test" ? t("settings.testing") : t("settings.test")}
                </Button>
                <Button
                  variant="outline"
                  onClick={clear}
                  disabled={busy !== null || !ownKey}
                >
                  {t("settings.clear")}
                </Button>
              </div>
            </section>
          </div>
        </div>
      </div>
    </Modal>
  );
}
