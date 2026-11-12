import { useEffect, useState } from "react";
import { Button, Input, Modal, Switch } from "@forgeflow/dsh-primitives";
import { api, getModelSettings, testModelConnection } from "./api";
import { useWorkspace } from "./workspace";
import { SelectMenu } from "./SelectMenu";
import { KnowledgeLibrary } from "./KnowledgeLibrary";
import { useI18n, type MessageKey } from "./i18n";
import {
  MODEL_PROFILES,
  PROFILE_ORDER,
  clearModelConfig,
  isModelConfigComplete,
  isModelProfile,
  loadModelConfig,
  saveModelConfig,
  type ModelConfig,
  type ModelProfile,
} from "./modelConfig";
import type { ModelSettingsInfo } from "./types";

type Props = {
  onClose: () => void;
  initialProfile?: ModelProfile;
};

type BusyState = "save" | "test" | null;

type Status = {
  kind: "info" | "success" | "error";
  key: MessageKey;
  vars?: Record<string, string | number>;
};

function defaultConfig(profile: ModelProfile = "deepseek_flash"): ModelConfig {
  return {
    profile,
    base_url: MODEL_PROFILES[profile]?.base_url ?? "",
    model: MODEL_PROFILES[profile]?.model ?? "",
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

export function SettingsDialog({ onClose, initialProfile }: Props) {
  const { t } = useI18n();
  const { scope_id, deployment } = useWorkspace();
  const [initialSaved] = useState<ModelConfig | null>(() => loadModelConfig(scope_id, deployment));
  const [config, setConfig] = useState<ModelConfig>(() => (
    initialSaved && (!initialProfile || initialSaved.profile === initialProfile)
      ? initialSaved
      : defaultConfig(initialProfile ?? "deepseek_flash")
  ));
  const [ownKey, setOwnKey] = useState(() => isModelConfigComplete(initialSaved) || deployment === "local");
  const [platform, setPlatform] = useState<ModelSettingsInfo | null>(null);
  const canOverride = deployment !== "hosted" && platform?.can_override !== false;
  const [platformLoading, setPlatformLoading] = useState(true);
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState<BusyState>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [binding, setBinding] = useState<{ bound: boolean; role: string | null } | null>(null);
  const [odooUserId, setOdooUserId] = useState("");
  const [adminToken, setAdminToken] = useState("");
  const [bindingBusy, setBindingBusy] = useState(false);
  const [documentFile, setDocumentFile] = useState<File | null>(null);
  const [documentScope, setDocumentScope] = useState("project");
  const [documentBusy, setDocumentBusy] = useState(false);
  const [documentStatus, setDocumentStatus] = useState("");
  const [documents, setDocuments] = useState<{ source_path: string; filename: string; version: number; acl_scope: string; indexed_generation: string | null }[]>([]);
  const [sourceEnabled, setSourceEnabled] = useState(false);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [sourceStatus, setSourceStatus] = useState("");
  const [sourceHealthy, setSourceHealthy] = useState<boolean | null>(null);
  const [versionPath, setVersionPath] = useState("");
  const [versions, setVersions] = useState<{ version: number; created_at: string }[]>([]);

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

  useEffect(() => {
    if (deployment !== "local") return;
    let active = true;
    api<{ bound: boolean; role: string | null }>("/api/odoo20/binding")
      .then((value) => { if (active) setBinding(value); })
      .catch(() => { if (active) setBinding(null); });
    return () => { active = false; };
  }, [deployment]);

  async function bindOdooUser() {
    if (!odooUserId || !adminToken) return;
    setBindingBusy(true);
    try {
      await api("/api/odoo20/binding", {
        method: "POST",
        body: JSON.stringify({
          odoo_user_id: Number(odooUserId), role: "approver", admin_token: adminToken,
        }),
      });
      setBinding({ bound: true, role: "approver" });
      setAdminToken("");
    } catch {
      setBinding({ bound: false, role: null });
    } finally {
      setBindingBusy(false);
    }
  }

  const refreshDocuments = () => {
    if (deployment !== "local") return;
    void api<{ source_path: string; filename: string; version: number; acl_scope: string; indexed_generation: string | null }[]>("/api/knowledge/documents")
      .then(setDocuments).catch(() => setDocuments([]));
  };

  useEffect(() => { refreshDocuments(); }, [deployment]);

  useEffect(() => {
    if (deployment !== "local") return;
    void api<{ enabled: boolean }[]>("/api/sources/subscriptions")
      .then((rows) => setSourceEnabled(Boolean(rows[0]?.enabled)))
      .catch(() => setSourceEnabled(false));
    void api<{ healthy: boolean }>("/api/sources/health")
      .then((value) => setSourceHealthy(value.healthy))
      .catch(() => setSourceHealthy(false));
  }, [deployment]);

  async function saveSource() {
    setSourceBusy(true);
    try {
      await api("/api/sources/subscriptions", {
        method: "POST",
        body: JSON.stringify({ source_type: "odoo20.purchase_order", enabled: sourceEnabled, interval_seconds: 60 }),
      });
      setSourceStatus(t("settings.saved"));
      const health = await api<{ healthy: boolean }>("/api/sources/health");
      setSourceHealthy(health.healthy);
    } catch {
      setSourceStatus(t("settings.documentFailed"));
    } finally {
      setSourceBusy(false);
    }
  }

  async function deleteDocument(sourcePath: string) {
    setDocumentBusy(true);
    try {
      const response = await fetch(`/api/knowledge/documents/${encodeURIComponent(sourcePath)}`, {
        method: "DELETE", credentials: "same-origin",
      });
      if (!response.ok) throw new Error("delete_failed");
      refreshDocuments();
    } catch {
      setDocumentStatus(t("settings.documentFailed"));
    } finally {
      setDocumentBusy(false);
    }
  }

  async function indexDocument(sourcePath: string) {
    try {
      await api(`/api/knowledge/documents/${encodeURIComponent(sourcePath)}/index`, { method: "POST" });
      refreshDocuments();
    } catch {
      setDocumentStatus(t("settings.documentFailed"));
    }
  }

  async function showVersions(sourcePath: string) {
    setVersionPath(sourcePath);
    setVersions([]);
    try {
      const rows = await api<{ version: number; created_at: string }[]>(
        `/api/knowledge/documents/${encodeURIComponent(sourcePath)}/versions`);
      setVersions(rows);
    } catch {
      setVersions([]);
    }
  }

  async function restoreVersion(sourcePath: string, version: number) {
    try {
      await api(`/api/knowledge/documents/${encodeURIComponent(sourcePath)}/restore/${version}`, { method: "POST" });
      await showVersions(sourcePath);
      refreshDocuments();
    } catch {
      setDocumentStatus(t("settings.documentFailed"));
    }
  }

  async function uploadDocument() {
    if (!documentFile) return;
    setDocumentBusy(true);
    setDocumentStatus("");
    const form = new FormData();
    form.append("file", documentFile);
    form.append("acl_scope", documentScope);
    try {
      const response = await fetch("/api/knowledge/documents", {
        method: "POST", body: form, credentials: "same-origin",
      });
      if (!response.ok) throw new Error("upload_failed");
      const result = await response.json() as { version: number };
      setDocumentStatus(t("settings.documentUploaded", { version: String(result.version) }));
      setDocumentFile(null);
      refreshDocuments();
    } catch {
      setDocumentStatus(t("settings.documentFailed"));
    } finally {
      setDocumentBusy(false);
    }
  }

  function update<K extends keyof ModelConfig>(key: K, value: ModelConfig[K]) {
    setConfig((current) => ({ ...current, [key]: value }));
    setStatus(null);
  }

  function selectProfile(profile: ModelProfile) {
    const preset = MODEL_PROFILES[profile];
    setConfig((current) => ({
      ...current,
      profile,
      base_url: profile === "custom" ? "" : preset.base_url,
      model: profile === "custom" ? "" : preset.model,
      api_key: "",
    }));
    setShowKey(false);
    setStatus(null);
  }

  function toggleOwnKey(next: boolean) {
    if (!canOverride) return;
    setOwnKey(next);
    setStatus(next ? null : { kind: "info", key: "settings.statusPlatform" });
    if (!next) {
      // Off means requests must omit the model payload. Presence of the
      // localStorage entry is the only persisted "own key active" signal, so
      // switching off removes it instead of keeping a stale active config.
      clearModelConfig(scope_id);
      setShowKey(false);
    }
  }

  function save() {
    if (!canOverride || !ownKey) {
      setStatus({ kind: "success", key: "settings.statusPlatform" });
      return;
    }
    const candidate = formConfig(config);
    if (!candidate.base_url || !candidate.model || !candidate.api_key) {
      setStatus({ kind: "error", key: "settings.statusRequired" });
      return;
    }
    setBusy("save");
    const stored = saveModelConfig(candidate, scope_id, deployment);
    setBusy(null);
    if (!stored) {
      setStatus({ kind: "error", key: "settings.statusSaveFailed" });
      return;
    }
    setConfig(candidate);
    setStatus({ kind: "success", key: "settings.statusSaved" });
  }

  async function test() {
    const candidate = formConfig(config);
    if (canOverride && ownKey && (!candidate.base_url || !candidate.model || !candidate.api_key)) {
      setStatus({ kind: "error", key: "settings.statusRequired" });
      return;
    }
    setBusy("test");
    setStatus({ kind: "info", key: "settings.testing" });
    try {
      // Off: POST {} so the server tests its platform default with its own
      // hidden key. Never send a stale browser config in that mode.
      const result = await testModelConnection(canOverride && ownKey ? candidate : undefined);
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
              {canOverride ? (
                <div className="settings-field settings-model-choice">
                  <label htmlFor="settings-model-choice">{t("settings.model")}</label>
                  <SelectMenu
                    id="settings-model-choice"
                    value={config.profile}
                    ariaLabel={t("settings.model")}
                    options={PROFILE_ORDER.map((profile) => ({
                      value: profile,
                      label: MODEL_PROFILES[profile].label,
                    }))}
                    onChange={(value) => { selectProfile(value as ModelProfile); setOwnKey(true); }}
                  />
                </div>
              ) : null}
              {deployment === "local" && <section className="settings-section" aria-labelledby="settings-odoo20-heading">
                <h3 id="settings-odoo20-heading">{t("settings.odoo20Access")}</h3>
                {binding?.bound ? <p className="settings-note">{t("settings.boundAs", { role: binding.role ?? "" })}</p> : <>
                  <div className="settings-field">
                    <label htmlFor="settings-odoo-user">{t("settings.odooUserId")}</label>
                    <Input id="settings-odoo-user" inputMode="numeric" value={odooUserId} onChange={(event) => setOdooUserId(event.target.value)} />
                  </div>
                  <div className="settings-field">
                    <label htmlFor="settings-binding-token">{t("settings.adminToken")}</label>
                    <Input id="settings-binding-token" type="password" value={adminToken} onChange={(event) => setAdminToken(event.target.value)} />
                  </div>
                  <button type="button" className="secondary" disabled={bindingBusy || !odooUserId || !adminToken} onClick={() => void bindOdooUser()}>{t("settings.bind")}</button>
                </>}
              </section>}
              {deployment !== "local" && <KnowledgeLibrary />}
              {deployment === "local" && <section className="settings-section" aria-labelledby="settings-documents-heading">
                <h3 id="settings-documents-heading">{t("settings.documents")}</h3>
                <div className="settings-field">
                  <label htmlFor="settings-document">{t("settings.documentUpload")}</label>
                  <input id="settings-document" type="file" accept=".pdf,.docx,.xlsx,.csv,.tsv,.md,.txt" onChange={(event) => setDocumentFile(event.currentTarget.files?.[0] ?? null)} />
                </div>
                <div className="settings-field">
                  <label htmlFor="settings-document-scope">{t("settings.documentAcl")}</label>
                  <SelectMenu id="settings-document-scope" value={documentScope} ariaLabel={t("settings.documentAcl")}
                    options={[{ value: "project", label: "project" }, { value: "engineering", label: "engineering" }]}
                    onChange={setDocumentScope} />
                </div>
                <button type="button" className="secondary" disabled={documentBusy || !documentFile} onClick={() => void uploadDocument()}>{t("settings.documentUpload")}</button>
                {documentStatus && <p className="settings-note">{documentStatus}</p>}
                {documents.length > 0 && <ul className="settings-documents">
                  {documents.map(document => <li key={document.source_path}>
                    <span>{document.filename} · v{document.version} · {document.acl_scope} · {document.indexed_generation ? t("settings.indexed") : t("settings.notIndexed")}</span>
                    {!document.indexed_generation && <button type="button" className="secondary" disabled={documentBusy} onClick={() => void indexDocument(document.source_path)}>{t("settings.index")}</button>}
                    <button type="button" className="secondary" disabled={documentBusy} onClick={() => void showVersions(document.source_path)}>{t("settings.versions")}</button>
                    <button type="button" className="secondary" disabled={documentBusy} onClick={() => void deleteDocument(document.source_path)}>{t("settings.delete")}</button>
                  </li>)}
                </ul>}
                {versionPath && <div className="settings-documents">
                  <h4>{t("settings.versions")} · {versionPath}</h4>
                  {versions.map(item => <div key={item.version}>
                    <span>v{item.version}</span>
                    <button type="button" className="secondary" onClick={() => void restoreVersion(versionPath, item.version)}>{t("settings.restore")}</button>
                  </div>)}
                </div>}
              </section>}
              {deployment === "local" && <section className="settings-section" aria-labelledby="settings-sources-heading">
                <h3 id="settings-sources-heading">{t("settings.sources")}</h3>
                <label className="settings-check"><input type="checkbox" checked={sourceEnabled} onChange={(event) => setSourceEnabled(event.currentTarget.checked)} /> {t("settings.sourceOdoo")}</label>
                <button type="button" className="secondary" disabled={sourceBusy} onClick={() => void saveSource()}>{t("settings.save")}</button>
                {sourceHealthy !== null && <p className="settings-note">{sourceHealthy ? t("settings.sourceHealthy") : t("settings.sourceStale")}</p>}
                {sourceStatus && <p className="settings-note">{sourceStatus}</p>}
              </section>}
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

              {canOverride && <div className="settings-toggle">
                <Switch
                  checked={ownKey}
                  onChange={toggleOwnKey}
                  label={t("settings.useOwnKey")}
                />
                <span aria-hidden="true">{t("settings.useOwnKey")}</span>
              </div>}

              {canOverride && ownKey ? (
                <>
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

                </>
              ) : null}

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
                  disabled={busy !== null || !canOverride || !ownKey}
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
                  disabled={busy !== null || !canOverride || !ownKey}
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
