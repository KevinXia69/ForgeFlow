import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { useI18n, type MessageKey } from "./i18n";

type PanelId = "connection" | "parse" | "index" | "rules" | "recovery" | "business";
type PanelState = {
  status: "loading" | "ready" | "forbidden" | "error";
  code?: string;
};
type Health = { healthy?: boolean; services?: { kind?: string }[] };
type Subscription = { source_type: string; enabled: boolean; interval_seconds: number };
type ParseJob = { job_id: string; state: string; source_path: string; attempts: number; error?: string | null; blocks: number };
type Document = { source_path: string; parse_status: string; indexed_generation?: string | null; acl_scope: string; block_count: number };
type Rule = { id: number; name: string; version: number; source_ref?: string | null };
type Failure = { ticket_id: string; goal: string; need?: string };
type Restore = { paused: boolean; reason?: string | null };
type BusinessRecord = { id: number; name?: string; state?: string; partner_id?: [number, string];
  date_planned?: string; commitment_date?: string; product_tmpl_id?: [number, string];
  product_qty?: number; components?: { product_id: [number, string]; quantity: number }[] };
type Block = { ord: number; block_type: string; text: string; locator: string; page?: number | null };

const STATE_KEY: Record<PanelState["status"], MessageKey> = {
  loading: "setupOps.state.loading", ready: "setupOps.state.ready",
  forbidden: "setupOps.state.forbidden", error: "setupOps.state.error",
};

const INITIAL: Record<PanelId, PanelState> = {
  connection: { status: "loading" }, parse: { status: "loading" },
  index: { status: "loading" }, rules: { status: "loading" },
  recovery: { status: "loading" }, business: { status: "loading" },
};

function failureState(error: unknown): PanelState {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    return { status: "forbidden", code: error.message };
  }
  return { status: "error", code: error instanceof Error ? error.message : "unknown" };
}

/**
 * Actionable setup operations: connection, parsing, indexing, rules and error
 * recovery. Each panel loads and fails independently so one unavailable service
 * cannot hide the rest, and every state is explicit rather than a spinner.
 */
