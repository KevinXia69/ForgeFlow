import { useCallback, useEffect, useState } from "react";
import { CasesView } from "./CasesView";
import { HomeView } from "./HomeView";
import { Login } from "./Login";
import { RunsView } from "./RunsView";
import { SettingsDialog } from "./SettingsDialog";
import { Sidebar, type WorkbenchView } from "./Sidebar";
import { api, getConfig, getRuns } from "./api";
import { useI18n } from "./i18n";
import { caseRoute, navigate, WorkspaceContext, type WorkspaceIdentity } from "./workspace";
import type { Config, RunSummary } from "./types";

export default function App() {
  const { t } = useI18n();
  const [config, setConfig] = useState<Config>({ deployment: "local", runner_enabled: false });
  const [identity, setIdentity] = useState<WorkspaceIdentity | null>(null);
  const [loading, setLoading] = useState(true);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runError, setRunError] = useState("");
  const [hash, setHash] = useState(location.hash);
  const [settingsOpen, setSettingsOpen] = useState(false);
  useEffect(() => {
    const update = () => setHash(location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  const params = new URLSearchParams(hash.slice(1));
  const caseId = params.get("case");
  const runId = params.get("run");
  // Start page by default: a bare entry point or #home/#knowledge shows the ask
  // surface. Cases, a case and evidence keep their own explicit routes.
  const view: WorkbenchView = runId || params.has("runs")
    ? "runs"
    : caseId || params.has("cases")
      ? "cases"
      : "knowledge";
  const loadRuns = useCallback(async () => {
    try { setRuns(await getRuns()); setRunError(""); }
    catch (error) { setRunError(error instanceof Error ? error.message : "unavailable"); }
  }, []);
  const authenticate = useCallback(async () => {
    try {
      const [me, cfg] = await Promise.all([api<WorkspaceIdentity>("/api/auth/me"), getConfig()]);
      setIdentity(me); setConfig(cfg);
    } catch { setIdentity(null); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void authenticate(); }, [authenticate]);
  useEffect(() => { if (identity) void loadRuns(); }, [identity, loadRuns]);
  if (loading) return <div className="loading">{t("app.loading")}</div>;
  if (!identity) return <div className="app login-only"><main className="workspace"><Login onDone={authenticate} /></main></div>;
  const switchView = (next: WorkbenchView) => navigate(next === "cases" ? "#cases" : next === "runs" ? "#runs" : "#home");
  return <WorkspaceContext.Provider value={identity}>
    <div className="app">
      <Sidebar runs={runs} view={view} onView={switchView}
        onOpenRun={(id) => navigate(`#run=${encodeURIComponent(id)}`)}
        onOpenSettings={() => setSettingsOpen(true)} review={config.deployment === "review"} />
      <main className="workspace">
        <header className="topbar">
          <span className="topbar-title"><span className="topbar-brand">ForgeFlow</span><span className="chev" aria-hidden="true">/</span>
            <span className="topbar-view">{t(view === "cases" ? "app.topbarCases" : view === "runs" ? "app.topbarEvidence" : "app.topbarKnowledge")}</span></span>
          <span className="status-pill">{t(config.deployment === "review" ? "app.topbarReviewStatus" : "app.topbarStatus")}</span>
        </header>
        {config.deployment === "review" && <div className="review-banner">{t("app.reviewBanner")}</div>}
        <div className={`app-panel${view === "cases" ? " app-panel-workbench" : ""}`}>
          {view === "cases" ? <CasesView selectedId={caseId} chat={params.has("chat")} evidence={params.has("evidence")}
            demo={params.has("demo")} initialView={params.get("view")}
            onSelectCase={(id) => navigate(id ? caseRoute(id) : "#cases")}
            onAskCase={(item) => navigate(caseRoute(item.case_id, "chat"))}
            onOpenEvidence={() => switchView("runs")} />
            : view === "runs" ? <RunsView runs={runs} config={config} selected={runId} error={runError}
              onSelect={(id) => navigate(`#run=${encodeURIComponent(id)}`)} onRefresh={() => void loadRuns()} /> : <HomeView />}
        </div>
      </main>
      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
    </div>
  </WorkspaceContext.Provider>;
}
