import type { ReactNode } from "react";
import type { CaseFile, ProductPlan } from "./types";
import { useI18n, type Translate } from "./i18n";
import { formatPercent, formatDateTime, shortId } from "./caseUi";
import { costMoney } from "./costAmounts";
import { CostBreakdown } from "./CostBreakdown";
import { DecisionFacts } from "./DecisionFacts";
import { OrderReadiness } from "./OrderReadiness";
import { ImpactChain } from "./ImpactChain";

export function candidateName(plan: Pick<ProductPlan, "objective">, t: Translate): string {
  return t(plan.objective === "preserve_quantities" ? "candidate.quantity" : plan.objective === "min_planned_cost"
    ? "candidate.cost" : plan.objective === "preserve_capacity" ? "candidate.capacity" : "candidate.plan");
}

export function CandidateComparison({ caseFile, disabled, readOnly = false, onSelect, onResume, onReject, renderPlan }: {
  caseFile: CaseFile; disabled: boolean; readOnly?: boolean;
  onSelect: (planId: string) => void; onResume: () => void; onReject: () => void;
  renderPlan: (plan: ProductPlan) => ReactNode;
}) {
  const { t, lang } = useI18n();
  const comparison = caseFile.comparison;
  if (!comparison) return null;
  if (comparison.status !== "ready") return <section className="case-section" data-candidate-comparison="unavailable">
    <h2>{t("candidate.title")}</h2><p role="status">{t("candidate.unavailable")}</p>
  </section>;
  const pending = caseFile.decision?.selection_pending === true;
  const mutable = !readOnly && !caseFile.approval && !caseFile.execution
    && ["awaiting_selection", "awaiting_approval"].includes(caseFile.state);
  return <section className="case-section candidate-comparison" data-candidate-comparison="ready" aria-busy={disabled && mutable}>
    <div className="case-section-head"><h2>{t("candidate.title")}</h2><span>{t("v9.planVersion", { version: comparison.version })}</span></div>
    <p>{t("candidate.basis", { count: comparison.plans.length })}</p>
    <p className="decision-note">{t("candidate.preferenceNote")}</p>
    {pending && <div className="candidate-notice" data-selection-pending="true" role="status">
      <h3>{t("candidate.pendingTitle")}</h3><p>{t("candidate.pendingBody")}</p>
      {caseFile.selection_request && <p>{t("candidate.requested", { name: candidateName(comparison.plans.find(plan => plan.plan_id === caseFile.selection_request?.plan_id) ?? {}, t) })}</p>}
      {!readOnly && <button className="secondary" type="button" disabled={disabled || !caseFile.decision?.can_resume_selection}
        onClick={onResume} data-resume-selection="true">{t("candidate.resume")}</button>}
    </div>}
    <div className="candidate-cards">
      {comparison.plans.map(plan => {
        const selected = comparison.selected_plan_id === plan.plan_id;
        const money = (value: string | null | undefined) => costMoney(value, lang, t("v9.unknown"));
        return <article key={plan.plan_id} className={`candidate-card${selected ? " is-selected" : ""}`}
          data-candidate={plan.plan_id} data-candidate-selected={selected ? "true" : "false"}>
          <h3>{candidateName(plan, t)}</h3>
          {selected && <p className="candidate-selected" role="status">{t(pending ? "candidate.lastConfirmed" : "candidate.selected")}</p>}
          <p>{t(plan.objective === "preserve_quantities" ? "candidate.quantityBody" : plan.objective === "min_planned_cost" ? "candidate.costBody" : plan.objective === "preserve_capacity" ? "candidate.capacityBody" : "candidate.preferenceNote")}</p>
          <dl className="candidate-facts">
            <div><dt>{t("candidate.manufactured")}</dt><dd>{plan.capacity?.manufactured_units ?? t("v9.unknown")}</dd></div>
            <div><dt>{t("candidate.finishedPurchase")}</dt><dd>{plan.capacity?.finished_purchase_units ?? t("v9.unknown")}</dd></div>
            <div><dt>{t("candidate.standardMinutes")}</dt><dd data-standard-minutes={plan.plan_id}>{plan.capacity?.standard_minutes == null ? t("v9.unknown") : new Intl.NumberFormat(lang, { maximumFractionDigits: 2 }).format(plan.capacity.standard_minutes)}</dd></div>
            <div><dt>{t("decision.newSpend")}</dt><dd>{money(plan.costs?.planned.total)}</dd></div>
            <div><dt>{t("cost.delta")}</dt><dd>{money(plan.costs?.known_delta)}</dd></div>
            <div><dt>{t("decision.margin")}</dt><dd>{formatPercent(plan.margin)}</dd></div>
            <div><dt>{t("candidate.changes")}</dt><dd>{plan.actions.length}</dd></div>
            <div><dt>{t("decision.due")}</dt><dd>{t(plan.on_time === true ? "case.onTimePlanned" : plan.on_time === false ? "v9.onTimeFailed" : "v9.unknown")}</dd></div>
          </dl>
          <DecisionFacts plan={plan} />
          <p className="decision-note">{t("candidate.capacityNote")}</p>
          <p className="decision-note">{t("cost.deltaNote")}</p>
          {mutable && comparison.plans.length > 1 && <button type="button" className={selected ? "secondary" : "primary"}
            disabled={disabled || pending || selected || caseFile.decision?.can_select_candidate !== true}
            onClick={() => onSelect(plan.plan_id)} data-select-candidate={plan.plan_id}
            aria-label={t("candidate.chooseNamed", { name: candidateName(plan, t) })}>
            {t(selected ? "candidate.selected" : "candidate.choose")}
          </button>}
          <details data-candidate-details={plan.plan_id}>
            <summary>{t("candidate.details", { name: candidateName(plan, t) })}</summary>
            {renderPlan(plan)}
            <CostBreakdown costs={plan.costs} actions={plan.actions} />
            <OrderReadiness plan={plan} />
            <ImpactChain plan={plan} selectionKey={comparison.selected_plan_id ?? "unselected"} />
            <p className="decision-note">{t("case.evidencePrepared")}: {formatDateTime(plan.prepared_at, lang)}</p>
            <p className="decision-note mono">{t("case.evidencePlanId")}: {shortId(plan.plan_id, 16)}</p>
          </details>
        </article>;
      })}
    </div>
    {comparison.attempts.filter(attempt => attempt.status !== "prepared").map(attempt => <p key={attempt.objective} data-candidate-attempt={attempt.status}>
      {candidateName(attempt, t)}: {t(attempt.status === "duplicate" ? "candidate.duplicate" : attempt.status === "infeasible" ? "candidate.infeasible" : "candidate.rejected")}
    </p>)}
    <p>{t(!mutable ? "candidate.recordedNote" : comparison.plans.length === 1 ? "candidate.single" : "candidate.selectNote")}</p>
    {mutable && !caseFile.plan && <button type="button" className="secondary decision-reject" data-reject-candidates="true"
      disabled={disabled || pending || !caseFile.decision?.can_reject} onClick={onReject}>{t("candidate.rejectAll")}</button>}
  </section>;
}
