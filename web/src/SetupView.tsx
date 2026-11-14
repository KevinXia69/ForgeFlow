import { useEffect, useMemo, useId, useRef, useState } from "react";
import { addWorkspaceSource, ApiError, applyWorkspaceImport, authorizeWorkspaceImport, confirmWorkspace, createWorkspace, getOnboardingCompanies, getOnboardingTargets, getWorkspaces, previewWorkspaceImport, readbackWorkspaceImport } from "./api";
import { useI18n, type MessageKey } from "./i18n";
import type { OnboardingTarget, Workspace, WorkspaceMapping } from "./types";

type MappingDraft = WorkspaceMapping & { ignored: boolean };

const MODULES = [
  ["sales", "setup.moduleSales"],
  ["purchase", "setup.modulePurchase"],
  ["inventory", "setup.moduleInventory"],
  ["manufacturing", "setup.moduleManufacturing"],
  ["knowledge", "setup.moduleKnowledge"],
] as const;

function asDraft(mapping: WorkspaceMapping): MappingDraft {
  return { ...mapping, ignored: mapping.status === "ignored" };
}

async function sourcePayload(file: File): Promise<{ filename: string; media_type: string; content: string }> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes.byteLength > 5 * 1024 * 1024) throw new Error("source_too_large");
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return { filename: file.name, media_type: file.type || "application/octet-stream", content: btoa(binary) };
}


const STATUS_LABEL: Record<string, MessageKey> = {
  draft: "setup.draft",
  mapping_confirmed: "setup.mappingConfirmed",
  import_authorized: "setup.importAuthorized",
  imported: "setup.imported",
  import_partial: "setup.importPartial",
  readback_passed: "setup.readbackPassed",
  readback_failed: "setup.readbackFailed",
};

function statusKey(status: string): MessageKey {
  return STATUS_LABEL[status] ?? "setup.draft";
}

function errorCode(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "unknown";
}

/**
 * Tenant-context setup. It proposes business-model field mappings and requires
 * explicit mapping confirmation and authorization before importing ERP records.
 */
