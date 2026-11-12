import { useEffect, useRef, useState } from "react";
import { ChatSurface, useKnowledgeResponder } from "./Assistant";
import { api, cancelAssistantTurn, getModelSettings, getWorkspaces, intakeWorkspaceMaterial } from "./api";
import { useI18n } from "./i18n";
import { MODEL_PROFILES, isModelConfigComplete, isModelProfile, loadModelConfig, saveModelConfig, type ModelProfile } from "./modelConfig";
import { SelectMenu } from "./SelectMenu";
import type { ModelSettingsInfo, Workspace } from "./types";
import { navigate, useWorkspace } from "./workspace";
import { uploadProgress, type UploadedDocument } from "./knowledgeUpload";

/**
 * Workbench entry. The same composer either asks a question or records company
 * material, selected explicitly with a mode switch so pasted data is never
 * mistaken for a chat question.
 */
export function HomeView({ onOpenModelSettings }: { onOpenModelSettings?: (profile?: ModelProfile) => void }) {
  const { t } = useI18n();
  const responder = useKnowledgeResponder();
  const { scope_id, deployment } = useWorkspace();
  const savedModel = loadModelConfig(scope_id, deployment);
  const [platform, setPlatform] = useState<ModelSettingsInfo | null>(null);
  const platformProfile = platform?.configured && isModelProfile(platform.profile)
    ? platform.profile : null;
  const currentProfile: ModelProfile = savedModel?.profile ?? platformProfile ?? "deepseek_flash";
  const configuredModels = new Map<ModelProfile, string>();
  if (platformProfile) configuredModels.set(platformProfile, MODEL_PROFILES[platformProfile].label);
  if (savedModel && isModelConfigComplete(savedModel)) {
    configuredModels.set(savedModel.profile, MODEL_PROFILES[savedModel.profile].label);
  }
  const modelOptions = [...configuredModels].map(([value, label]) => ({ value, label }));
  const [mode, setMode] = useState<"ask" | "intake">("ask");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [intakeWorkspaceId, setIntakeWorkspaceId] = useState("");
  const [panel, setPanel] = useState<"lookup" | "tasks" | "upload" | null>(null);
  const materialInput = useRef<HTMLInputElement | null>(null);
  const [uploadStatus, setUploadStatus] = useState("");
  const [uploaded, setUploaded] = useState<{ source_path: string; job_id?: string } | null>(null);
  const [uploadCheck, setUploadCheck] = useState(0);
  const uploadGeneration = useRef(0);
  const [tasks, setTasks] = useState<Array<Record<string, unknown>> | null>(null);
  const [tasksError, setTasksError] = useState(false);
  const [lookup, setLookup] = useState("");
  const [lookupRows, setLookupRows] = useState<Array<Record<string, unknown>> | null>(null);
  const [lookupError, setLookupError] = useState(false);
  const [lookupBusy, setLookupBusy] = useState(false);

  useEffect(() => {
    if (!uploaded) return;
    const controller = new AbortController();
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    let attempts = 0;
    let failures = 0;
    const check = async () => {
      attempts++;
      try {
        const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]);
        const job = uploaded.job_id
          ? await api<{ state: string }>(`/api/knowledge/parse-jobs/${encodeURIComponent(uploaded.job_id)}`, { signal })
          : null;
        const documents = !job || job.state === "succeeded"
          ? await api<UploadedDocument[]>("/api/knowledge/documents", { signal }) : [];
        if (!live) return;
        const progress = uploadProgress(job?.state, documents.find(row => row.source_path === uploaded.source_path));
        setUploadStatus(progress); failures = 0;
        if (["ready", "review", "failed"].includes(progress)) return;
      } catch {
        if (!live) return;
        failures++;
        if (failures >= 3) { setUploadStatus("unavailable"); return; }
      }
      if (attempts >= 120) { setUploadStatus("background"); return; }
      timer = setTimeout(() => { void check(); }, 3000);
    };
    void check();
    return () => { live = false; controller.abort(); clearTimeout(timer); };
  }, [uploaded, uploadCheck]);

  useEffect(() => {
    let live = true;
    void getModelSettings().then((value) => { if (live) setPlatform(value); })
      .catch(() => { /* no configured platform model means no selector */ });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    let live = true;
    void getWorkspaces()
      .then((rows) => {
        if (!live) return;
        setWorkspaces(rows);
        setIntakeWorkspaceId((current) => current || rows[0]?.id || "");
      })
      .catch(() => { /* the intake selector stays empty and asks for a choice */ });
    return () => { live = false; };
  }, []);

  function chooseModel(value: string) {
    const profile = value as ModelProfile;
    if (profile === currentProfile) return;
    if (profile === "deepseek_flash" && savedModel?.profile === "deepseek_flash" && savedModel.api_key) {
      const preset = MODEL_PROFILES[profile];
      const next = { ...savedModel, profile, base_url: preset.base_url, model: preset.model };
      if (saveModelConfig(next, scope_id, deployment)) return;
    }
    onOpenModelSettings?.(profile);
  }

  const modelControl = modelOptions.length === 0 ? null : (
    <div className="composer-model">
      <span className="sr-only">{t("home.model")}</span>
      {modelOptions.length === 1 ? (
        <span className="composer-model-name">{modelOptions[0].label}</span>
      ) : (
        <SelectMenu value={currentProfile} options={modelOptions}
          ariaLabel={t("home.model")} className="composer-model-select"
          onChange={chooseModel} />
      )}
    </div>
  );

  async function readTasks() {
    setPanel("tasks"); setTasksError(false); setTasks(null);
    try {
      const rows = await api<Array<Record<string, unknown>>>("/api/odoo20/tasks");
      setTasks(rows.filter((row) => String(row.user_state ?? "") !== "completed"));
    } catch { setTasksError(true); }
  }

  async function searchBusiness() {
    setPanel("lookup"); setLookupBusy(true); setLookupError(false); setLookupRows(null);
    const query = encodeURIComponent(lookup.trim());
    const signal = AbortSignal.timeout(20_000);
    try {
      const [orders, stock] = await Promise.all([
        api<{ records: Array<Record<string, unknown>> }>(
          `/api/odoo20/business-data?kind=purchase_order&limit=50&q=${query}`, { signal }),
        api<{ records: Array<Record<string, unknown>> }>(
          `/api/odoo20/business-data?kind=stock&limit=50&q=${query}`, { signal }),
      ]);
      setLookupRows([
        ...orders.records.map((row) => ({ ...row, _kind: "order" })),
        ...stock.records.map((row) => ({ ...row, _kind: "stock" })),
      ]);
    } catch { setLookupRows(null); setLookupError(true); }
    finally { setLookupBusy(false); }
  }

  async function uploadMaterial(file: File | null): Promise<string> {
    if (!file) return "";
    const generation = ++uploadGeneration.current;
    setPanel("upload"); setUploaded(null); setUploadStatus("uploading");
    const form = new FormData();
    form.append("file", file);
    form.append("acl_scope", "project");
    try {
      const result = await api<{ source_path: string; job_id?: string }>("/api/knowledge/documents", {
        method: "POST", body: form, signal: AbortSignal.timeout(60_000),
      });
      if (generation === uploadGeneration.current) { setUploaded(result); setUploadStatus("processing"); }
      return t("home.uploadSubmitted");
    } catch (error) {
      if (generation === uploadGeneration.current) setUploadStatus("failed");
      throw error;
    }
  }

  async function saveMaterial(text: string) {
    const title = text.split("\n")[0].trim().slice(0, 120) || t("home.materialTitle");
    // The workspace is explicit.  A "newest draft" fallback attaches material to
    // whichever enterprise happens to be first on disk.
    if (!intakeWorkspaceId) throw new Error(t("home.intakeChoose"));
    const workspace = await intakeWorkspaceMaterial(intakeWorkspaceId, title, text);
    return t("home.materialSaved", { name: workspace.name });
  }

  return (
    <div className="view home-view" data-knowledge-view="true" data-home-view="true">
      <section className="home-hero">
        <h1>{mode === "intake" ? t("home.intakeTitle") : t("home.title")}</h1>
        {mode === "intake" ? (
          <div className="composer-workspace">
            {workspaces.length > 1 ? (
              <SelectMenu value={intakeWorkspaceId}
                ariaLabel={t("home.intakeWorkspace")}
                options={[{ value: "", label: t("home.intakeChoose") },
                  ...workspaces.map((workspace) => ({ value: workspace.id, label: workspace.name }))]}
                onChange={setIntakeWorkspaceId} />
            ) : (
              <span className="composer-workspace-name">
                {workspaces[0]?.name ?? t("home.intakeChoose")}
              </span>
            )}
          </div>
        ) : null}
        <div className="home-mode-switch" role="group" aria-label={t("assistant.composerMode")}>
          <button type="button" className={mode === "ask" ? "is-active" : ""} aria-pressed={mode === "ask"} onClick={() => setMode("ask")}>{t("assistant.modeAsk")}</button>
          <button type="button" className={mode === "intake" ? "is-active" : ""} aria-pressed={mode === "intake"} onClick={() => setMode("intake")}>{t("assistant.modeIntake")}</button>
        </div>

      </section>

      <ChatSurface
        responder={responder}
        cancelTurn={cancelAssistantTurn}
        storageScope={`${scope_id}:knowledge`}
        presets={[
          { icon: "cases", label: t("home.cardLookup"), description: t("home.cardLookupBody"),
            onSelect: () => setPanel("lookup") },
          { icon: "shield", label: t("home.cardException"), description: t("home.cardExceptionBody"),
            onSelect: () => navigate("#cases") },
          { icon: "server", label: t("home.cardUpload"), description: t("home.cardUploadBody"),
            onSelect: () => materialInput.current?.click() },
          { icon: "check", label: t("home.cardTasks"), description: t("home.cardTasksBody"),
            onSelect: () => void readTasks() },
        ]}
        emptyText={t("home.empty")}
        composerPlaceholder={t("home.composerPlaceholder")}
        composerAria={t("home.composerAria")}
        intake={saveMaterial}
        intakePlaceholder={t("assistant.intakePlaceholder")}
        mode={mode}
        modelControl={mode === "ask" ? modelControl : undefined}
        onDocument={(file) => uploadMaterial(file)}
        className="home-chat"
      />
      <input ref={materialInput} type="file" hidden
        accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.csv,.tsv,.json,.md,.markdown,.txt"
        onChange={(event) => { void uploadMaterial(event.target.files?.[0] ?? null).catch(() => undefined); event.currentTarget.value = ""; }} />
      {panel ? <section className="home-task-panel" data-home-task-panel={panel}>
        <div className="home-task-panel-head">
          <strong>{t(panel === "lookup" ? "home.cardLookup" : panel === "tasks" ? "home.cardTasks" : "home.cardUpload")}</strong>
          <button type="button" className="home-task-close" aria-label={t("home.panelClose")}
            onClick={() => { setPanel(null); setUploadStatus(""); }}>×</button>
        </div>
        {panel === "lookup" ? <>
          <div className="home-task-search">
            <input value={lookup} onChange={(event) => setLookup(event.target.value)}
              placeholder={t("home.lookupPlaceholder")} aria-label={t("home.lookupPlaceholder")} />
            <button type="button" className="primary" disabled={lookupBusy} onClick={() => void searchBusiness()}>
              {lookupBusy ? t("home.lookupLoading") : t("home.lookupButton")}
            </button>
          </div>
          {lookupError ? <p role="alert">{t("home.lookupError")}</p> : null}
          {lookupRows ? (() => {
            // The API also matches order-line products, which are not present
            // in these summary rows. Filtering again would discard real hits.
            const rows = [
              ...lookupRows.filter((row) => row._kind !== "stock").slice(0, 6),
              ...lookupRows.filter((row) => row._kind === "stock").slice(0, 6),
            ];
            return rows.length ? <ul className="home-task-list">{rows.map((row) =>
              <li key={`${String(row._kind)}-${String(row.id)}`}>
                <span>{String(row.name ?? row.id)}</span>
                <small>{row._kind === "stock"
                  ? `${t("home.stockAvailable")} ${String(row.qty_available ?? 0)}`
                  : `${Array.isArray(row.partner_id) ? String(row.partner_id[1]) : ""} · ${String(row.state)}`}</small></li>)}</ul>
              : <p>{t("home.lookupEmpty")}</p>;
          })() : null}
        </> : null}
        {panel === "tasks" ? (
          tasksError ? <p role="alert">{t("home.tasksError")} <button type="button" className="secondary" onClick={() => void readTasks()}>{t("home.tasksRetry")}</button></p>
          : tasks === null ? <p>{t("home.tasksLoading")}</p>
          : tasks.length === 0 ? <p>{t("home.tasksEmpty")}</p>
          : <ul className="home-task-list">{tasks.map((task) =>
              <li key={String(task.ticket_id ?? task.plan_id)}>
                <button type="button" className="home-task-link"
                  onClick={() => task.plan_id ? navigate(`#cases&plan=${String(task.plan_id)}`) : navigate("#cases")}>
                  {String(task.goal ?? task.ticket_id ?? "")}
                </button>
                <small>{String(task.user_state ?? task.state ?? "")}</small></li>)}</ul>
        ) : null}
        {panel === "upload" ? (
          <div>
            <p role="status">{t(uploadStatus === "ready" ? "home.uploadReady"
              : uploadStatus === "uploading" ? "assistant.documentUploading"
              : uploadStatus === "failed" ? "home.uploadFailed"
              : uploadStatus === "indexing" ? "home.uploadIndexing"
              : uploadStatus === "review" ? "home.uploadReview"
              : uploadStatus === "unavailable" ? "home.uploadStatusUnavailable"
              : uploadStatus === "background" ? "home.uploadBackground" : "home.uploadProcessing")}</p>
            {uploaded && ["unavailable", "background"].includes(uploadStatus)
              ? <button type="button" className="secondary" onClick={() => { setUploadStatus("processing"); setUploadCheck(value => value + 1); }}>{t("home.uploadCheck")}</button> : null}
          </div>
        ) : null}
      </section> : null}

    </div>
  );
}
