import { originalScoreThresholdMiss } from "./verificationExplanation";
import { RecordedRules, productRuleEvaluation } from "./RecordedRules";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Modal } from "@forgeflow/dsh-primitives";
import {
  approveProductCase,
  getProductCase,
  prepareProductCase,
  reprepareProductCase,
  rejectProductCase,
  reviseProductCase,
  reconcileProductCase,
  cancelUnstartedProductCase,
  selectProductCandidate,
} from "./api";
import { ActivityPanel } from "./Activity";
import { PreparationStatus } from "./PreparationStatus";
import { CostBreakdown } from "./CostBreakdown";
import { DecisionFacts, ChoiceReason } from "./DecisionFacts";
import { OrderReadiness } from "./OrderReadiness";
import { ImpactChain } from "./ImpactChain";
import { downloadReceipt } from "./receipt";
import { environmentAccessNotice } from "./environmentAccess";
import { CandidateComparison, candidateName } from "./CandidateComparison";
import { canContinuePreparation, preparationFailure, preparationMessage, revisionFloor, suggestedRevisionMargin, validRevisionMargin } from "./casePreparation";
import {
  actionKindClass,
  actionCountsLabel,
  caseOutcomeLabel,
  isCaseVerified,
  isExecutionUnknown,
  actionKindLabel,
  actionSummary,
  buildCaseActivity,
  caseSteps,
  checkCounts,
  checkDisplayName,
  caseConclusion,
  verifierKindKey,
  approverLabel,
  missingRequiredChecks,
  approvalGate,
  budgetAggregateNote,
  policyCheckValue,
  effectiveChecks,
  errorDetail,
  dueWindow,
  exceptionText,
  formatAge,
  formatCheckValue,
  formatCurrency,
  formatDateTime,
  formatPercent,
  formatQuantity,
  friendlyDocument,
  marginFloor,
  orderCount,
  planNewSpend,
  severityLabel,
  severityOf,
  shortId,
  stateLabel,
  stateTone,
  stepStatusIcon,
  timelineEvents,
  uniqueActionDocuments,
  verifierSummary,
  verifierSummaryText,
  type CaseMode,
  type Step,
  type TimelineEvent,
} from "./caseUi";
import { useI18n, type Translate, type MessageKey } from "./i18n";
import type {
  ActivityState,
  CaseFile,
  Capabilities,
  ProductAction,
  ProductPlan,
} from "./types";

import { useWorkspace } from "./workspace";

type Props = {
  caseFile: CaseFile;
  mode: CaseMode;
  capabilities: Capabilities | null;
  caseReadBlocked?: boolean;
  evidenceOpen?: boolean;
  nextCaseId: string | null;
  onBack: () => void;
  onAsk: () => void;
  onUpdated: (next: CaseFile) => void;
  onOperationPendingChange?: (pending: boolean) => void;
  onNext: () => void;
  onOpenEvidence: () => void;
  /** When this page last re-read the case. Page metadata, not a case fact. */
  lastReadAt?: string | null;
};

function disruptionLabel(kind: string, t: Translate): string {
  if (kind === "workcenter_outage") return t("case.disruptionWorkcenter");
  if (kind.includes("supplier")) return t("case.disruptionSupplier");
  return kind;
}

function missingPlanFacts(plan: ProductPlan, caseFile: CaseFile): string[] {
  const missing: string[] = [];
  if (!plan.costs) missing.push("costs");
  if (!plan.order_readiness) missing.push("readiness");
  if (!plan.impact_chain) missing.push("impact");
  if (!plan.decision_metrics) missing.push("constraints");
  if (!caseFile.approval?.comment?.trim()) missing.push("reason");
  return missing;
}

function DecisionSummary({ caseFile, onEvidence }: { caseFile: CaseFile; onEvidence: () => void }) {
  const { t } = useI18n();
  const orders = orderCount(caseFile.order.ref);
  const state = caseFile.state;
  // One plain sentence a reader can repeat: what succeeded and on what evidence.
  const conclusion = caseConclusion(caseFile, t);
  const action = state === "awaiting_approval"
    ? t("v11.decisionAwaiting")
    : state === "executing"
      ? t("v11.decisionExecuting")
      : state === "succeeded"
        ? t("v11.decisionSucceeded")
        : state === "failed"
          ? t("v11.decisionFailed")
          : state === "rejected"
            ? t("v11.decisionRejected")
            : state === "expired"
              ? t("v11.decisionExpired")
              : t("v11.decisionPreparing");
  return (
    <section className="case-decision-summary" data-case-decision-summary="true">
      <p className="case-decision-kicker">{t("v11.decisionKicker")}</p>
      <h2>{action}</h2>
      <p className="case-decision-body">{t("v11.decisionBody", {
        exception: exceptionText(caseFile, t),
        orders: orders ?? "?",
        units: formatQuantity(caseFile.order.units),
      })}</p>
      {conclusion ? <p className="case-conclusion" data-case-conclusion="true">{conclusion}</p> : null}
      <div className="case-decision-facts">
        <span><strong>{orders ?? "?"}</strong> {t("v11.decisionOrders")}</span>
        <span><strong>{formatQuantity(caseFile.order.units)}</strong> {t("v11.decisionUnits")}</span>
      </div>
      {state === "awaiting_approval" ? (
        <div className="case-decision-actions">
          <button type="button" className="secondary case-decision-action" data-review-plan="true"
            onClick={() => document.querySelector("[data-decision-panel]")?.scrollIntoView({ behavior: "smooth", block: "start" })}>
            {t("v11.reviewPlan")}
          </button>
          <button type="button" className="secondary" data-case-evidence-link="true" onClick={onEvidence}>
            {t("v11.viewEvidence")}
          </button>
        </div>
      ) : (
        <div className="case-decision-actions">
          <button type="button" className="secondary" data-case-evidence-link="true" onClick={onEvidence}>
            {t(state === "failed" ? "v11.viewFailure" : state === "executing" ? "v11.viewProgress" : "v11.viewReceipt")}
          </button>
        </div>
      )}
    </section>
  );
}

