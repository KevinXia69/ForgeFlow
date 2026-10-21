import { useI18n, type Translate } from "./i18n";
import { formatDateTime, friendlyDocument, shortId } from "./caseUi";
import type { CostValue, ProductAction, ProductCosts } from "./types";
import { costMoney } from "./costAmounts";

function Amount({ value, t, lang }: { value: CostValue | null; t: Translate; lang: string }) {
  if (!value) return <span>{t("cost.absent")}</span>;
  return <div>
    <strong>{costMoney(value.amount, lang, t("v9.unknown"))}</strong>
    <p className="decision-note">{value.quantity} {value.uom_label ?? t("cost.unit", { id: value.uom_id })} × {value.unit_price === null ? t("v9.unknown") : `USD ${value.unit_price}`}</p>
    <p className="decision-note">{t(value.price_source === "purchase_unit_price" ? "cost.poPrice" : "cost.moStandard")}</p>
    {value.price_source === "public_workcenter_standard" && value.price_reference && <p className="decision-note">{value.price_reference}</p>}
    {value.missing && <p className="decision-note">{t("cost.missingPrice")}</p>}
  </div>;
}

export function CostBreakdown({ costs, actions }: { costs: ProductCosts | null | undefined; actions: readonly ProductAction[] }) {
  const { t, lang } = useI18n();
  const money = (value: string | null | undefined) => costMoney(value, lang, t("v9.unknown"));
  return <section className="case-section cost-breakdown" data-cost-breakdown="true">
    <h2>{t("cost.title")}</h2>
    {!costs ? <p className="decision-note">{t("cost.unrecorded")}</p> : <>
      <p>{t("cost.scope", { product: costs.basis.target_product_code, orders: costs.basis.sales_order_ids.length, time: formatDateTime(costs.basis.as_of, lang) })}</p>
      <div className="cost-kpis">
        <div className="kpi"><span className="kpi-label">{t("cost.baseline")}</span><strong className="kpi-value">{money(costs.baseline.total)}</strong><span className="kpi-note">{t("cost.originalNote")}</span></div>
        <div className="kpi"><span className="kpi-label">{t("decision.newSpend")}</span><strong className="kpi-value">{money(costs.planned.total)}</strong><span className="kpi-note">{t("decision.newSpendNote")}</span></div>
        <div className="kpi"><span className="kpi-label">{t("cost.delta")}</span><strong className="kpi-value">{money(costs.known_delta)}</strong><span className="kpi-note">{t("cost.deltaNote")}</span></div>
      </div>
      {(costs.baseline.total === null || costs.planned.total === null) && <p>{t("cost.partial", { before: money(costs.baseline.known_subtotal), after: money(costs.planned.known_subtotal) })}</p>}
      <p>{t("cost.additions", { created: money(costs.new_purchase_value), increased: money(costs.purchase_increase), manufacturing: money(costs.new_manufacturing_standard) })}</p>
      <p>{t(costs.missing.includes("cancellation_fees") ? "cost.unknownFees" : "cost.unknownFinancials")}</p>
      {!costs.comparable && <p>{t("cost.notComparable")}</p>}
      <details>
        <summary>{t("cost.details", { count: costs.lines.length })}</summary>
        <p className="decision-note">{t("cost.basis")}</p>
        <p className="decision-note">{t("cost.commitmentNote")}</p>
        <div className="table-wrap" tabIndex={0} role="region" aria-label={t("cost.detailsLabel")}>
          <table>
            <caption>{t("cost.rounding")}</caption>
            <thead><tr><th scope="col">{t("cost.document")}</th><th scope="col">{t("cost.baseline")}</th><th scope="col">{t("cost.proposed")}</th><th scope="col">{t("cost.delta")}</th></tr></thead>
            <tbody>{costs.lines.map((line, index) => <tr key={index}>
              <th scope="row">{friendlyDocument(line.document, t, actions)}<p>{t(`cost.${line.action}`)}</p><p className="decision-note">{line.product_code}</p></th>
              <td><Amount value={line.before} t={t} lang={lang} /></td>
              <td><Amount value={line.after} t={t} lang={lang} /></td>
              <td>{money(line.known_delta)}{line.action === "cancel" && <p className="decision-note">{t("cost.feeUnknown")}</p>}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <p className="decision-note">{t("cost.subtotals", { beforePo: money(costs.baseline.purchase), beforeMo: money(costs.baseline.manufacturing), afterPo: money(costs.planned.purchase), afterMo: money(costs.planned.manufacturing) })}</p>
        <p className="decision-note">{t("cost.binding", { snapshot: shortId(costs.basis.snapshot_sha256, 12), pricing: shortId(costs.basis.pricing_sha256, 12) })}</p>
      </details>
    </>}
  </section>;
}
