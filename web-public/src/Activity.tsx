import { useEffect, useState } from "react";
import { JsonTree, SearchBlock } from "@forgeflow/dsh-primitives";
import { useI18n, type Lang } from "./i18n";
import type { ActivityState, ActivityStep, ActivityStepStatus } from "./types";
import { DshReasoningRow } from "./dsh/ReasoningRow";

export type { ActivityState, ActivityStep, ActivityStepStatus } from "./types";

const COPY = {
  en: {
    working: "Running",
    done: "Done",
    waiting: "Waiting",
    failed: "Failed",
    workedFor: (seconds: string) => `Done · ${seconds}`,
    workingFor: (seconds: string) => `Running · ${seconds}`,
    waitingFor: (seconds: string) => `Waiting · ${seconds}`,
    activity: "Activity",
    reasoning: "Activity",
    toolCalls: (n: number) => `${n} tool calls`,
    hide: "Hide",
    show: "Show",
    close: "Dismiss",
    kinds: {
      think: "Think",
      odoo: "Odoo",
      search: "Search",
      solver: "Solver",
      policy: "Policy",
      approval: "Approval",
      executor: "Executor",
      readback: "Readback",
      verify: "Verifier",
      answer: "Answer",
      read: "Read",
      glob: "Search",
      bash: "Shell",
      write: "Write",
    },
  },
} as const;