function StatusStepper({ caseFile }: { caseFile: CaseFile }) {
  const { t } = useI18n();
  const steps = caseSteps(caseFile);
  return (
    <ol className="status-stepper" data-case-stepper="true" aria-label={t("case.step.detected")}>
      {steps.map((step: Step, index: number) => (
        <li
          key={`${step.key}-${index}`}
          className={`status-step is-${step.status}`}
          data-step={step.key}
          data-step-status={step.status}
        >
          <span className="status-step-dot" aria-hidden="true">
            {stepStatusIcon(step.status)}
          </span>
          <span className="visually-hidden">{t("case.stepOf", { index: index + 1, total: steps.length })}</span>
          <span className="status-step-label">{t(step.labelKey)}</span>
          {index < steps.length - 1 ? (
            <span className="status-step-line" aria-hidden="true" />
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function Timeline({ caseFile }: { caseFile: CaseFile }) {
  const { t, lang } = useI18n();
  const events: TimelineEvent[] = timelineEvents(caseFile, t, lang);
  return (
    <ol className="case-timeline" data-case-timeline="true" aria-label={t("case.timelineAria")}>
      {events.map((event: TimelineEvent) => (
        <li
          key={event.id}
          className={`case-timeline-event is-${event.status}`}
          data-timeline-event={event.id}
          data-event-status={event.status}
        >
          <span className="case-timeline-dot" aria-hidden="true">
            {stepStatusIcon(event.status)}
          </span>
          <div className="case-timeline-body">
            <strong>{event.label}</strong>
            <p>{event.detail}</p>
          </div>
          <time className="case-timeline-time">{event.time}</time>
        </li>
      ))}
    </ol>
  );
}

function CaseFacts({ caseFile, mode }: { caseFile: CaseFile; mode: CaseMode }) {
  const { t, lang } = useI18n();
  const orders = orderCount(caseFile.order.ref);
  return (
    <section className="case-section" data-case-facts="true">
      <div className="case-section-head">
        <h2>{t("case.factsTitle")}</h2>
        <span className="case-data-type">
          {mode === "fixture"
            ? t("case.dataSynthetic")
            : caseFile.synthetic
              ? t("case.dataSyntheticLive")
              : t("case.dataLive")}
        </span>
      </div>
      <dl className="case-facts-grid">
        <div>
          <dt>{t("case.factOrder")}</dt>
          <dd className="mono">{caseFile.order.ref}</dd>
        </div>
        <div>
          <dt>{t("case.factProduct")}</dt>
          <dd>{caseFile.order.product}</dd>
        </div>
        <div>
          <dt>{t("case.factUnits")}</dt>
          <dd>
            {formatQuantity(caseFile.order.units)} {t("cases.units")}
            {orders ? ` · ${t("cases.ordersCount", { count: orders })}` : ""}
          </dd>
        </div>
        <div>
          <dt>{t("case.factDue")}</dt>
          <dd>{dueWindow(caseFile, t)}</dd>
        </div>
        <div>
          <dt>{t(orders && caseFile.order.customer === `${orders} confirmed sales orders` ? "v10.orderScope" : "case.factCustomer")}</dt>
          <dd>{orders && caseFile.order.customer === `${orders} confirmed sales orders` ? t("v10.confirmedOrders", { count: orders }) : caseFile.order.customer}</dd>
        </div>
        <div>
          <dt>{t(caseFile.disruption.kind === "workcenter_outage" ? "case.factWorkcenter" : "case.factVendor")}</dt>
          <dd>{caseFile.disruption.workcenter || caseFile.disruption.vendor || t("v9.unknown")}</dd>
        </div>
        <div>
          <dt>{t("case.factDisruption")}</dt>
          <dd>{disruptionLabel(caseFile.disruption.kind, t)}</dd>
        </div>
        <div>
          <dt>{t("case.factCreated")}</dt>
          <dd>{formatDateTime(caseFile.created_at, lang)}</dd>
        </div>
        <div>
          <dt>{t("case.factUpdated")}</dt>
          <dd>{formatDateTime(caseFile.updated_at, lang)}</dd>
        </div>
      </dl>
    </section>
  );
}

function ActionTable({
  plan,
  caseFile,
}: {
  plan: ProductPlan;
  caseFile: CaseFile;
}) {
  const { t } = useI18n();
  return (
    <div className="change-table-wrap">
      <table className="change-table" data-change-table="true">
        <thead>
          <tr>
            <th>{t("case.colDocument")}</th>
            <th>{t("case.colAction")}</th>
            <th>{t("case.colQty")}</th>
            <th>{t("case.colWhy")}</th>
          </tr>
        </thead>
        <tbody>
          {plan.actions.map((action: ProductAction, index: number) => (
            <tr key={`${action.kind}-${action.document}-${index}`}>
              <td className="mono" data-action-document={action.document}>
                {friendlyDocument(action.document, t, plan.actions)}
              </td>
              <td>
                <span className={`case-action-kind ${actionKindClass(action.kind)}`}>
                  {actionKindLabel(action.kind, t)}
                </span>
              </td>
              <td>{formatQuantity(action.quantity)}</td>
              <td className="change-summary">
                {actionSummary(action, caseFile, t)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const DIFF_FIELDS: Record<string, MessageKey> = {
  quantity: "case.colQty", unit_price: "case.diffUnitPrice", arrival_day: "case.diffArrivalDay", date_planned: "case.diffPlannedDate",
  start_day: "case.diffStartDay", due_day: "case.diffDueDay", date_start: "case.diffStartDate", date_deadline: "case.diffDeadline",
  product_code: "case.diffProduct", vendor_ref: "case.factVendor", workcenter_code: "case.factWorkcenter", supply_role: "case.diffSupplyRole", state: "cases.colState",
  duration_expected_minutes: "case.diffDuration", date_finished: "case.diffFinishDate", time_per_unit_minutes: "case.diffUnitTime", unit_cost: "case.diffUnitCost", lead_days: "case.diffLeadDays",
};
function SnapshotFields({ value, actions }: { value: ProductAction["before"]; actions: ProductAction[] }) {
  const { t } = useI18n();
  if (!value || !Object.keys(value).length) return <p>{t("runs.notRecorded")}</p>;
  const scalar = Object.entries(value).filter(([key, item]) => key in DIFF_FIELDS && (item === null || typeof item !== "object"));
  const fields = (entries: [string, unknown][]) => <dl className="change-diff-fields">{entries.map(([key, item]) => <div key={key}><dt>{t(DIFF_FIELDS[key])}</dt><dd>{formatCheckValue(item as string | number | boolean | null, t)}</dd></div>)}</dl>;
  return <>
    {fields(scalar)}
    {Array.isArray(value.origin_refs) && <details className="change-diff-nested"><summary>{t("case.diffOrigins", { count: value.origin_refs.length })}</summary><ul>{value.origin_refs.map((item, index) => {
      const ref = item && typeof item === "object" ? item as Record<string, unknown> : {};
      const name = typeof ref.name === "string" && ref.name ? friendlyDocument(ref.name, t, actions) : null;
      const token = typeof ref.token === "string" ? ref.token : "";
      return <li key={index}>{name ?? (token.startsWith("new:") ? t("case.diffUnassigned", { document: friendlyDocument(token, t, actions) }) : t("case.diffNameMissing"))}</li>;
    })}</ul></details>}
    {(["workorder_plan", "public_standard"] as const).map(key => {
      const nested = value[key];
      return nested && typeof nested === "object" && !Array.isArray(nested) ? <details className="change-diff-nested" key={key}><summary>{t(key === "workorder_plan" ? "case.diffWorkorder" : "case.diffStandard")}</summary>{fields(Object.entries(nested).filter(([field]) => field in DIFF_FIELDS))}</details> : null;
    })}
  </>;
}

function ActionDiff({ plan }: { plan: ProductPlan }) {
  const { t } = useI18n();
  return (
    <div className="change-diff" data-change-diff="true">
      {plan.actions.map((action: ProductAction, index: number) => {
        return (
          <div
            className="change-diff-row"
            key={`${action.kind}-${action.document}-${index}`}
          >
            <div className="change-diff-head">
              <span className="mono">
                {friendlyDocument(action.document, t, plan.actions)}
              </span>
              <span className={`case-action-kind ${actionKindClass(action.kind)}`}>
                {actionKindLabel(action.kind, t)}
              </span>
              <span className="change-diff-qty">
                {formatQuantity(action.quantity)} {t("cases.units")}
              </span>
            </div>
            <div className="change-diff-cols">
              <div className="change-diff-col is-before">
                <span className="change-diff-label">{t("case.before")}</span>
                <SnapshotFields value={action.before} actions={plan.actions} />
              </div>
              <div className="change-diff-col is-after">
                <span className="change-diff-label">{t("case.after")}</span>
                <SnapshotFields value={action.after} actions={plan.actions} />
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function VerificationSection({ caseFile }: { caseFile: CaseFile }) {
  const { t } = useI18n();
  const [recordView, setRecordView] = useState<"writes" | "readback">("readback");
  const execution = caseFile.execution;
  if (!execution || execution.status === "not_started") return null;
  const summary = verifierSummary(execution, t, caseFile.result);
  const records = execution.readback?.records ?? [];
  const writes = execution.writes;
  const showingReadback = recordView === "readback";
  return (
    <section className="case-section" data-case-verification="true">
      <div className="case-section-head">
        <h2>{t("case.verificationTitle")}</h2>
        <span
          className={`case-verifier is-${summary.tone}`}
          data-verifier-status={summary.status}
        >
          {summary.label}
        </span>
      </div>
      <div className="verification-grid">
        <div className="verification-block">
          <h3>{t(execution.verifier?.source === "synthetic_executor" ? "v9.simulatedCheck" : "case.verifierTitle")}</h3>
          {/* The rule counts live once, in the recorded-rules block below; this
              line states the status only. */}
          <p className="verification-primary" data-verification-verifier="true" data-verification-counts="moved">
            {summary.label}
          </p>
          {originalScoreThresholdMiss(execution) && <p className="decision-note" data-score-threshold-explanation="true">{t("case.scoreThresholdFailure")}</p>}
          {execution.verifier?.checks?.length ? (
            <ul className="case-checks">
              {execution.verifier.checks.map((check, index) => {
                // Some verifiers report the aggregate score in the document slot;
                // it is not a document, and the score is already stated once.
                const raw = (check.document ?? "").trim();
                const scoreLike = /^\d+(\.\d+)?\/\d+$/.test(raw);
                return (
                  <li key={`${raw || "check"}-${index}`}>
                    <span
                      className={check.passed === false ? "is-failed" : check.passed === true ? "is-success" : "is-unknown"}
                      aria-hidden="true"
                    >
                      {check.passed === false ? "✗" : check.passed === true ? "✓" : "—"}
                    </span>
                    <span className={scoreLike ? undefined : "mono"}>
                      {scoreLike ? t("v13.verifierAggregate") : raw || "—"}
                    </span>
                    <span>{t(verifierKindKey(check.kind))}</span>

                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="muted">{t("decision.receiptNoVerifier")}</p>
          )}
        </div>
        <div className="verification-block" data-record-view={recordView}>
          <div className="verification-block-head">
            <h3>{t("case.recordsTitle")}</h3>
            <div className="segmented" role="group" aria-label={t("case.recordsTitle")}>
              <button type="button" className={showingReadback ? "" : "is-active"} aria-pressed={!showingReadback}
                onClick={() => setRecordView("writes")} data-record-view-button="writes">
                {t("v13.recordViewWrites", { count: writes.length })}
              </button>
              <button type="button" className={showingReadback ? "is-active" : ""} aria-pressed={showingReadback}
                onClick={() => setRecordView("readback")} data-record-view-button="readback">
                {t("v13.recordViewReadback", { count: records.length })}
              </button>
            </div>
          </div>
          <p className="verification-primary" data-records-summary="true">
            {showingReadback
              ? execution.readback
                ? t(Array.isArray(execution.readback.records) ? "decision.receiptReadbackValue" : "decision.receiptReadbackUnknown", {
                  count: records.length,
                  steps: execution.readback.verified_steps ?? "—",
                })
                : t("runs.notRecorded")
              : isExecutionUnknown(caseFile)
                ? t("v9.writeScopeUnknown")
                : t("case.writesCount", { count: writes.length })}
          </p>
          {showingReadback ? (
            records.length > 0 ? (
              <ul className="case-readback" data-record-list="readback">
                {records.map((record, index) => (
                  <li key={`${record.document ?? "record"}-${index}`}>
                    <span className="mono">{friendlyDocument(record.document, t, caseFile.plan?.actions)}</span>
                    <span>{actionKindLabel(record.kind, t)}</span>
                    <span className="case-readback-state">{record.state ?? "—"}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">{t(Array.isArray(execution.readback?.records) ? "case.readbackEmpty" : "decision.receiptReadbackMissing")}</p>
            )
          ) : writes.length > 0 ? (
            <ul className="case-readback" data-record-list="writes">
              {writes.map((write, index) => (
                <li key={`${write.document ?? "write"}-${index}`}>
                  <span className="mono">{friendlyDocument(write.document, t, caseFile.plan?.actions)}</span>
                  <span>{actionKindLabel(write.kind, t)}</span>
                  <span>{formatQuantity(write.quantity)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">{t("case.writesNone")}</p>
          )}
        </div>
      </div>
      {execution.verifier?.source !== "synthetic_executor" && <RecordedRules evaluation={productRuleEvaluation(execution.verifier)} />}
    </section>
  );
}

function PolicyChecks({
  checks,
  derived,
  unknownChecks = [],
}: {
  checks: ReturnType<typeof effectiveChecks>["checks"];
  derived: boolean;
  unknownChecks?: string[];
}) {
  const { t } = useI18n();
  const missing = missingRequiredChecks(unknownChecks, checks);
  if (checks.length === 0 && missing.length === 0) return null;
  const counts = checkCounts(checks);
  const budgetNote = budgetAggregateNote(checks, t);
  return (
    <section className="decision-block" data-policy-checks="true">
      <div className="decision-block-head">
        <h3>{t("decision.checksTitle")}</h3>
        {derived ? (
          <span className="decision-badge is-planned">
            {t("decision.checkStatusPlanned")}
          </span>
        ) : (
          <span className="decision-counts" data-check-counts={missing.length ? "with-missing" : "plain"}>
            {missing.length
              ? t("v13.checkCountsWithMissing", {
                pass: counts.pass,
                warn: counts.warn,
                block: counts.block,
                notRecorded: missing.length,
              })
              : t("decision.checkCounts", {
                pass: counts.pass,
                warn: counts.warn,
                block: counts.block,
              })}
          </span>
        )}
      </div>
      {derived ? (
        <p className="decision-note">{t("decision.checkNone")}</p>
      ) : null}
      {missing.length > 0 ? (
        <>
          <p className="decision-note is-attention" data-check-missing-note="true">
            {t(missing.length === 1 ? "v13.checkMissingNoteOne" : "v13.checkMissingNote", {
              count: missing.length,
              names: missing.map((code) => checkDisplayName(code, t)).join(t("v13.checkNameSeparator")),
            })}
          </p>
          <p className="decision-note" data-legacy-plan="true">{t("v13.legacyPlanNotice")}</p>
        </>
      ) : null}
      {budgetNote && <p className="decision-note" data-budget-check-note="true">{budgetNote}</p>}
      <ul className="policy-check-list">
        {checks.map((check) => {
          const rawStatus = check.status;
          const status =
            rawStatus === "pass" ||
            rawStatus === "warn" ||
            rawStatus === "block" ||
            rawStatus === "planned"
              ? rawStatus
              : "unknown";
          const statusLabel = derived
            ? t("decision.checkStatusPlanned")
            : status === "planned"
              ? t("decision.checkStatusPlanned")
              : status === "unknown"
                ? t("decision.checkStatusUnknown")
                : status === "pass"
                  ? t("decision.checkStatusPass")
                  : status === "warn"
                    ? t("decision.checkStatusWarn")
                    : t("decision.checkStatusBlock");
          return (
            <li
              key={check.code}
              className={`policy-check is-${status}`}
              data-check-code={check.code}
              data-check-status={status}
              data-budget-basis={check.budgetBasis}
            >
              <span className="policy-check-status">{statusLabel}</span>
              <span className="policy-check-label">
                {check.label}
                {check.unknownCode ? (
                  <span className="policy-check-code" data-check-untranslated={check.unknownCode} title={t("v13.unknownCheckTitle")}>
                    ({check.unknownCode})
                  </span>
                ) : null}
              </span>
              <span
                className="policy-check-value"
                title={check.technical ? t("v13.policyHashTitle", { hash: check.technical }) : undefined}
                data-check-technical={check.technical}
              >
                {policyCheckValue(check, t)}
              </span>
              <span className="policy-check-source">
                {check.source || t("case.checkSourcePlan")}
              </span>
            </li>
          );
        })}
        {missing.map((code) => (
          <li
            key={`missing-${code}`}
            className="policy-check is-not-recorded"
            data-check-code={code}
            data-check-status="not_recorded"
            data-check-missing="true"
          >
            <span className="policy-check-status">{t("v13.checkStatusNotRecorded")}</span>
            <span className="policy-check-label">{checkDisplayName(code, t)}</span>
            <span className="policy-check-value">{t("v13.checkMissingValue")}</span>
            <span className="policy-check-source">{t("v13.checkMissingSource")}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function EvidencePanel({
  caseFile,
  mode,
  evidenceRef,
}: {
  caseFile: CaseFile;
  mode: CaseMode;
  evidenceRef: RefObject<HTMLDivElement | null>;
}) {
  const { t, lang } = useI18n();
  const plan = caseFile.plan;
  const dataType =
    mode === "fixture"
      ? t("case.evidenceSynthetic")
      : caseFile.synthetic
        ? t("case.dataSyntheticLive")
        : t("case.evidenceLive");
  return (
    <section
      className="case-evidence"
      id="case-evidence"
      tabIndex={-1}
      data-case-evidence="true"
      ref={evidenceRef}
    >
      <div className="decision-block-head">
        <h3>{t("inbox.evidenceTitle")}</h3>
        <span className="case-data-type">{dataType}</span>
      </div>
      <dl className="case-evidence-facts">
        <dt>{t("case.evidenceCaseId")}</dt>
        <dd className="mono">{caseFile.case_id}</dd>
        <dt>{t("case.evidenceDataType")}</dt>
        <dd>{dataType}</dd>
        <dt>{t("case.evidencePlanId")}</dt>
        <dd className="mono" title={plan?.plan_id}>
          {plan ? shortId(plan.plan_id, 24) : "—"}
        </dd>
        <dt>{t("case.evidencePolicy")}</dt>
        <dd className="mono" title={plan?.policy_sha256}>
          {plan ? shortId(plan.policy_sha256, 20) : "—"}
        </dd>
        <dt>{t("case.evidenceSnapshot")}</dt>
        <dd className="mono" title={plan?.snapshot_sha256}>
          {plan ? shortId(plan.snapshot_sha256, 20) : "—"}
        </dd>
        <dt>{t("case.evidencePrepared")}</dt>
        <dd>{formatDateTime(plan?.prepared_at, lang)}</dd>
        <dt>{t("case.evidenceApproval")}</dt>
        <dd className="mono">
          {caseFile.approval ? formatDateTime(caseFile.approval.issued_at, lang) : t("case.evidenceNone")}
        </dd>
        <dt>{t("case.evidenceExpires")}</dt>
        <dd>
          {caseFile.approval?.expires_at
            ? formatDateTime(caseFile.approval.expires_at, lang)
            : t("case.evidenceNone")}
        </dd>
        <dt>{t("case.evidenceCreated")}</dt>
        <dd>{formatDateTime(caseFile.created_at, lang)}</dd>
        <dt>{t("case.evidenceUpdated")}</dt>
        <dd>{formatDateTime(caseFile.updated_at, lang)}</dd>
        {caseFile.reason ? (
          <>
            <dt>{t("case.evidenceReason")}</dt>
            <dd className="mono">{caseFile.reason}</dd>
          </>
        ) : null}
      </dl>
      <p className="case-evidence-note">
        {mode === "fixture"
          ? t("inbox.demoNote")
          : t("case.evidenceLiveNote")}
      </p>
    </section>
  );
}

function ExecutionReceipt({
  caseFile,
  nextCaseId,
  onNext,
  onViewAudit,
}: {
  caseFile: CaseFile;
  nextCaseId: string | null;
  onNext: () => void;
  onViewAudit: () => void;
}) {
  const { t, lang } = useI18n();
  const execution = caseFile.execution;
  const plan = caseFile.plan;
  if (!execution) return null;
  const summary = verifierSummary(execution, t, caseFile.result);
  const success = isCaseVerified(caseFile);
  const simulationPassed = caseFile.result?.simulation_passed === true;
  const records = execution.readback?.records ?? [];
  if (execution.status === "not_started") return (
    <section className="execution-receipt" data-execution-receipt="true" data-receipt-state="not-started">
      <h3>{t("v10.unstartedClosed")}</h3>
      <p>{t("v10.unstartedReceipt")}</p>
      <p className="muted">{formatDateTime(caseFile.updated_at, lang)}</p>
      <button type="button" className="secondary" onClick={onViewAudit}>{t("v9.audit")}</button>
    </section>
  );
  return (
    <section
      className={`execution-receipt ${success ? "is-success" : simulationPassed ? "is-simulated" : caseFile.result?.strict_success === false ? "is-failed" : "is-unknown"}`}
      data-execution-receipt="true"
      data-receipt-state={success ? "success" : simulationPassed ? "simulated" : caseFile.result?.strict_success === false ? "failed" : "unknown"}
    >
      <div className="receipt-head">
        <span className="receipt-mark" aria-hidden="true">
          {success || simulationPassed ? "✓" : "!"}
        </span>
        <div>
          <h3>
            {caseOutcomeLabel(caseFile, t)}
          </h3>
          <p className="receipt-sub">
            {stateLabel(execution.status, t)} · {formatDateTime(caseFile.updated_at, lang)}
          </p>
        </div>
        <span className={`case-verifier is-${summary.tone}`}>
          {summary.label}
        </span>
      </div>

      <dl className="receipt-facts">
        <dt>{t(execution.verifier?.source === "synthetic_executor" ? "v9.simulatedCheck" : "decision.receiptVerifier")}</dt>
        <dd data-receipt-verifier="true">{summary.label}</dd>
        <dt>{t("decision.receiptWrites")}</dt>
        <dd data-receipt-writes="true">
          {isExecutionUnknown(caseFile) ? t("v9.writeScopeUnknown") : t("decision.receiptWritesValue", { count: execution.writes.length })}
        </dd>
        <dt>{t(execution.verifier?.source === "synthetic_executor" ? "v9.simulatedReadback" : "decision.receiptReadback")}</dt>
        <dd data-receipt-readback="true">
          {execution.readback ? t(Array.isArray(execution.readback.records) ? "decision.receiptReadbackValue" : "decision.receiptReadbackUnknown", {
            count: records.length,
            steps: execution.readback.verified_steps ?? "—",
          }) : t("runs.notRecorded") }
        </dd>
        <dt>{t("decision.receiptApprover")}</dt>
        <dd data-receipt-approver="true">{approverLabel(caseFile.approval?.approver_label, t)}</dd>
        <dt>{t("decision.receiptTime")}</dt>
        <dd>{formatDateTime(caseFile.approval?.issued_at, lang)}</dd>
      </dl>
      <details className="receipt-technical" data-receipt-technical="true">
        <summary>{t("v13.technicalDetails")}</summary>
        <dl className="receipt-facts">
          <dt>{t("decision.receiptPlan")}</dt>
          <dd className="mono" title={plan?.plan_id} data-receipt-plan="true">{shortId(plan?.plan_id, 16)}</dd>
          <dt>{t("decision.receiptVerifier")}</dt>
          <dd>{verifierSummaryText(execution, t, caseFile.result)}</dd>
          <dt>{t("decision.receiptApprover")}</dt>
          <dd className="mono">{caseFile.approval?.approver_label || t("runs.notRecorded")}</dd>
          <dt>{t("decision.comment")}</dt>
          <dd data-receipt-comment="true">{caseFile.approval?.comment || "—"}</dd>
        </dl>
      </details>

      {originalScoreThresholdMiss(execution) && <p className="decision-note" data-receipt-score-explanation="true">{t("case.scoreThresholdFailure")}</p>}
      {summary.score === null ? (
        <p className="decision-note">{t("decision.receiptNoVerifier")}</p>
      ) : null}

      <div className="receipt-actions">
        <button className="primary" type="button" onClick={onViewAudit} data-view-audit="true">
          {t("case.viewAudit")}
        </button>
        <button className="secondary" type="button" onClick={onNext} data-next-case="true">
          {nextCaseId ? t("case.nextCase") : t("case.backToCases")}
        </button>
      </div>
    </section>
  );
}

function DecisionPanel({
  caseFile,
  capabilities,
  mode,
  busy,
  approver,
  comment,
  canPrepare,
  canDecide,
  approvalCapable,
  decisionPending,
  nextCaseId,
  onComment,
  onApprove,
  onRequestChanges,
  onReject,
  onPrepare,
  onNext,
  onViewAudit,
}: {
  caseFile: CaseFile;
  mode: CaseMode;
  busy: "approving" | "preparing" | null;
  approver: string;
  capabilities: Capabilities | null;
  comment: string;
  canPrepare: boolean;
  canDecide: boolean;
  approvalCapable: boolean;
  decisionPending: boolean;
  nextCaseId: string | null;
  onComment: (value: string) => void;
  onApprove: () => void;
  onRequestChanges: () => void;
  onReject: () => void;
  onPrepare: () => void;
  onNext: () => void;
  onViewAudit: () => void;
}) {
  const { t, lang } = useI18n();
  const plan = caseFile.plan;
  const state = caseFile.state;
  const execution = caseFile.execution;
  const fixture = mode === "fixture";
  const awaiting = state === "awaiting_approval";
  const rejection = (caseFile.audit ?? []).slice().reverse().find((row): row is Record<string, unknown> => !!row && typeof row === "object" && "action" in row && row.action === "rejected");
  const authorizationPending = caseFile.decision?.authorization_pending === true;
  const checksInfo = effectiveChecks(caseFile, t);
  const floor = marginFloor(plan);
  const spend = planNewSpend(plan);
  const orders = orderCount(caseFile.order.ref);
  const dateCheck = checksInfo.checks.find(
    (check) =>
      check.code === "on_time" ||
      check.code === "date" ||
      check.code.includes("date"),
  );
  const datePlanChecked = dateCheck?.status === "pass";
  const blockedChecks = checksInfo.checks.filter(
    (check) => check.status === "block",
  );
  const gate = approvalGate({
    caseFile,
    checks: checksInfo.checks,
    fixture,
    live: mode === "live",
    awaiting,
    canDecide,
    approvalCapable,
    capabilities,
    busy: busy !== null,
    t,
  });
  const canApprove = gate.canApprove && blockedChecks.length === 0;
  const block = gate.block;

  if (execution) {
    return (
      <div className="decision-panel" data-decision-panel="true">
        <ExecutionReceipt
          caseFile={caseFile}
          nextCaseId={nextCaseId}
          onNext={onNext}
          onViewAudit={onViewAudit}
        />
      </div>
    );
  }

  if (!plan) {
    return (
      <div className="decision-panel" data-decision-panel="true">
        <section className="decision-block">
          <div className="decision-block-head">
            <h3>{t(state === "awaiting_selection" ? "candidate.awaiting" : state === "rejected" ? "case.state.rejected" : "case.noPlanTitle")}</h3>
            <span className={`case-state-pill is-${stateTone(state)}`}>
              {caseOutcomeLabel(caseFile, t)}
            </span>
          </div>
          <p className="decision-note">
            {state === "rejected" && caseFile.comparison ? t("candidate.recordedNote") : preparationMessage(caseFile, t)}
          </p>
          {mode === "live" && state === "preparing" && !caseFile.decision?.repreparation_pending ? (
            <button
              className="secondary decision-secondary"
              type="button"
              onClick={onPrepare}
              disabled={busy !== null || !canPrepare || caseFile.decision?.can_resume_preparation === false}
              data-prepare-case="true"
            >
              {busy === "preparing" ? t("case.preparingPlan") : t(caseFile.decision?.preparation_pending ? "v10.resumePreparation" : "case.retryPrepare")}
            </button>
          ) : null}
        </section>
        {state === "rejected" && <section className="decision-block" data-rejection-receipt="true"><h3>{t("case.state.rejected")}</h3><p>{typeof rejection?.comment === "string" ? rejection.comment : t("runs.notRecorded")}</p><dl className="facts"><dt>{t("decision.receiptApprover")}</dt><dd>{typeof rejection?.actor_label === "string" ? rejection.actor_label : t("runs.notRecorded")}</dd><dt>{t("decision.receiptTime")}</dt><dd>{formatDateTime(typeof rejection?.at === "string" ? rejection.at : null, lang)}</dd></dl></section>}
        {fixture ? (
          <p className="decision-disabled">{t("decision.fixtureDisabled")}</p>
        ) : null}
      </div>
    );
  }

  const scopeBody = t("decision.scopeBody", {
    documents: uniqueActionDocuments(caseFile),
    units: formatQuantity(caseFile.order.units),
    due: dueWindow(caseFile, t),
  });

  return (
    <div className="decision-panel" data-decision-panel="true">
      {plan.objective && <p data-selected-candidate="true">{caseFile.decision?.selection_pending ? `${t("candidate.lastConfirmed")}: ${candidateName(plan, t)}` : t("candidate.selectedScope", { name: candidateName(plan, t) })}</p>}
      {caseFile.decision?.revision_pending && <p className="decision-note" data-displayed-plan="true">{t("v10.oldPlanVersion", { version: caseFile.revision?.version ?? 1 })}</p>}
      <div className="kpi-grid">
        <div className="kpi" data-kpi="new-spend">
          <span className="kpi-label">{t("decision.newSpend")}</span>
          <strong className="kpi-value">{formatCurrency(spend)}</strong>
          <span className="kpi-note">{t("decision.newSpendNote")}</span>
        </div>
        <div className="kpi" data-kpi="margin" data-margin-verified={checksInfo.checks.some(check => check.code.includes("margin") && check.status === "pass") ? "true" : "false"}>
          <span className="kpi-label">{t("decision.margin")}</span>
          <strong className="kpi-value">{formatPercent(plan.margin)}</strong>
          {floor !== null ? (
            <span className="kpi-note">
              {t("decision.marginFloor", { floor })}
            </span>
          ) : (
            <span className="kpi-note is-planned">
              {t("decision.marginPlanned")}
            </span>
          )}
          <span className="kpi-note">{t("decision.marginBasis")}</span>
        </div>
        <div className="kpi" data-kpi="due">
          <span className="kpi-label">{t("decision.due")}</span>
          <strong className="kpi-value">
            {dueWindow(caseFile, t)}
          </strong>
          <span className="kpi-note">
            {plan.on_time
              ? datePlanChecked
                ? t("case.onTimeVerified")
                : t("case.onTimePlanned")
              : plan.on_time === false ? t("v9.onTimeFailed") : t("v11.dueUnmeasured")}
          </span>
        </div>
        <div className="kpi" data-kpi="units">
          <span className="kpi-label">{t("decision.units")}</span>
          <strong className="kpi-value">
            {formatQuantity(caseFile.order.units)}
          </strong>
          <span className="kpi-note">
            {orders
              ? t("decision.unitsValue", {
                  units: formatQuantity(caseFile.order.units),
                  orders,
                })
              : t("decision.unitsValuePlain", {
                  units: formatQuantity(caseFile.order.units),
                })}
          </span>
        </div>
      </div>

      <section className="decision-block" data-decision-scope="true">
        <div className="decision-block-head">
          <h3>{t("decision.scopeTitle")}</h3>
        </div>
        <p className="decision-scope">{scopeBody}</p>
      </section>

      <PolicyChecks checks={checksInfo.checks} derived={checksInfo.derived} unknownChecks={caseFile.decision?.unknown_checks} />
      {state === "rejected" && <section className="decision-block" data-rejection-receipt="true"><h3>{t("case.state.rejected")}</h3><p>{typeof rejection?.comment === "string" ? rejection.comment : t("runs.notRecorded")}</p><dl className="facts"><dt>{t("decision.receiptApprover")}</dt><dd>{typeof rejection?.actor_label === "string" ? rejection.actor_label : t("runs.notRecorded")}</dd><dt>{t("decision.receiptTime")}</dt><dd>{formatDateTime(typeof rejection?.at === "string" ? rejection.at : null, lang)}</dd></dl></section>}

      <section className="decision-block" data-decision-approval="true">
        <div className="field">
          <label htmlFor={`case-approver-${caseFile.case_id}`}>
            {t("decision.approver")}
          </label>
          <input
            id={`case-approver-${caseFile.case_id}`}
            value={approverLabel(approver, t)}
            title={approver}
            readOnly
            aria-readonly="true"
            autoComplete="name"
          />
        </div>
        <div className="field">
          <label htmlFor={`case-comment-${caseFile.case_id}`}>
            {t("decision.comment")}
          </label>
          <textarea
            id={`case-comment-${caseFile.case_id}`}
            value={comment}
            onChange={(event) => onComment(event.target.value)}
            placeholder={t("decision.commentPlaceholder")}
            disabled={!canApprove || authorizationPending}
            maxLength={2000}
            rows={2}
          />
        </div>
        <p className="decision-note">{t("v9.commentSaved")}</p>
        {authorizationPending && <p className="decision-note" data-authorization-pending="true">{t("v10.authorizationPending")}</p>}

        <div className="decision-actions">
          <button
            className="primary decision-primary"
            type="button"
            onClick={onApprove}
            disabled={!canApprove}
            data-case-approve="true"
          >
            {busy === "approving" ? t(state === "executing" ? "decision.approving" : "v10.submittingApproval") : t(authorizationPending ? "v10.continueApproval" : "decision.approve")}
          </button>
          <button
            className="secondary"
            type="button"
            onClick={onRequestChanges}
            disabled={fixture || !canPrepare || !awaiting || busy !== null || caseFile.decision?.can_request_changes !== true || !validRevisionMargin(suggestedRevisionMargin(caseFile), revisionFloor(caseFile))}
            title={!validRevisionMargin(suggestedRevisionMargin(caseFile), revisionFloor(caseFile)) ? t(revisionFloor(caseFile) === null ? "v10.marginUnknown" : "v10.invalidMargin") : caseFile.decision?.can_request_changes === true ? undefined : !awaiting ? caseOutcomeLabel(caseFile, t) : t("v9.reviseUnavailable")}
            data-case-request-changes="true"
          >
            {t("decision.requestChanges")}
          </button>
          <button
            className="secondary decision-reject"
            type="button"
            onClick={onReject}
            disabled={fixture || !canDecide || !awaiting || busy !== null || caseFile.decision?.can_reject !== true}
            data-case-reject="true"
          >
            {t("decision.reject")}
          </button>
        </div>

        {fixture ? (
          <p className="decision-disabled" data-decision-disabled="true">
            {t("decision.fixtureDisabled")}
          </p>
        ) : busy !== null || decisionPending ? (
          <p className="decision-note" data-decision-in-progress="true">
            {t("v10.operationPending")}
          </p>
        ) : !canApprove && block ? (
          <div className="decision-disabled" data-decision-disabled="true" data-block-kind={block.kind}>
            <p className="decision-disabled-reason">{block.reason}</p>
            {block.recovery ? <p className="decision-disabled-recovery" data-block-recovery="true">{block.recovery}</p> : null}
          </div>
        ) : (
          <p className="decision-note">{t("decision.approvalHint")}</p>
        )}
        <p className="decision-note">{t("decision.expiry")}</p>
      </section>

    </div>
  );
}

function InboxPanel({
  caseFile,
  mode,
  activity,
  evidenceRef,
  onOpenEvidence,
}: {
  caseFile: CaseFile;
  mode: CaseMode;
  activity: ActivityState | null;
  evidenceRef: RefObject<HTMLDivElement | null>;
  onOpenEvidence: () => void;
}) {
  const { t, lang } = useI18n();
  const execution = caseFile.execution;
  const summary = verifierSummary(execution, t, caseFile.result);
  return (
    <div className="inbox-panel" data-inbox-panel="true">
      <section className="decision-block" data-inbox-activity="true">
        <div className="decision-block-head">
          <h3>{t("inbox.activity")}</h3>
        </div>
        {activity ? (
          <ActivityPanel
            state={activity}
            lang={lang}
            className="case-inbox-activity"
            defaultOpen
          />
        ) : (
          <p className="decision-note">{t("inbox.activityEmpty")}</p>
        )}
      </section>

      <section className="decision-block" data-inbox-summary="true">
        <div className="decision-block-head">
          <h3>{t("inbox.caseSummary")}</h3>
          <span className={`case-state-pill is-${isCaseVerified(caseFile) ? "green" : caseFile.execution ? "neutral" : stateTone(caseFile.state)}`}>
            {caseOutcomeLabel(caseFile, t)}
          </span>
        </div>
        <dl className="inbox-facts">
          <dt>{t("inbox.caseId")}</dt>
          <dd className="mono">{shortId(caseFile.case_id, 12)}</dd>
          <dt>{t("inbox.planId")}</dt>
          <dd className="mono">{shortId(caseFile.plan?.plan_id, 12)}</dd>
          <dt>{t("inbox.state")}</dt>
          <dd>{caseOutcomeLabel(caseFile, t)}</dd>
          <dt>{t("inbox.data")}</dt>
          <dd>
            {mode === "fixture"
              ? t("case.dataSynthetic")
              : caseFile.synthetic
                ? t("case.dataSyntheticLive")
                : t("case.dataLive")}
          </dd>
          <dt>{t("inbox.verifier")}</dt>
          <dd>{summary.label}</dd>
        </dl>
        <button
          className="secondary"
          type="button"
          onClick={onOpenEvidence}
          data-inbox-open-evidence="true"
        >
          {t("inbox.openEvidence")}
        </button>
      </section>

      <p className="decision-note">{t("v9.noRunLink")}</p>
      <EvidencePanel caseFile={caseFile} mode={mode} evidenceRef={evidenceRef} />
    </div>
  );
}

type ProgressEvent = {
  phase?: string;
  status?: string;
  detail?: string;
};

function applyProgressEvent(
  previous: ActivityState,
  event: ProgressEvent,
): ActivityState {
  const phase = event.phase;
  const status = event.status;
  if (!phase || !status) return previous;
  const stepId =
    phase === "approval"
      ? "freeze"
      : phase === "apply"
        ? "apply"
        : phase === "readback"
          ? "readback"
          : phase === "verify"
            ? "verify"
            : null;
  if (!stepId) return previous;
  const normalized =
    status === "done" || status === "failed" || status === "active"
      ? status
      : "active";
  return {
    ...previous,
    steps: previous.steps.map((step) =>
      step.id === stepId
        ? {
            ...step,
            status: normalized,
            detail:
              event.detail && normalized === "failed"
                ? event.detail
                : step.detail,
          }
        : step,
    ),
  };
}

export function CaseDetail({
  caseFile,
  mode,
  capabilities,
  caseReadBlocked = false,
  evidenceOpen = false,
  nextCaseId,
  onBack,
  onAsk,
  onUpdated,
  onOperationPendingChange,
  onNext,
  onOpenEvidence,
  lastReadAt = null,
}: Props) {
  const { t, lang } = useI18n();
  const [tab, setTab] = useState<"decision" | "inbox">("decision");
  const [planView, setPlanView] = useState<"table" | "diff">("table");
  const [busy, setBusy] = useState<"approving" | "preparing" | null>(null);
  const [activity, setActivity] = useState<ActivityState | null>(null);
  const [streamProgressSeen, setStreamProgressSeen] = useState(false);
  const approvalStream = useRef<EventSource | null>(null);
  useEffect(() => () => { approvalStream.current?.close(); }, []);
  const [error, setError] = useState<string | null>(null);
  const [decisionDialog, setDecisionDialog] = useState<"reject" | "revision" | "reprepare" | null>(null);
  const [decisionTarget, setDecisionTarget] = useState<{ version: number; planId: string | null } | null>(null);
  const [decisionReason, setDecisionReason] = useState("");
  const [revisionMargin, setRevisionMargin] = useState(0);
  const [decisionBusy, setDecisionBusy] = useState(false);
  useEffect(() => {
    onOperationPendingChange?.(busy !== null || decisionBusy);
    return () => onOperationPendingChange?.(false);
  }, [busy, decisionBusy, onOperationPendingChange]);
  const [riskOpen, setRiskOpen] = useState(false);
  const [cancelUnstartedOpen, setCancelUnstartedOpen] = useState(false);
  const [riskAcknowledged, setRiskAcknowledged] = useState(false);
  const { display_name: approver } = useWorkspace();
  const [confirmedPlan, setConfirmedPlan] = useState<string | null>(null);
  const [caseReadRequired, setCaseReadRequired] = useState(false);
  const writesBlocked = caseReadRequired || caseReadBlocked
    || (mode === "live" && environmentAccessNotice(t, capabilities, caseFile) !== null);
  useEffect(() => { setCaseReadRequired(false); }, [caseFile]);
  const [comment, setComment] = useState("");
  const evidenceRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setRiskOpen(false); setRiskAcknowledged(false); setConfirmedPlan(null);
    setCancelUnstartedOpen(false);
  }, [caseFile.plan?.plan_id, caseFile.state, caseFile.decision?.selection_pending]);
  useEffect(() => { setComment(caseFile.approval?.comment ?? ""); }, [caseFile.plan?.plan_id]);
  useEffect(() => {
    if (evidenceOpen) { setTab("inbox"); window.setTimeout(() => evidenceRef.current?.scrollIntoView({ block: "start" }), 0); }
  }, [evidenceOpen]);
  useEffect(() => {
    if (writesBlocked) {
      setRiskOpen(false); setRiskAcknowledged(false); setConfirmedPlan(null);
      setDecisionDialog(null);
      setCancelUnstartedOpen(false);
    }
  }, [writesBlocked]);

  const derivedActivity = useMemo(
    () => buildCaseActivity(caseFile, t),
    [caseFile, t],
  );
  const approvalInFlight = !caseFile.execution && ["awaiting_approval", "executing"].includes(caseFile.state);
  const shownActivity = busy === "approving" && approvalInFlight &&
    (caseFile.state === "awaiting_approval" || streamProgressSeen) ? activity ?? derivedActivity : derivedActivity;
  const plan = caseFile.plan;
  const comparison = caseFile.comparison;
  const decisionPlanId = plan?.plan_id ?? (comparison?.status === "ready" ? comparison.plans[0]?.plan_id ?? null : null);
  const planApproved = Boolean(plan && !caseFile.decision?.authorization_pending && caseFile.approval?.plan_id === plan.plan_id);
  const changesTitle = caseFile.decision?.revision_pending
    ? t("v10.oldPlanVersion", { version: caseFile.revision?.version ?? 1 })
    : t(planApproved ? "case.approvedChangesTitle" : "case.proposedTitle");
  const dialogTargetMatches = decisionTarget?.version === (caseFile.revision?.version ?? 1)
    && decisionTarget?.planId === decisionPlanId;
  const dialogPermitted = mode === "live" && (decisionDialog === "reject"
    ? capabilities?.case_approve === true && caseFile.decision?.can_reject === true
    : capabilities?.case_prepare === true && (decisionDialog === "reprepare"
      ? caseFile.decision?.can_reprepare === true
      : caseFile.decision?.can_request_changes === true && validRevisionMargin(revisionMargin, revisionFloor(caseFile))));
  const severity = severityOf(caseFile);
  const mainActiveActivity = shownActivity?.active ? shownActivity : null;
  const isSupplier = caseFile.disruption.kind.includes("supplier");
  const heading = t(
    isSupplier ? "case.headingSupplierCancellation" : caseFile.disruption.kind === "workcenter_outage" ? "case.headingWorkcenter" : "case.headingGeneric",
    { product: caseFile.order.product },
  );
  const orders = orderCount(caseFile.order.ref);

  // The sticky decision bar uses the same gate as the decision panel, so the
  // reason it shows can never drift from the reason the panel shows.
  const stickyGate = approvalGate({
    caseFile,
    checks: effectiveChecks(caseFile, t).checks,
    fixture: mode === "fixture",
    live: mode === "live",
    awaiting: caseFile.state === "awaiting_approval",
    canDecide: !writesBlocked && capabilities?.case_approve === true,
    approvalCapable: capabilities?.case_approve === true,
    capabilities,
    busy: busy !== null || decisionBusy,
    t,
  });

  const runApprove = async () => {
    if (writesBlocked || mode !== "live" || capabilities?.case_approve !== true || !plan || busy !== null || decisionBusy || caseFile.state !== "awaiting_approval" || !caseFile.decision?.can_approve || !riskAcknowledged || confirmedPlan !== plan.plan_id) {
      return;
    }
    setBusy("approving");
    setStreamProgressSeen(false);
    setError(null);
    const startedAt = Date.now();
    setActivity({
      active: true,
      startedAt,
      hideDuration: true,
      title: t("case.activityTitle"),
      tone: "verify",
      steps: [
        { id: "freeze", kind: "approval", label: t("case.activityApproval"), status: "active" },
        { id: "apply", kind: "executor", label: t("case.activityApply"), status: "pending" },
        { id: "readback", kind: "readback", label: t("case.activityReadback"), status: "pending" },
        { id: "verify", kind: "verify", label: t("case.activityVerify"), status: "pending" },
      ],
    });
    let progressSource: EventSource | null = null;
    if (typeof EventSource !== "undefined") {
      progressSource = new EventSource(
        `/api/product/cases/${encodeURIComponent(caseFile.case_id)}/events`,
      );
      approvalStream.current = progressSource;
      progressSource.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data) as ProgressEvent;
          if (parsed.phase && ["approval", "apply", "readback", "verify"].includes(parsed.phase) && parsed.status) setStreamProgressSeen(true);
          setActivity((previous) =>
            previous ? applyProgressEvent(previous, parsed) : previous,
          );
        } catch {
          // Progress is an enhancement; a malformed frame must not break approval.
        }
      };
      progressSource.onerror = () => {
        setStreamProgressSeen(false);
        progressSource?.close();
        approvalStream.current = null;
        progressSource = null;
      };
    }
    try {
      const updated = await approveProductCase(caseFile.case_id, plan.plan_id, comment);
      onUpdated(updated);
      setActivity(buildCaseActivity(updated, t));
    } catch (failure) {
      setError(t("decision.approveFailed", { detail: errorDetail(failure) }));
      setActivity((previous) =>
        previous
          ? {
              ...previous,
              active: false,
              finishedAt: Date.now(),
              steps: previous.steps.map((step) =>
                step.status === "active"
                  ? { ...step, status: "failed" as const, detail: t("case.activityFailed") }
                  : step,
              ),
            }
          : previous,
      );
      try {
        const refreshed = await getProductCase(caseFile.case_id);
        onUpdated(refreshed);
        setActivity(null);
        if (refreshed.execution) setError(t("v9.approvalReceiptReloaded"));
      } catch {
        setCaseReadRequired(true);
        setError(t("v9.refreshBeforeDecision"));
      }
    } finally {
      progressSource?.close();
      approvalStream.current = null;
      setBusy(null);
    }
  };

  const runPrepare = async () => {
    if (writesBlocked || mode !== "live" || busy !== null || decisionBusy || capabilities?.case_prepare !== true || !canContinuePreparation(caseFile)) return;
    setBusy("preparing");
    setError(null);
    try {
      onUpdated(await prepareProductCase(caseFile.case_id));
    } catch (failure) {
      try {
        const refreshed = await getProductCase(caseFile.case_id);
        onUpdated(refreshed);
        setError(refreshed.decision?.revision_pending || refreshed.decision?.repreparation_pending || refreshed.decision?.preparation_pending
          ? preparationMessage(refreshed, t) : refreshed.plan || refreshed.comparison?.status === "ready" ? null : preparationFailure(refreshed.reason ?? errorDetail(failure), t));
      } catch {
        setCaseReadRequired(true);
        setError(t("v9.refreshBeforeDecision"));
      }
    } finally {
      setBusy(null);
    }
  };

  const viewAudit = () => {
    setTab("inbox");
    window.setTimeout(() => {
      evidenceRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  };

  const runSelect = async (planId?: string) => {
    if (writesBlocked || mode !== "live" || capabilities?.case_approve !== true || busy !== null || decisionBusy
        || comparison?.status !== "ready") return;
    const pending = caseFile.decision?.selection_pending === true;
    const saved = caseFile.selection_request;
    if (pending ? !caseFile.decision?.can_resume_selection || !saved
      : !caseFile.decision?.can_select_candidate || !comparison.plans.some(candidate => candidate.plan_id === planId)) return;
    const request = pending && saved
      ? { plan_id: saved.plan_id, comparison_id: saved.comparison_id, selection_revision: saved.selection_revision }
      : { plan_id: planId!, comparison_id: comparison.comparison_id, selection_revision: comparison.selection_revision };
    setDecisionBusy(true); setError(null); setRiskOpen(false); setRiskAcknowledged(false); setConfirmedPlan(null);
    try {
      onUpdated(await selectProductCandidate(caseFile.case_id, request));
    } catch {
      try {
        const refreshed = await getProductCase(caseFile.case_id);
        onUpdated(refreshed);
        const confirmed = refreshed.comparison?.status === "ready"
          && refreshed.comparison.comparison_id === request.comparison_id
          && refreshed.comparison.selected_plan_id === request.plan_id
          && refreshed.comparison.selection_revision > request.selection_revision;
        setError(confirmed || refreshed.decision?.selection_pending
          ? null : t("candidate.unconfirmed"));
      } catch {
        setCaseReadRequired(true); setError(t("v9.refreshBeforeDecision"));
      }
    } finally { setDecisionBusy(false); }
  };

  const submitDecision = async () => {
    const reprepare = decisionDialog === "reprepare";
    if (writesBlocked || !decisionDialog || decisionBusy || busy !== null || mode !== "live"
        || decisionTarget?.version !== (caseFile.revision?.version ?? 1) || decisionTarget.planId !== decisionPlanId
        || (decisionDialog === "reject" ? capabilities?.case_approve !== true || !caseFile.decision?.can_reject
          : capabilities?.case_prepare !== true)
        || (reprepare ? !caseFile.decision?.can_reprepare : !decisionPlanId)
        || (decisionDialog === "revision" && (!caseFile.decision?.can_request_changes || !validRevisionMargin(revisionMargin, revisionFloor(caseFile))))) return;
    if (!decisionReason.trim()) { setError(t("v9.reasonRequired")); return; }
    setDecisionBusy(true); setError(null);
    try {
      const next = reprepare ? await reprepareProductCase(caseFile.case_id, decisionTarget.version, decisionReason.trim())
        : decisionDialog === "reject"
        ? await rejectProductCase(caseFile.case_id, decisionPlanId!, decisionReason.trim())
        : await reviseProductCase(caseFile.case_id, plan!.plan_id, decisionReason.trim(), { minimum_margin_pct: revisionMargin });
      onUpdated(next); setDecisionDialog(null); setDecisionReason("");
    } catch (failure) {
      try {
        const refreshed = await getProductCase(caseFile.case_id);
        onUpdated(refreshed); setDecisionDialog(null);
        const confirmed = decisionDialog === "reject" ? refreshed.state === "rejected"
          : (refreshed.plan !== null || refreshed.comparison?.status === "ready") && (refreshed.revision?.version ?? 1) > decisionTarget.version;
        setError(confirmed ? null : refreshed.decision?.revision_pending || refreshed.decision?.repreparation_pending
          ? preparationMessage(refreshed, t) : decisionDialog === "reject" ? t("v10.decisionUnconfirmed")
          : preparationFailure(refreshed.reason ?? errorDetail(failure), t));
      } catch {
        setCaseReadRequired(true); setError(t("v9.refreshBeforeDecision"));
      }
    }
    finally { setDecisionBusy(false); }
  };
  const openDecision = (kind: "reject" | "revision" | "reprepare") => {
    setError(null); setDecisionReason("");
    setDecisionTarget({ version: caseFile.revision?.version ?? 1, planId: decisionPlanId });
    setRevisionMargin(suggestedRevisionMargin(caseFile)); setDecisionDialog(kind);
  };
  const reconcile = async () => {
    if (writesBlocked || decisionBusy || mode === "fixture" || !caseFile.decision?.can_reconcile) return;
    setDecisionBusy(true); setError(null);
    try { onUpdated(await reconcileProductCase(caseFile.case_id)); }
    catch (failure) { setError(errorDetail(failure)); }
    finally { setDecisionBusy(false); }
  };
  const cancelUnstarted = async () => {
    if (writesBlocked || decisionBusy || busy || mode !== "live" || !plan || capabilities?.case_approve !== true || !caseFile.decision?.can_cancel_unstarted) return;
    setDecisionBusy(true); setError(null);
    try {
      onUpdated(await cancelUnstartedProductCase(caseFile.case_id, plan.plan_id));
      setCancelUnstartedOpen(false);
    } catch (failure) {
      setError(t(errorDetail(failure).includes("product_dispatch_present") ? "v10.dispatchPresent" : "v10.cancellationUnknown"));
    } finally { setDecisionBusy(false); }
  };

  return (
    <article className="case-detail" data-case-detail="true" data-case-mode={mode}>
      <div className="case-detail-nav">
        <button className="case-back" type="button" onClick={onBack}>
          <span aria-hidden="true">←</span> {t("case.back")}
        </button>
        <div className="case-detail-actions">
          <button
            className="secondary case-ask"
            type="button"
            onClick={onAsk}
            data-ask-case="true"
          >
            <span aria-hidden="true">✦</span> {t("case.ask")}
          </button>
          <button className="secondary" type="button" data-export-receipt="true"
            onClick={() => downloadReceipt(caseFile, lang, capabilities?.deployment ?? "local")}>{t("v11.exportReceipt")}</button>
        </div>
      </div>

      <header className="case-detail-head">
        <div className="case-title-row">
          <div className="case-title-copy">
            <h1>{heading}</h1>
            <p className="case-detail-sub">
              {t("cases.productLine", {
                product: caseFile.order.product,
                units: formatQuantity(caseFile.order.units),
              })}
              {orders ? ` · ${t("cases.ordersCount", { count: orders })}` : ""}
              {caseFile.order.customer !== `${orders} confirmed sales orders` && <> · {caseFile.order.customer}</>}
            </p>
          </div>
          <span
            className={`case-state-pill is-${isCaseVerified(caseFile) ? "green" : caseFile.execution ? "neutral" : stateTone(caseFile.state)}`}
            data-case-state={caseFile.state}
            role="status"
          >
            <span className="case-state-dot" aria-hidden="true" />
            {caseOutcomeLabel(caseFile, t)}
          </span>
        </div>

        <details className="case-meta-details" data-case-meta-details="true">
          <summary>{t("case.metaSummary")}</summary>
          {lastReadAt ? (
            <p className="case-meta-read" data-case-last-confirmed="true">{t("v9.lastCaseRead", { time: formatDateTime(lastReadAt, lang) })}</p>
          ) : null}
          <dl className="case-meta">
          <div>
            <dt>{t("case.severityLabel")}</dt>
            <dd>
              <span className={`case-severity is-${severity}`} data-severity={severity}>
                <span className="case-severity-dot" aria-hidden="true" />
                {severityLabel(severity, t)}
              </span>
            </dd>
          </div>
          <div>
            <dt>{t("case.dueLabel")}</dt>
            <dd>{dueWindow(caseFile, t)}</dd>
          </div>
          <div>
            <dt>{t("case.ownerLabel")}</dt>
            <dd>{mode === "fixture" ? t("cases.ownerDemo") : t("cases.ownerYou")}</dd>
          </div>
          <div>
            <dt>{t("case.ageLabel")}</dt>
            <dd>{formatAge(caseFile.created_at, t)}</dd>
          </div>
          <div>
            <dt>{t("case.step.detected")}</dt>
            <dd>{formatDateTime(caseFile.created_at, lang)}</dd>
          </div>
        </dl>
        </details>

        <StatusStepper caseFile={caseFile} />
      </header>

      {mode === "fixture" ? (
        // One statement only: the environment banner at the top already says
        // this is a demo with no ERP writes. Here we say what that means.
        <div className="case-demo-banner" data-demo-banner="true">
          <span className="cases-demo-mark" aria-hidden="true">◈</span>
          <div>
            <p>{t("cases.demoExplain")}</p>
          </div>
        </div>
      ) : null}

      {error ? (
        <p className="case-error" role="alert">{error}</p>
      ) : null}

      <DecisionSummary caseFile={caseFile} onEvidence={() => { setTab("inbox"); window.setTimeout(() => { evidenceRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }); evidenceRef.current?.focus(); }, 0); }} />

      <div className="case-detail-grid">
        <div className="case-detail-main">
          <section className="case-section" data-case-timeline-section="true">
            <div className="case-section-head">
              <h2>{t("case.whatHappened")}</h2>
              {/* The header pill already states the outcome; repeat it only when
                  the execution outcome itself is unknown, which needs saying. */}
              {isExecutionUnknown(caseFile) ? (
                <span className="case-state-caption">{caseOutcomeLabel(caseFile, t)}</span>
              ) : null}
            </div>
            <Timeline caseFile={caseFile} />
          </section>

          <PreparationStatus caseFile={caseFile}
            disabled={writesBlocked || mode !== "live" || busy !== null || decisionBusy || capabilities?.case_prepare !== true}
            canContinue={canContinuePreparation(caseFile)} onContinue={() => void runPrepare()}
            onReprepare={() => openDecision("reprepare")} />

          <CandidateComparison caseFile={caseFile} readOnly={mode !== "live"}
            disabled={writesBlocked || busy !== null || decisionBusy || capabilities?.case_approve !== true}
            onSelect={planId => void runSelect(planId)} onResume={() => void runSelect()}
            onReject={() => openDecision("reject")}
            renderPlan={candidate => <><ActionDiff plan={candidate} />
              <PolicyChecks checks={effectiveChecks({ ...caseFile, plan: candidate }, t).checks}
                derived={effectiveChecks({ ...caseFile, plan: candidate }, t).derived} /></>} />

          <CaseFacts caseFile={caseFile} mode={mode} />

          <section className="case-section" data-case-changes="true">
            <div className="case-section-head">
              <div>
                <h2>{changesTitle}</h2>
                {caseFile.revision && <p className="muted">{t("v9.planVersion", { version: caseFile.revision.version })}{caseFile.revision.reason ? ` · ${caseFile.revision.reason}` : ""}</p>}
                {plan ? (
                  <p className="muted case-section-sub">
                    {t(caseFile.decision?.revision_pending ? "v10.earlierDocuments" : planApproved ? "case.approvedChangesMeta" : "case.proposedMeta", { count: uniqueActionDocuments(caseFile) })}
                  </p>
                ) : null}
              </div>
              {plan ? (
                <div className="segmented" role="group" aria-label={changesTitle}>
                  <button
                    type="button"
                    className={planView === "table" ? "is-active" : ""}
                    aria-pressed={planView === "table"}
                    onClick={() => setPlanView("table")}
                    data-plan-view="table"
                  >
                    {t("case.tableView")}
                  </button>
                  <button
                    type="button"
                    className={planView === "diff" ? "is-active" : ""}
                    aria-pressed={planView === "diff"}
                    onClick={() => setPlanView("diff")}
                    data-plan-view="diff"
                  >
                    {t("case.diffView")}
                  </button>
                </div>
              ) : null}
            </div>

            {plan ? (
              planView === "table" ? (
                <ActionTable plan={plan} caseFile={caseFile} />
              ) : (
                <ActionDiff plan={plan} />
              )
            ) : (
              <div className="case-no-plan">
                <h3>{t(caseFile.state === "awaiting_selection" ? "candidate.awaiting" : "case.noPlanTitle")}</h3>
                <p className="muted">
                  {preparationMessage(caseFile, t)}
                </p>
                {mode === "live" && caseFile.state === "preparing" && !caseFile.decision?.repreparation_pending ? (
                  <button
                    className="secondary"
                    type="button"
                    onClick={() => void runPrepare()}
                    disabled={writesBlocked || busy !== null || capabilities?.case_prepare !== true || caseFile.decision?.can_resume_preparation === false}
                    data-prepare-case="true"
                  >
                    {busy === "preparing" ? t("case.preparingPlan") : t(caseFile.decision?.preparation_pending ? "v10.resumePreparation" : "case.prepare")}
                  </button>
                ) : null}
              </div>
            )}
          </section>

          {mainActiveActivity ? (
            <ActivityPanel
              state={mainActiveActivity}
              lang={lang}
              className="case-main-activity"
            />
          ) : null}

          {plan?.costs ? <CostBreakdown costs={plan.costs} actions={plan.actions} /> : null}
          {plan?.order_readiness ? <OrderReadiness plan={plan} /> : null}
          {plan?.impact_chain ? <ImpactChain plan={plan} /> : null}
          {plan?.decision_metrics ? <DecisionFacts plan={plan} /> : null}
          {plan && caseFile.approval?.comment?.trim() ? (
            caseFile.execution ? (
              <details className="case-section case-technical" data-technical-details="true">
                <summary>{t("decisionFacts.choice")}</summary>
                <ChoiceReason caseFile={caseFile} />
              </details>
            ) : <ChoiceReason caseFile={caseFile} />
          ) : null}
          {caseFile.execution ? <VerificationSection caseFile={caseFile} /> : null}
          {plan && missingPlanFacts(plan, caseFile).length ? (
            <section className="case-section case-missing-summary" data-case-missing="true">
              <h2>{t("v11.missingTitle")}</h2>
              <p>{t("v11.missingBody", { items: missingPlanFacts(plan, caseFile).map(item => t(`v11.missing.${item}` as MessageKey)).join(t("v11.listSeparator")) })}</p>
            </section>
          ) : null}
        </div>

        <aside className="case-detail-rail">
          <div className="rail-tabs" role="tablist" aria-label={t("decision.title")}>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "decision"}
              className={tab === "decision" ? "is-active" : ""}
              onClick={() => setTab("decision")}
              data-rail-tab="decision"
            >
              {t("decision.tab")}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "inbox"}
              className={tab === "inbox" ? "is-active" : ""}
              onClick={() => setTab("inbox")}
              data-rail-tab="inbox"
            >
              {t("inbox.tab")}
              {caseFile.execution ? <span className="rail-tab-dot" aria-hidden="true" /> : null}
            </button>
          </div>

          {tab === "decision" ? (
            <DecisionPanel
              caseFile={caseFile}
              capabilities={capabilities}
              mode={mode}
              busy={busy}
              decisionPending={decisionBusy}
              approver={approver}
              comment={comment}
              canPrepare={!writesBlocked && !decisionBusy && capabilities?.case_prepare === true}
              canDecide={!writesBlocked && !decisionBusy && capabilities?.case_approve === true}
              approvalCapable={capabilities?.case_approve === true}
              nextCaseId={nextCaseId}
              onComment={setComment}
              onApprove={() => { setConfirmedPlan(plan?.plan_id ?? null); setRiskAcknowledged(false); setRiskOpen(true); }}
              onRequestChanges={() => openDecision("revision")}
              onReject={() => openDecision("reject")}
              onPrepare={() => void runPrepare()}
              onNext={onNext}
              onViewAudit={viewAudit}
            />
          ) : (
            <InboxPanel
              caseFile={caseFile}
              mode={mode}
              activity={shownActivity}
              evidenceRef={evidenceRef}
              onOpenEvidence={onOpenEvidence}
            />
          )}
        </aside>
      </div>

      <Modal className="decision-confirm" open={riskOpen} onClose={() => setRiskOpen(false)} closeLabel={t("v9.cancel")}
        title={t(caseFile.decision?.authorization_pending ? "v10.continueApproval" : "decision.approve")}
        description={caseFile.decision?.authorization_pending ? t("v10.authorizationPending") : (plan?.objective ? t("candidate.confirmScope", { name: candidateName(plan, t) }) + " " : "") + t("v9.confirmBody", { actions: actionCountsLabel(plan?.actions ?? [], t), plan: shortId(plan?.plan_id, 12), count: uniqueActionDocuments(caseFile), spend: formatCurrency(planNewSpend(plan)) }) + (plan?.costs ? ` ${t("cost.confirmNote")}` : "")}
        contentClassName="decision-confirm-content"
        footer={<><button type="button" data-modal-autofocus className="secondary" onClick={() => setRiskOpen(false)}>{t("v9.cancel")}</button>
          <button type="button" className="primary" data-confirm-approval="true"
            disabled={!riskAcknowledged || writesBlocked || busy !== null || capabilities?.case_approve !== true || caseFile.state !== "awaiting_approval" || caseFile.decision?.can_approve !== true || confirmedPlan !== plan?.plan_id}
            onClick={() => { setRiskOpen(false); void runApprove(); }}>{t(caseFile.decision?.authorization_pending ? "v10.continueApproval" : "decision.approve")}</button></>}>
        {plan && <><DecisionFacts plan={plan} /><OrderReadiness plan={plan} />
          <PolicyChecks checks={effectiveChecks(caseFile, t).checks} derived={effectiveChecks(caseFile, t).derived} unknownChecks={caseFile.decision?.unknown_checks} /></>}
        <h3>{t(caseFile.decision?.authorization_pending ? "decisionFacts.choice" : "decisionFacts.choiceDraft")}</h3>
        <p data-approval-reason="true">{(caseFile.decision?.authorization_pending ? caseFile.approval?.comment : comment)?.trim() || t("decisionFacts.choiceEmpty")}</p>
        <label className="approval-acknowledgement"><input type="checkbox"
          checked={riskAcknowledged} onChange={event => setRiskAcknowledged(event.target.checked)} />{t("v9.acknowledge")}</label>
      </Modal>

      {decisionDialog && <Modal open closeLabel={t("v9.cancel")} title={t(decisionDialog === "reject" ? "v9.rejectTitle" : decisionDialog === "reprepare" ? "v10.reprepare" : "v9.reviseTitle")} onClose={() => { if (!decisionBusy) setDecisionDialog(null); }}>
        {decisionDialog === "revision" && <div className="field">
          {caseFile.revision_limits && <p data-original-margin="true">{t("v10.originalMargin", { floor: caseFile.revision_limits.original_margin_pct })}</p>}
          <p data-effective-margin="true">{revisionFloor(caseFile) === null ? t("v10.marginUnknown") : t("v10.effectiveMargin", { floor: revisionFloor(caseFile)! })}</p>
          <label htmlFor="revision-margin">{t("v9.revisionMargin")}</label>
          <input id="revision-margin" type="number" min={suggestedRevisionMargin(caseFile)} max={99.99} step="0.01" value={revisionMargin} disabled={decisionBusy} onChange={event => setRevisionMargin(Number(event.target.value))} />
          <p>{t("v10.marginEffect")}</p><p>{t("v10.invalidMargin")}</p>
        </div>}
        {decisionDialog === "reprepare" && <div data-reprepare-summary="true"><p>{t("v10.reprepareBody")}</p>
          <p>{t("v10.reprepareConstraints", { version: (caseFile.revision?.version ?? 1) + 1 })}</p>
          {caseFile.revision_limits && <><p>{t("v10.originalMargin", { floor: caseFile.revision_limits.original_margin_pct })}</p><p>{t("v10.effectiveMargin", { floor: caseFile.revision_limits.effective_margin_pct })}</p></>}
        </div>}
        <label htmlFor="decision-reason">{t("v9.reason")}</label>
        <textarea id="decision-reason" rows={4} maxLength={2000} value={decisionReason} onChange={event => setDecisionReason(event.target.value)} disabled={decisionBusy} />
        {error && <p role="alert" className="error">{error}</p>}
        <button type="button" className="primary" disabled={writesBlocked || decisionBusy || busy !== null || !dialogTargetMatches || !dialogPermitted || !decisionReason.trim()} onClick={() => void submitDecision()}>{t(decisionBusy ? "v10.busyPreparing" : decisionDialog === "reprepare" ? "v10.reprepareSubmit" : "v9.submit")}</button>
      </Modal>}
      {caseFile.decision?.can_reconcile && mode !== "fixture" && <button className="secondary" disabled={writesBlocked || decisionBusy} onClick={() => void reconcile()}>{t("v9.reconcile")}</button>}
      {caseFile.decision?.can_cancel_unstarted && mode === "live" && <button type="button" className="secondary" data-cancel-unstarted="true" disabled={writesBlocked || decisionBusy || busy !== null || capabilities?.case_approve !== true} onClick={() => { setError(null); setCancelUnstartedOpen(true); }}>{t("v10.cancelUnstarted")}</button>}
      <Modal open={cancelUnstartedOpen} closeLabel={t("v9.cancel")} title={t("v10.cancelUnstarted")} onClose={() => { if (!decisionBusy) setCancelUnstartedOpen(false); }}>
        <p>{t("v10.cancelUnstartedBody")}</p>
        {error && <p role="alert" className="error">{error}</p>}
        <button type="button" className="primary" disabled={writesBlocked || decisionBusy || busy !== null || capabilities?.case_approve !== true || !caseFile.decision?.can_cancel_unstarted} onClick={() => void cancelUnstarted()}>{t("v10.cancelUnstartedConfirm")}</button>
      </Modal>
      {(caseFile.audit?.length || caseFile.history?.length) ? <details className="case-audit"><summary>{t("v9.audit")}</summary><pre>{JSON.stringify({ revision: caseFile.revision, audit: caseFile.audit, history: caseFile.history }, null, 2)}</pre></details> : null}


      {plan && mode === "live" && caseFile.state === "awaiting_approval" ? (
        <div
          className={`case-sticky-decision${stickyGate.canApprove ? "" : " is-blocked"}`}
          data-case-sticky-decision="true"
          data-sticky-blocked={stickyGate.canApprove ? undefined : "true"}
        >
          <div>
            <strong>{t(stickyGate.canApprove ? "v11.stickyTitle" : "v13.stickyBlockedTitle")}</strong>
            <span>{t("v11.stickyScope", { documents: uniqueActionDocuments(caseFile), units: formatQuantity(caseFile.order.units) })}</span>
            {!stickyGate.canApprove && stickyGate.block ? <span className="sticky-block-reason" data-sticky-block-reason="true">{stickyGate.block.reason}</span> : null}
            {!stickyGate.canApprove && stickyGate.block?.recovery ? <span className="sticky-block-recovery" data-sticky-block-recovery="true">{stickyGate.block.recovery}</span> : null}
            {capabilities?.environment?.execution === "synthetic_executor" ? <span>{t("v11.syntheticExecution")}</span> : null}
          </div>
          <button type="button" className="primary" data-case-approve-sticky="true"
            disabled={!stickyGate.canApprove || writesBlocked || decisionBusy || busy !== null}
            title={stickyGate.canApprove ? undefined : stickyGate.block?.reason}
            onClick={() => { setConfirmedPlan(plan.plan_id); setRiskAcknowledged(false); setRiskOpen(true); }}>
            {t("decision.approve")}
          </button>
        </div>
      ) : null}
    </article>
  );
}
