import { useEffect, useState } from "react";
import logo from "./assets/forgeflow-logo.png";
import { Tooltip } from "@forgeflow/dsh-primitives";
import { useI18n } from "./i18n";
import { runTaskLabel } from "./runLabels";
import type { RunSummary } from "./types";

export type WorkbenchView = "cases" | "runs" | "knowledge";

type Props = {
  runs: RunSummary[];
  view: WorkbenchView;
  onView: (view: WorkbenchView) => void;
  onOpenRun: (runId: string) => void;
  onOpenSettings: () => void;
  review?: boolean;
};

export function Sidebar({
  runs,
  view,
  onView,
  onOpenRun,
  onOpenSettings,
  review = false,
}: Props) {
  const { t } = useI18n();
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem("forgeflow.sidebarCollapsed") === "true";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem("forgeflow.sidebarCollapsed", String(collapsed));
    } catch {
      // Storage is optional; the current session still works.
    }
  }, [collapsed]);

  return (
    <aside
      className={`sidebar${collapsed ? " is-collapsed" : ""}`}
      aria-label={t("sidebar.ariaLabel")}
    >
      <div className="sidebar-head">
        <a className="brand" href="#home" aria-label={t("sidebar.brandHome")}>
          <img className="brand-logo" src={logo} alt="" aria-hidden="true" />
          <span className="brand-name">{t("sidebar.brandName")}</span>
        </a>
        <Tooltip label={t(collapsed ? "sidebar.expand" : "sidebar.collapse")} side="bottom">
          <button
            className="sidebar-toggle"
            type="button"
            data-sidebar-toggle="true"
            aria-expanded={!collapsed}
            aria-label={t(collapsed ? "sidebar.expand" : "sidebar.collapse")}
            onClick={() => setCollapsed((value) => !value)}
          >
            <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
              <rect x="2.5" y="3.5" width="15" height="13" rx="2" fill="none" stroke="currentColor" strokeWidth="1.4" />
              <path d="M8 3.8v12.4" fill="none" stroke="currentColor" strokeWidth="1.4" />
            </svg>
          </button>
        </Tooltip>
      </div>

      <nav className="nav">
        <button
          className={`nav-item${view === "knowledge" ? " is-active" : ""}`}
          type="button"
          data-nav="knowledge"
          aria-current={view === "knowledge" ? "page" : undefined}
          aria-label={t("sidebar.navKnowledge")}
          title={t("sidebar.navKnowledge")}
          onClick={() => onView("knowledge")}
        >
          <span className="nav-icon" aria-hidden="true">
            <svg viewBox="0 0 20 20" fill="none">
              <path d="M4 4.75h12a1.75 1.75 0 0 1 1.75 1.75v6.5A1.75 1.75 0 0 1 16 14.75H9l-3.4 2.5v-2.5H4a1.75 1.75 0 0 1-1.75-1.75v-6.5A1.75 1.75 0 0 1 4 4.75Z" stroke="currentColor" strokeWidth="1.45" strokeLinejoin="round" />
              <circle cx="7" cy="9.8" r=".9" fill="currentColor" />
              <circle cx="10" cy="9.8" r=".9" fill="currentColor" />
              <circle cx="13" cy="9.8" r=".9" fill="currentColor" />
            </svg>
          </span>
          <span className="nav-label">{t("sidebar.navKnowledge")}</span>
        </button>
        {view === "knowledge" ? <div className="side-chat" data-chat-history-slot="true" /> : null}
        <button
          className={`nav-item${view === "cases" ? " is-active" : ""}`}
          type="button"
          data-nav="cases"
          aria-current={view === "cases" ? "page" : undefined}
          aria-label={t("sidebar.navCases")}
          title={t("sidebar.navCases")}
          onClick={() => onView("cases")}
        >
          <span className="nav-icon" aria-hidden="true">▤</span>
          <span className="nav-label">{t("sidebar.navCases")}</span>
        </button>
        <button
          className={`nav-item${view === "runs" ? " is-active" : ""}`}
          type="button"
          data-nav="runs"
          aria-current={view === "runs" ? "page" : undefined}
          aria-label={t("sidebar.navEvidence")}
          title={t("sidebar.navEvidence")}
          onClick={() => onView("runs")}
        >
          <span className="nav-icon" aria-hidden="true">◫</span>
          <span className="nav-label">{t("sidebar.navEvidence")}</span>
        </button>
        <button
          className="nav-item settings-trigger"
          type="button"
          aria-label={t("sidebar.settings")}
          title={t("sidebar.settings")}
          onClick={onOpenSettings}
        >
          <span className="nav-icon" aria-hidden="true">⚙</span>
          <span className="nav-label">{t("sidebar.settings")}</span>
        </button>
      </nav>

      {view === "runs" ? (
        <>
          <div className="side-section">
            <h2 className="side-title">{t("sidebar.recentTitle")}</h2>
            <div className="recent-list">
              {runs.slice(0, 5).map((run) => (
                <button
                  key={run.run_id}
                  className="recent"
                  type="button"
                  title={run.run_id}
                  onClick={() => onOpenRun(run.run_id)}
                >
                  <span>{runTaskLabel(run.task, run.task_label, t)}</span>
                  <span className="recent-score">{run.score ?? "—"}</span>
                </button>
              ))}
            </div>
          </div>


        </>
      ) : null}

      <footer className="side-footer">
        <span className="avatar" aria-hidden="true">F</span>
        <span className="side-who">
          <strong>{t(review ? "sidebar.reviewWorkspace" : "sidebar.localWorkspace")}</strong>
        </span>
      </footer>
    </aside>
  );
}