function duration(ms: number): string {
  const safe = Math.max(0, ms);
  if (safe < 10_000) return `${(safe / 1000).toFixed(1)}s`;
  const total = Math.floor(safe / 1000);
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

function statusGlyph(status: ActivityStepStatus): string {
  if (status === "done") return "✓";
  if (status === "failed") return "!";
  if (status === "active") return "●";
  return "○";
}

function kindLabel(kind: string | undefined, kinds: Record<string, string>): string {
  if (!kind) return "Tool";
  if (kind.startsWith("mcp__")) return "MCP";
  return kinds[kind] ?? kind.charAt(0).toUpperCase() + kind.slice(1);
}

/** A direct, observable trace of harness thinking and tool calls. */

function EvidenceBlock({ step }: { step: ActivityStep }) {
  const { t } = useI18n();
  const searchLabels = { pathsSummary: (shown: number, total: number) => `${shown}/${total}`, matchesSummary: (shown: number, total: number) => `${shown}/${total}`, copy: t("v9.copy"), copied: t("v9.copied"), noResults: t("v9.unknown"), collapseAria: t("sidebar.collapse"), expandAria: (hidden: number) => `${t("sidebar.expand")} (${hidden})`, collapse: t("sidebar.collapse"), expand: (hidden: number) => `${t("sidebar.expand")} (${hidden})` };
  if (step.evidence && step.evidence.length > 0) {
    return (
      <SearchBlock
        kind="matches"
        truncated={false}
        total={step.evidence.length}
        files={step.evidence.map((row, index) => ({
          path: row.kind ? `${row.kind} · ${row.id ?? ""}` : row.id || `evidence ${index + 1}`,
          matches: [{ lineNumber: 1, line: row.excerpt || "" }],
        }))}
        labels={searchLabels}
      />
    );
  }
  if (step.payload && typeof step.payload === "object") {
    return (
      <details className="activity-json">
        <summary>{t("v9.evidenceJson")}</summary>
        <JsonTree
          data={step.payload as object | unknown[]}
          label={t("v9.evidenceJson")}
          collapsedStringLines={3}
          copyable
          labels={{
            copyValue: t("v9.copy"), copyJson: t("v9.copy"), copyPath: t("v9.copy"),
            copyPrettyJson: t("v9.copy"), copyCompactJson: t("v9.copy"),
            copied: t("v9.copied"), copyFailed: t("v9.copyFailed"), collapseNode: t("sidebar.collapse"),
            expandNode: t("sidebar.expand"), copyButtonTitle: (action) => action,
          }}
        />
      </details>
    );
  }
  return null;
}

export function ActivityPanel({
  state,
  lang,
  onDismiss,
  className = "",
  defaultOpen = false,
}: {
  state: ActivityState;
  lang?: Lang;
  onDismiss?: () => void;
  className?: string;
  defaultOpen?: boolean;
}) {
  const copy = COPY[lang ?? "en"];
  const [now, setNow] = useState(() => Date.now());
  const [open, setOpen] = useState(state.active || defaultOpen);

  useEffect(() => {
    setOpen((current) => (state.active ? true : current));
  }, [state.active]);

  useEffect(() => {
    if (!state.active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [state.active]);

  const elapsed = (state.finishedAt ?? now) - state.startedAt;
  const seconds = duration(elapsed);
  const failed =
    state.outcome === "failed" ||
    (state.outcome === undefined && state.steps.some((step) => step.status === "failed"));
  const completed = state.steps.every(
    (step) => step.status === "done" || step.status === "failed",
  );
  const headerStatus = state.active
    ? copy.working
    : failed
      ? copy.failed
      : completed
        ? copy.done
        : copy.waiting;
  const footerText = state.hideDuration ? headerStatus : state.durationLabel ? `${state.durationLabel} · ${seconds}` : state.active
    ? copy.workingFor(seconds)
    : completed
      ? copy.workedFor(seconds)
      : copy.waitingFor(seconds);

  return (
    <section
      className={`activity activity-log ${state.active ? "is-active" : "is-done"} ${
        failed ? "is-failed" : ""
      } ${className}`.trim()}
      aria-label={copy.activity}
      aria-live="polite"
      data-activity="true"
      data-activity-active={state.active ? "true" : "false"}
    >
      <div className="activity-log-head">
        <span className="activity-log-pulse" aria-hidden="true" />
        <span className="activity-log-status">
          {state.title ?? copy.activity}
        </span>
        <span className="activity-dot">·</span>
        <span className="activity-verb">{headerStatus}</span>
        <span className="activity-controls">
          <button
            type="button"
            className="activity-link"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
          >
            {open ? copy.hide : copy.show}
          </button>
          {onDismiss ? (
            <button
              type="button"
              className="activity-close"
              onClick={onDismiss}
              aria-label={copy.close}
            >
              ×
            </button>
          ) : null}
        </span>
      </div>
      {open ? (
        <ol className="activity-log-lines">
          {state.steps.filter(step => step.kind !== "think" && step.kind !== "reasoning").map((step) => (
            <li className={`activity-log-line is-${step.status}`} key={step.id}>
              <span className="activity-log-icon" aria-hidden="true">
                {statusGlyph(step.status)}
              </span>
              <span className="activity-kind">
                {kindLabel(step.kind, copy.kinds)}
              </span>
              <span className="activity-sep">·</span>
              <span className="activity-line-body">
                <span className="activity-line-label">{step.label}</span>
                {step.detail ? (
                  <span className="activity-line-detail" style={{ overflowWrap: "anywhere" }}>
                    {step.detail}
                  </span>
                ) : null}
              </span>
              <EvidenceBlock step={step} />
            </li>
          ))}
        </ol>
      ) : null}
      <div className="activity-log-foot">
        <span className="activity-compass" aria-hidden="true">↳</span>
        <span className="activity-timer" data-activity-timer="true">
          {footerText}
        </span>
      </div>
    </section>
  );
}



/** ForgeFlow data adapter around the vendored DSH ReasoningRow. */
export function ReasoningRow({
  state,
  lang,
}: {
  state?: ActivityState | null;
  lang?: Lang;
}) {
  const copy = COPY[lang ?? "en"];
  if (!state || state.steps.length === 0) return null;
  const text = state.steps
    .filter((step) => step.kind === "think" && step.label.trim() !== "")
    .map((step) => step.label)
    .join("\n\n");
  return (
    <DshReasoningRow
      text={text}
      running={state.active}
      title={copy.reasoning}
      body={(
        <ol className="dsh-reasoning-steps">
          {state.steps.filter(step => step.kind !== "think" && step.kind !== "reasoning").map((step) => (
            <li className={`dsh-reasoning-step is-${step.status}`} key={step.id}>
              <span className="dsh-reasoning-step-icon" aria-hidden="true">
                {statusGlyph(step.status)}
              </span>
              <span className="dsh-reasoning-kind">{kindLabel(step.kind, copy.kinds)}</span>
              <span>
                <span>{step.label}</span>
                {step.detail ? (
                  <small style={{ overflowWrap: "anywhere" }}>{step.detail}</small>
                ) : null}
              </span>
              <EvidenceBlock step={step} />
            </li>
          ))}
        </ol>
      )}
    />
  );
}