export function SetupView() {
  const { t } = useI18n();
  const mappingPrefix = useId();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [targets, setTargets] = useState<OnboardingTarget[]>([]);
  const [companies, setCompanies] = useState<{ id: number; name: string }[]>([]);
  const [importCompanyId, setImportCompanyId] = useState(0);
  const [canAuthorizeImport, setCanAuthorizeImport] = useState(false);
  const [importAccessError, setImportAccessError] = useState(false);
  const [selected, setSelected] = useState<Workspace | null>(null);
  const [mappings, setMappings] = useState<MappingDraft[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [industry, setIndustry] = useState("");
  const [description, setDescription] = useState("");
  const [modules, setModules] = useState<string[]>(["sales", "purchase", "inventory"]);
  const fileInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let live = true;
    void getOnboardingCompanies().then((access) => {
      if (!live) return;
      setCompanies(access.companies);
      setCanAuthorizeImport(access.can_authorize);
      if (access.companies.length === 1) setImportCompanyId((current) => current || access.companies[0].id);
    }).catch(() => { if (live) setImportAccessError(true); });
    void Promise.all([getWorkspaces(), getOnboardingTargets()])
      .then(([rows, registry]) => {
        if (!live) return;
        setWorkspaces(rows);
        setTargets(registry.models);
        if (rows[0]) selectWorkspace(rows[0]);
      })
      .catch((failure) => { if (live) setError(errorCode(failure)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, []);

  function applyWorkspace(workspace: Workspace) {
    setSelected(workspace);
    setImportCompanyId(workspace.import?.preview?.access_scope?.company_id
      ?? (companies.length === 1 ? companies[0].id : 0));
    setMappings(workspace.mappings.map(asDraft));
    setWorkspaces((rows) => rows.map((row) => row.id === workspace.id ? workspace : row));
  }

  function selectWorkspace(workspace: Workspace) {
    applyWorkspace(workspace);
    setError("");
  }

  async function refresh(id?: string) {
    const rows = await getWorkspaces();
    setWorkspaces(rows);
    const next = rows.find((row) => row.id === id) ?? rows[0] ?? null;
    if (next) selectWorkspace(next);
    else setSelected(null);
  }

  async function create() {
    if (!name.trim() || !industry.trim() || busy) return;
    setBusy("create"); setError("");
    try {
      const workspace = await createWorkspace({ name: name.trim(), industry: industry.trim(), description: description.trim(), modules });
      setName(""); setIndustry(""); setDescription("");
      await refresh(workspace.id);
    } catch (failure) { setError(errorCode(failure)); }
    finally { setBusy(""); }
  }

  async function upload(file: File | null) {
    if (!file || !selected || busy || selected.status !== "draft") return;
    setBusy("source"); setError("");
    try {
      const workspace = await addWorkspaceSource(selected.id, await sourcePayload(file));
      applyWorkspace(workspace);
    } catch (failure) { setError(errorCode(failure)); }
    finally { setBusy(""); if (fileInput.current) fileInput.current.value = ""; }
  }

  function updateMapping(id: string, patch: Partial<MappingDraft>) {
    setMappings((rows) => rows.map((row) => row.id === id ? { ...row, ...patch } : row));
  }

  async function confirm() {
    if (!selected || busy || selected.status !== "draft") return;
    if (mappings.some((row) => !row.ignored && (!row.target_model || !row.target_field))) return;
    setBusy("confirm"); setError("");
    try {
      const workspace = await confirmWorkspace(selected.id, mappings.map((row) => row.ignored
        ? { id: row.id, ignored: true }
        : { id: row.id, target_model: row.target_model, target_field: row.target_field }));
      applyWorkspace(workspace);
    } catch (failure) { setError(errorCode(failure)); }
    finally { setBusy(""); }
  }

  async function previewImport() {
    if (!selected || busy || !importCompanyId) return;
    setBusy("preview"); setError("");
    try { applyWorkspace(await previewWorkspaceImport(selected.id, importCompanyId)); }
    catch (failure) { setError(errorCode(failure)); }
    finally { setBusy(""); }
  }

  async function authorizeImport() {
    const digest = selected?.import?.preview?.preview_sha256;
    if (!selected || !digest || busy) return;
    setBusy("authorize"); setError("");
    try { applyWorkspace(await authorizeWorkspaceImport(selected.id, digest)); }
    catch (failure) { setError(errorCode(failure)); }
    finally { setBusy(""); }
  }

  async function applyImport() {
    const digest = selected?.import?.preview?.preview_sha256;
    if (!selected || !digest || busy) return;
    setBusy("apply"); setError("");
    try { applyWorkspace(await applyWorkspaceImport(selected.id, digest)); }
    catch (failure) { setError(errorCode(failure)); }
    finally { setBusy(""); }
  }

  async function readbackImport() {
    if (!selected || busy) return;
    setBusy("readback"); setError("");
    try { applyWorkspace(await readbackWorkspaceImport(selected.id)); }
    catch (failure) { setError(errorCode(failure)); }
    finally { setBusy(""); }
  }

  const sources = selected?.sources ?? [];
  const fieldsFor = (model: string) => targets.find((row) => row.model === model)?.fields ?? [];
  const complete = mappings.length > 0 && mappings.every((row) => row.ignored || (row.target_model && row.target_field));
  const sourceLabel = useMemo(() => (sourceId: string) => sources.find((row) => row.id === sourceId)?.filename ?? "", [sources]);

  if (loading) return <div className="view setup-view"><p className="muted">{t("setup.loading")}</p></div>;

  return (
    <div className="view setup-view" data-setup-view="true">
      <header className="setup-head">
        <div>
          <h1>{t("setup.title")}</h1>
        </div>
      </header>

      {error ? <p className="setup-error" role="alert">{error}</p> : null}

      <div className="setup-layout">
        <aside className="setup-side">
          <h2>{t("setup.workspaces")}</h2>
          {workspaces.length === 0 ? null : (
            <div className="setup-workspaces">
              {workspaces.map((workspace) => (
                <button type="button" key={workspace.id} className={`setup-workspace${selected?.id === workspace.id ? " is-active" : ""}`}
                  onClick={() => selectWorkspace(workspace)}>
                  <strong>{workspace.name}</strong>
                  <span>{workspace.industry}</span>
                  <small>{t(statusKey(workspace.status))} · v{workspace.version}</small>
                </button>
              ))}
            </div>
          )}
          <section className="setup-create">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder={t("setup.name")} aria-label={t("setup.name")} maxLength={120} />
            <input value={industry} onChange={(event) => setIndustry(event.target.value)} placeholder={t("setup.industry")} aria-label={t("setup.industry")} maxLength={120} />
            <textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder={t("setup.description")} aria-label={t("setup.description")} rows={2} maxLength={1000} />
            <div className="setup-modules">
              {MODULES.map(([id, key]) => (
                <label key={id}>
                  <input type="checkbox" checked={modules.includes(id)}
                    onChange={(event) => setModules((current) => event.target.checked ? [...current, id] : current.filter((item) => item !== id))} />
                  {t(key)}
                </label>
              ))}
            </div>
            <button type="button" className="primary" onClick={() => void create()} disabled={busy !== "" || !name.trim() || !industry.trim()}>
              {busy === "create" ? t("setup.creating") : t("setup.create")}
            </button>
          </section>
        </aside>

        <main className="setup-main">
          {!selected ? null : (
            <>
              <section className="setup-panel">
                <div className="setup-panel-head">
                  <div>
                    <h2>{selected.name}</h2>
                    <p className="muted">{selected.industry} · {selected.modules.map((id) => MODULES.find(([key]) => key === id)?.[1] ? t(MODULES.find(([key]) => key === id)![1]) : id).join(" · ")}</p>
                  </div>
                  <span className={`setup-status is-${selected.status}`} data-business-status>
                    {t(selected.import?.readback?.status === "readback_passed" ? "setup.businessReady"
                      : selected.status === "readback_failed" || selected.status === "import_partial" ? "setup.businessFailed"
                      : selected.status === "draft" || selected.status === "mapping_confirmed" ? "setup.businessNeedsConfirmation"
                      : "setup.businessProcessing")}
                  </span>
                </div>

                {selected.status === "draft" ? (
                  <div className="setup-upload">
                    <input ref={fileInput} type="file" hidden accept=".csv,.tsv,.json,.txt,.md,.xlsx,.pdf"
                      onChange={(event) => void upload(event.target.files?.[0] ?? null)} />
                    <button type="button" className="secondary" onClick={() => fileInput.current?.click()} disabled={busy !== ""}>
                      {busy === "source" ? t("setup.uploading") : t("setup.upload")}
                    </button>
                  </div>
                ) : null}
                <h3>{t("setup.sources")}</h3>
                {sources.length === 0 ? null : (
                  <div className="setup-sources">
                    {sources.map((source) => (
                      <article key={source.id} className="setup-source">
                        <strong>{source.filename}</strong>
                        <span>{source.kind.toUpperCase()} · {source.row_count} {t("setup.rows")}</span>
                        {source.headers.length > 0 ? <small>{source.headers.join(" · ")}</small> : null}
                      </article>
                    ))}
                  </div>
                )}
              </section>

              <section className="setup-panel">
                <div className="setup-panel-head">
                  <div><h2>{t("setup.mappingTitle")}</h2></div>
                  <span className="setup-count">{mappings.filter((row) => row.ignored || (row.target_model && row.target_field)).length}/{mappings.length}</span>
                </div>
                {mappings.length === 0 ? null : (
                  <div className="setup-mapping-list">
                    {mappings.map((row) => {
                      const sourceId = `${mappingPrefix}-${encodeURIComponent(row.id)}-source`;
                      const sampleId = `${sourceId}-sample`;
                      const sourceName = `${sourceLabel(row.source_id)} · ${row.source_field}`;
                      return <article className={`setup-mapping${row.ignored ? " is-ignored" : ""}`} key={row.id}
                        role="group" aria-labelledby={sourceId} data-mapping-id={row.id}>
                        <div className="setup-mapping-source" id={sourceId}>
                          <strong>{row.source_field}</strong>
                          <span>{sourceLabel(row.source_id)}</span>
                          <small id={sampleId}>{row.sample || t("setup.noSample")}</small>
                        </div>
                        <select aria-label={`${t("setup.targetModel")} · ${sourceName}`} aria-describedby={sampleId} value={row.target_model}
                          disabled={row.ignored || selected.status !== "draft"}
                          onChange={(event) => {
                            const model = event.target.value;
                            updateMapping(row.id, { target_model: model, target_field: fieldsFor(model)[0] ?? "" });
                          }}>
                          <option value="">{t("setup.chooseModel")}</option>
                          {targets.map((target) => <option value={target.model} key={target.model}>{target.label}</option>)}
                        </select>
                        <select aria-label={`${t("setup.targetField")} · ${sourceName}`} aria-describedby={sampleId} value={row.target_field}
                          disabled={row.ignored || !row.target_model || selected.status !== "draft"}
                          onChange={(event) => updateMapping(row.id, { target_field: event.target.value })}>
                          <option value="">{t("setup.chooseField")}</option>
                          {fieldsFor(row.target_model).map((field) => <option value={field} key={field}>{field}</option>)}
                        </select>
                        <label className="setup-ignore">
                          <input type="checkbox" aria-label={`${t("setup.ignore")} · ${sourceName}`} aria-describedby={sampleId}
                            checked={row.ignored} disabled={selected.status !== "draft"}
                            onChange={(event) => updateMapping(row.id, { ignored: event.target.checked })} />
                          {t("setup.ignore")}
                        </label>
                      </article>;
                    })}
                  </div>
                )}
                {selected.status === "draft" ? (
                  <div className="setup-actions">
                    <button type="button" className="primary" disabled={busy !== "" || !complete} onClick={() => void confirm()}>
                      {busy === "confirm" ? t("setup.confirming") : t("setup.confirm")}
                    </button>
                  </div>
                ) : null}
              </section>

              <section className="setup-panel" data-setup-import="true">
                <div className="setup-panel-head">
                  <div><h2>{t("setup.importTitle")}</h2></div>
                  <span className="setup-count">{selected.states?.import_executed ? t("setup.importExecuted") : t("setup.notImported")}</span>
                </div>
                <p className="decision-note">{t("setup.mappingConfirmedNote")}</p>
                {importAccessError ? <p role="status">{t("setup.importAccessRequired")}</p> : null}
                {selected.status === "mapping_confirmed" ? (
                  <label>{t("setup.importCompany")}
                    <select data-import-company aria-label={t("setup.importCompany")}
                      value={importCompanyId || ""} disabled={busy !== "" || !companies.length}
                      onChange={(event) => {
                        setImportCompanyId(Number(event.target.value));
                        setSelected({ ...selected, import: null });
                      }}>
                      <option value="">{t("setup.chooseImportCompany")}</option>
                      {companies.map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}
                    </select>
                  </label>
                ) : null}
                {selected.status === "mapping_confirmed" && !selected.import?.preview ? (
                  <div className="setup-actions">
                    <button type="button" data-import-preview className="secondary" disabled={busy !== "" || !importCompanyId || !companies.length} onClick={() => void previewImport()}>
                      {busy === "preview" ? t("setup.previewingImport") : t("setup.previewImport")}
                    </button>
                  </div>
                ) : null}
                {selected.import?.preview ? (
                  <>
                    <ul className="setup-import-counts">
                      <li>{t("setup.importCreated")}: {selected.import.preview.counts.create ?? 0}</li>
                      <li>{t("setup.importUpdated")}: {selected.import.preview.counts.update ?? 0}</li>
                      <li>{t("setup.importUnchanged")}: {selected.import.preview.counts.unchanged ?? 0}</li>
                      <li>{t("setup.importConflict")}: {selected.import.preview.counts.conflict ?? 0}</li>
                    </ul>
                    {selected.import.preview.blocking.length > 0 ? (
                      <ul className="setup-import-blocking">
                        {selected.import.preview.blocking.map((item, index) => (
                          <li key={`${item.code}:${item.source_id ?? ""}:${index}`}>{item.code}{item.field ? `: ${item.field}` : ""}</li>
                        ))}
                      </ul>
                    ) : null}
                    {selected.status === "mapping_confirmed" ? (
                      <div className="setup-actions">
                        <button type="button" data-import-authorize className="primary" disabled={busy !== "" || !canAuthorizeImport || !selected.import.preview.importable} onClick={() => void authorizeImport()}>
                          {busy === "authorize" ? t("setup.authorizingImport") : t("setup.authorizeImport")}
                        </button>
                      </div>
                    ) : null}
                    {selected.status === "import_authorized" || selected.status === "import_partial"
                      || (selected.status === "readback_failed" && (selected.import.execution?.counts.failed ?? 0) > 0) ? (
                      <div className="setup-actions">
                        <button type="button" data-import-apply className="primary" disabled={busy !== "" || !canAuthorizeImport} onClick={() => void applyImport()}>
                          {busy === "apply" ? t("setup.applyingImport") : t("setup.applyImport")}
                        </button>
                      </div>
                    ) : null}
                  </>
                ) : null}
                {selected.status === "imported" || selected.status === "import_partial" || selected.status === "readback_failed" ? (
                  <div className="setup-actions">
                    <button type="button" className="secondary" disabled={busy !== ""} onClick={() => void readbackImport()}>
                      {busy === "readback" ? t("setup.readingBackImport") : t("setup.readbackImport")}
                    </button>
                  </div>
                ) : null}
                {selected.import?.readback ? (
                  <p className="decision-note">{selected.import.readback.status === "readback_passed" ? t("setup.readbackPassed") : t("setup.readbackFailed")} · {selected.import.readback.rows.filter((row) => row.readback === "matched").length}/{selected.import.readback.rows.length}</p>
                ) : null}
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