export function SetupOps() {
  const { t } = useI18n();
  const [panels, setPanels] = useState<Record<PanelId, PanelState>>(INITIAL);
  const [health, setHealth] = useState<Health | null>(null);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [binding, setBinding] = useState<{ bound: boolean; role?: string | null;
    odoo_user_id?: number | null } | null>(null);
  const [jobs, setJobs] = useState<ParseJob[]>([]);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [failures, setFailures] = useState<Failure[]>([]);
  const [restore, setRestore] = useState<Restore | null>(null);
  const [businessKind, setBusinessKind] = useState("purchase_order");
  const [business, setBusiness] = useState<BusinessRecord[]>([]);
  const [sourceView, setSourceView] = useState<{ path: string; blocks: Block[] } | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [aclScope, setAclScope] = useState("project");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async <T,>(id: PanelId, path: string,
                                        apply: (value: T) => void) => {
    setPanels((current) => ({ ...current, [id]: { status: "loading" } }));
    try {
      apply(await api<T>(path));
      setPanels((current) => ({ ...current, [id]: { status: "ready" } }));
    } catch (failure) {
      setPanels((current) => ({ ...current, [id]: failureState(failure) }));
    }
  }, []);

  const loadAll = useCallback(() => {
    void load<Health>("connection", "/api/sources/health", setHealth);
    void api<{ bound: boolean; role?: string | null; odoo_user_id?: number | null }>(
      "/api/odoo20/binding").then(setBinding).catch(() => setBinding(null));
    void api<Subscription[]>("/api/sources/subscriptions")
      .then(setSubscriptions).catch(() => setSubscriptions([]));
    void load<ParseJob[]>("parse", "/api/knowledge/parse-jobs", setJobs);
    void load<Document[]>("index", "/api/knowledge/documents", setDocuments);
    void load<Rule[]>("rules", "/api/rules", setRules);
    void load<Failure[]>("recovery", "/api/tasks/failures", setFailures);
    void load<{ records: BusinessRecord[] }>(
      "business", `/api/odoo20/business-data?kind=${businessKind}`,
      (value) => setBusiness(value.records ?? []));
    void api<Restore>("/api/restore/status").then(setRestore).catch(() => setRestore(null));
  }, [load, businessKind]);

  useEffect(() => { loadAll(); }, [loadAll]);

  async function action(key: string, path: string, method = "POST", body?: unknown) {
    if (busy) return;
    setBusy(key); setNotice("");
    try {
      await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
      setNotice(t("setupOps.done"));
      loadAll();
    } catch (failure) {
      setNotice(failure instanceof ApiError && failure.status === 403
        ? t("setupOps.forbidden") : t("setupOps.failed"));
    } finally { setBusy(""); }
  }

  const panel = (id: PanelId, title: string, body: React.ReactNode) => {
    const state = panels[id];
    return <section className="setup-ops-panel" data-setup-panel={id}
                     data-panel-state={state.status}>
      <div className="setup-ops-head"><h3>{title}</h3>
        <span className="muted">{t(STATE_KEY[state.status])}</span></div>
      {state.status === "loading" ? <p className="muted" data-panel-message="loading">{t("setupOps.loading")}</p> : null}
      {state.status === "forbidden" ? <p role="alert" data-panel-message="forbidden">{t("setupOps.forbidden")}</p> : null}
      {state.status === "error" ? <p role="alert" data-panel-message="error">
        {t("setupOps.error", { code: state.code ?? "unknown" })}
        <button type="button" className="secondary" onClick={() => loadAll()}>{t("setupOps.retryLoad")}</button>
      </p> : null}
      {state.status === "ready" ? body : null}
    </section>;
  };

  const empty = (empty: boolean, key: MessageKey, children: React.ReactNode) =>
    empty ? <p className="muted" data-panel-empty="true">{t(key)}</p> : children;

  async function uploadDocument() {
    if (!uploadFile || busy) return;
    setBusy("upload"); setNotice("");
    try {
      const form = new FormData();
      form.append("file", uploadFile);
      form.append("acl_scope", aclScope);
      await api("/api/knowledge/documents", { method: "POST", body: form });
      setUploadFile(null);
      setNotice(t("setupOps.uploaded"));
      loadAll();
    } catch (failure) {
      setNotice(failure instanceof ApiError && failure.status === 403
        ? t("setupOps.forbidden") : t("setupOps.failed"));
    } finally { setBusy(""); }
  }

  async function showSource(path: string) {
    if (busy) return;
    setBusy(`source:${path}`); setNotice("");
    try {
      const value = await api<{ blocks: Block[] }>(
        `/api/knowledge/documents/${encodeURIComponent(path)}/blocks`);
      setSourceView({ path, blocks: value.blocks });
    } catch (failure) {
      setNotice(failure instanceof ApiError && failure.status === 404
        ? t("setupOps.noPermissionSource") : t("setupOps.failed"));
    } finally { setBusy(""); }
  }

  return <section className="setup-ops" data-setup-ops="true">
    <div className="setup-ops-bar">
      <h2>{t("setupOps.title")}</h2>
      <button type="button" className="secondary" disabled={busy !== ""}
        onClick={() => loadAll()}>{t("setupOps.reconnect")}</button>
    </div>
    {notice ? <p role="status">{notice}</p> : null}
    <div className="setup-ops-grid">
      {panel("connection", t("setupOps.connection"), <>
        {empty(!health, "setupOps.noHealth", <p>
          <strong>{health?.healthy ? t("setupOps.connected") : t("setupOps.disconnected")}</strong>
          {" · "}{t("setupOps.sources", { count: subscriptions.filter(row => row.enabled).length })}
        </p>)}
        <p data-odoo-binding={binding?.bound ? "bound" : "unbound"}>
          {binding?.bound
            ? t("setupOps.boundAs", { role: binding.role ?? "", user: binding.odoo_user_id ?? "" })
            : t("setupOps.notBound")}
        </p>
        <button type="button" className="secondary" disabled={busy !== ""}
          onClick={() => action("scan", "/api/sources/scan")}>{t("setupOps.scan")}</button>
      </>)}
      {panel("parse", t("setupOps.parse"), empty(jobs.length === 0, "setupOps.noJobs", <ul>
        {jobs.slice(0, 6).map((job) => <li key={job.job_id}>
          <span>{job.source_path}<small>{job.state} · {job.attempts} · {job.blocks}</small></span>
          {job.state === "failed" ? <button type="button" className="secondary"
            onClick={() => action(`parse:${job.job_id}`,
              `/api/knowledge/parse-jobs/${encodeURIComponent(job.job_id)}/retry`)}>
            {t("setupOps.retry")}</button> : null}
        </li>)}
      </ul>))}
      {panel("index", t("setupOps.index"), <>
        <div className="setup-ops-upload">
          <input type="file" aria-label={t("setupOps.chooseDocument")}
            accept=".pdf,.docx,.xlsx,.csv,.tsv,.md,.txt"
            onChange={(event) => setUploadFile(event.target.files?.[0] ?? null)} />
          <select aria-label={t("setupOps.aclScope")} value={aclScope}
            onChange={(event) => setAclScope(event.currentTarget.value)}>
            <option value="project">{t("setupOps.aclProject")}</option>
            <option value="engineering">{t("setupOps.aclEngineering")}</option>
          </select>
          <button type="button" className="secondary"
            disabled={busy !== "" || !uploadFile}
            onClick={() => void uploadDocument()}>{t("setupOps.uploadDocument")}</button>
          <button type="button" className="secondary" disabled={busy !== ""}
            onClick={() => action("publish", "/api/knowledge/publish")}>
            {t("setupOps.publishAll")}</button>
        </div>
        {empty(documents.length === 0, "setupOps.noDocuments", <ul>
        {documents.slice(0, 6).map((document) => <li key={document.source_path}>
          <span>{document.source_path}<small>{document.parse_status} · {document.block_count} · {t(document.acl_scope === "engineering" ? "setupOps.aclEngineering" : "setupOps.aclProject")}</small></span>
          <button type="button" className="secondary"
            onClick={() => action(`index:${document.source_path}`,
              `/api/knowledge/documents/${encodeURIComponent(document.source_path)}/index`)}>
            {t("setupOps.reindex")}</button>
          <button type="button" className="secondary" disabled={busy !== ""}
            onClick={() => void showSource(document.source_path)}>
            {t("setupOps.viewSource")}</button>
        </li>)}
      </ul>)}
      </>)}
      {panel("rules", t("setupOps.rules"), empty(rules.length === 0, "setupOps.noRules", <ul>
        {rules.slice(0, 6).map((rule) => <li key={`${rule.id}:${rule.version}`}>
          <span>{rule.name}<small>v{rule.version} · {rule.source_ref ?? ""}</small></span>
          <button type="button" className="secondary"
            onClick={() => action(`rule:${rule.id}`,
              `/api/rules/${rule.id}/confirm`)}>
            {t("setupOps.confirmRule")}</button></li>)}
      </ul>))}
      {panel("recovery", t("setupOps.recovery"), <>
        {empty(failures.length === 0, "setupOps.noFailures", <ul>
          {failures.slice(0, 6).map((failure) => <li key={failure.ticket_id}>
            <span>{failure.goal}<small>{failure.need ?? ""}</small></span>
            <button type="button" className="secondary"
              onClick={() => action(`task:${failure.ticket_id}`,
                `/api/tasks/${failure.ticket_id}/retry`)}>{t("setupOps.retry")}</button>
          </li>)}
        </ul>)}
        {restore ? <p>
          {restore.paused ? t("setupOps.restorePaused", { reason: restore.reason ?? "" })
            : t("setupOps.restoreReady")}
          {restore.paused ? <button type="button" className="secondary"
            onClick={() => action("restore", "/api/restore/verify")}>
            {t("setupOps.verifyRestore")}</button> : null}
        </p> : null}
      </>)}
      {panel("business", t("setupOps.business"), <>
        <div className="setup-ops-tabs">
          {(["purchase_order", "sale_order", "bom"] as const).map((kind) => (
            <button key={kind} type="button"
              className={businessKind === kind ? "is-active" : ""}
              onClick={() => setBusinessKind(kind)}>
              {t(`setupOps.business.${kind}`)}</button>
          ))}
        </div>
        {empty(business.length === 0, "setupOps.noBusiness", <ul>
          {business.slice(0, 8).map((row) => <li key={`${businessKind}:${row.id}`}>
            <span>{row.name ?? row.product_tmpl_id?.[1] ?? row.id}
              <small>{row.state ?? ""} {row.partner_id?.[1] ?? ""}
                {row.product_qty ? ` · ${row.product_qty}` : ""}
                {row.components?.length ? ` · ${t("setupOps.components", { count: row.components.length })}` : ""}
              </small></span>
            {row.components?.length ? <small>{row.components.slice(0, 4).map(
              (item) => `${item.product_id[1]}×${item.quantity}`).join(" · ")}</small> : null}
          </li>)}
        </ul>)}
      </>)}
    </div>
    {sourceView ? <div className="setup-ops-source" data-source-view="true">
      <div className="setup-ops-head"><h3>{sourceView.path}</h3>
        <button type="button" className="secondary" onClick={() => setSourceView(null)}>
          {t("setupOps.close")}</button></div>
      <div>{sourceView.blocks.slice(0, 200).map((block) => (
        <p key={block.ord}><small>{block.locator} · {block.block_type}</small>{block.text}</p>
      ))}</div>
    </div> : null}
  </section>;
}
