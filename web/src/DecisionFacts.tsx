import { useI18n } from "./i18n";
import { costMoney } from "./costAmounts";
import { decisionFactLines } from "./decisionSummary";
import type { CaseFile, ProductPlan } from "./types";

export function DecisionFacts({ plan }: { plan: ProductPlan }) {
  const { t, lang } = useI18n();
  const data = plan.decision_metrics;
  const unknown = t("v9.unknown");
  const money = (value: string) => costMoney(value, lang, unknown);
  return <section className="case-section decision-facts" data-decision-facts={plan.plan_id}>
    <h3>{t("decisionFacts.title")}</h3>
    {decisionFactLines(plan, t, lang).map((line, index) => <p key={index}>{line}</p>)}
    {data && <>
      <p className="decision-note">{t("decisionFacts.note")}</p>
      <details>
        <summary>{t("decisionFacts.more")}</summary>
        <p className="decision-note">{t("decisionFacts.capacityNote")}</p>
        {data.workcenters.length ? <div className="table-wrap" tabIndex={0} role="region" aria-label={t("decisionFacts.center")}>
          <table><thead><tr><th scope="col">{t("decisionFacts.center")}</th><th scope="col">{t("decisionFacts.used")}</th>
            <th scope="col">{t("decisionFacts.limit")}</th><th scope="col">{t("decisionFacts.remaining")}</th></tr></thead>
            <tbody>{data.workcenters.map(row => <tr key={row.code}><th scope="row">{row.code}</th>
              <td>{row.used_standard_minutes ?? unknown}</td><td>{row.horizon_limit_minutes ?? unknown}</td>
              <td>{row.headroom_minutes ?? unknown}</td></tr>)}</tbody></table>
        </div> : <p>{t("decisionFacts.noManufacturing")}</p>}
        <p className="decision-note">{t("decisionFacts.budgetNote")}</p>
        <div className="table-wrap" tabIndex={0} role="region" aria-label={t("decisionFacts.customerCap")}>
          <table><thead><tr><th scope="col">{t("readiness.order")}</th><th scope="col">{t("decisionFacts.salesValue")}</th>
            <th scope="col">{t("decisionFacts.customerCap")}</th><th scope="col">{t("decisionFacts.capHeadroom")}</th></tr></thead>
            <tbody>{data.order_budgets.map(row => <tr key={row.order_ref}><th scope="row">{row.order_ref}</th>
              <td>{money(row.sales_value)}</td><td>{money(row.customer_cap)}</td><td>{money(row.headroom)}</td></tr>)}</tbody></table>
        </div>
      </details>
    </>}
  </section>;
}

export function ChoiceReason({ caseFile }: { caseFile: CaseFile }) {
  const { t } = useI18n();
  const approval = caseFile.approval;
  const reason = approval?.plan_id === caseFile.plan?.plan_id ? approval?.comment?.trim() : null;
  return <section className="case-section" data-choice-reason={caseFile.plan?.plan_id}>
    <h3>{t("decisionFacts.choice")}</h3>
    <p>{reason || t("decisionFacts.choiceUnknown")}</p>
  </section>;
}
