import { useCallback, useEffect, useRef, useState } from "react";
import { CasesView } from "./CasesView";
import { HomeView } from "./HomeView";
import { Login } from "./Login";
import { RunsView } from "./RunsView";
import { SetupView } from "./SetupView";
import { SettingsDialog } from "./SettingsDialog";
import { Sidebar, type WorkbenchView } from "./Sidebar";
import { api, ApiError, bindWorkspace, getConfig, getRuns, SESSION_CHANGE } from "./api";
import { useI18n } from "./i18n";
import { caseRoute, navigate, WorkspaceContext, type WorkspaceIdentity } from "./workspace";
import type { Config, RunSummary } from "./types";
import type { ModelProfile } from "./modelConfig";

export default function App() {
  const { t } = useI18n();
  const [config, setConfig] = useState<Config>({ deployment: "local", runner_enabled: false });
  const [identity, setIdentity] = useState<WorkspaceIdentity | null>(null);
  const [loading, setLoading] = useState(true);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runError, setRunError] = useState("");
  const [hash, setHash] = useState(location.hash);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsProfile, setSettingsProfile] = useState<ModelProfile | undefined>(undefined);
  useEffect(() => {
    const update = () => setHash(location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  const params = new URLSearchParams(hash.slice(1));
  const caseId = params.get("case");
  const runId = params.get("run");
  const planId = Number(params.get("plan") || 0);
  // Start page by default; the other views keep their explicit routes.
  const view: WorkbenchView = params.has("setup")
    ? "setup"
    : runId || params.has("runs")
      ? "runs"
      : caseId || params.has("cases")
        ? "cases"
        : "knowledge";
  const loadRuns = useCallback(async () => {
    try { setRuns(await getRuns()); setRunError(""); }
    catch (error) { setRunError(error instanceof Error ? error.message : "unavailable"); }
  }, []);
  const authGeneration = useRef(0);
  const authenticate = useCallback(async () => {
    const generation = ++authGeneration.current;
    try {
      const [me, cfg] = await Promise.all([api<WorkspaceIdentity>("/api/auth/me"), getConfig()]);
      if (generation !== authGeneration.current) return;
      bindWorkspace(me.scope_id); setIdentity(me); setConfig(cfg);
    } catch { if (generation === authGeneration.current) setIdentity(null); }
    finally { if (generation === authGeneration.current) setLoading(false); }
  }, []);
  useEffect(() => { void authenticate(); }, [authenticate]);
  useEffect(() => {
    let live = true;
    // Reload retires pending callbacks, dialogs and scoped drafts together.
    const leave = () => {
      if (!live) return;
      live = false;
      setLoading(true);
      window.history.replaceState(null, "", location.pathname + location.search);
      location.reload();
    };
    const verify = async () => {
      if (!identity) { await authenticate(); return; }
      try {
        const me = await api<WorkspaceIdentity>("/api/auth/me");
        if (me.scope_id !== identity.scope_id) leave();
      } catch (error) { if (error instanceof ApiError && error.status === 401) leave(); }
    };
    const visible = () => { if (!document.hidden) void verify(); };
    const changed = (event: StorageEvent) => {
      if (event.key !== SESSION_CHANGE) return;
      if (identity) leave();
      else void verify();
    };
    window.addEventListener(SESSION_CHANGE, leave);
    window.addEventListener("storage", changed);
    window.addEventListener("focus", visible);
    window.addEventListener("pageshow", visible);
    document.addEventListener("visibilitychange", visible);
    return () => {
      live = false;
      window.removeEventListener(SESSION_CHANGE, leave);
      window.removeEventListener("storage", changed);
      window.removeEventListener("focus", visible);
      window.removeEventListener("pageshow", visible);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [identity, authenticate]);
  useEffect(() => { if (identity) void loadRuns(); }, [identity, loadRuns]);
  if (loading) return <div className="loading">{t("app.loading")}</div>;
  if (!identity) return <div className="app login-only"><main className="workspace"><Login onDone={authenticate} /></main></div>;
  const switchView = (next: WorkbenchView) => navigate(next === "cases" ? "#cases" : next === "runs" ? "#runs" : next === "setup" ? "#setup" : "#home");
  return <WorkspaceContext.Provider value={identity}>
    <div className="app">
      <Sidebar runs={runs} view={view} onView={switchView} workbenches={config.workbenches}
        onOpenRun={(id) => navigate(`#run=${encodeURIComponent(id)}`)}
        onOpenSettings={() => { setSettingsProfile(undefined); setSettingsOpen(true); }} />
      <main className="workspace">
        <header className="topbar">
          <span className="topbar-title"><span className="topbar-brand">ForgeFlow</span><span className="chev" aria-hidden="true">/</span>
            <span className="topbar-view">{t(view === "setup" ? "app.topbarSetup" : view === "cases" ? "app.topbarCases" : view === "runs" ? "app.topbarEvidence" : "app.topbarKnowledge")}</span></span>
        </header>
        {config.deployment === "review" && <div className="review-banner">{t("app.reviewBanner")}</div>}
        <div className={`app-panel${view === "cases" ? " app-panel-workbench" : ""}`}>
          {view === "cases" ? <CasesView selectedId={caseId} chat={params.has("chat")} evidence={params.has("evidence")}
            demo={params.has("demo")} initialView={params.get("view")} planId={planId || null}
            onSelectCase={(id) => navigate(id ? caseRoute(id) : "#cases")}
            onAskCase={(item) => navigate(caseRoute(item.case_id, "chat"))} />
            : view === "setup" ? <SetupView />
              : view === "runs" ? <RunsView runs={runs} config={config} selected={runId} error={runError}
              onSelect={(id) => navigate(`#run=${encodeURIComponent(id)}`)} onRefresh={() => void loadRuns()} /> : <HomeView onOpenModelSettings={(profile) => { setSettingsProfile(profile); setSettingsOpen(true); }} />}
        </div>
      </main>
      {settingsOpen && <SettingsDialog initialProfile={settingsProfile} onClose={() => setSettingsOpen(false)} />}
    </div>
  </WorkspaceContext.Provider>;
}
