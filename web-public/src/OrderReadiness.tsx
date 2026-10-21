import { useI18n } from "./i18n";
import type { ProductPlan } from "./types";

export function OrderReadiness({ plan }: { plan: ProductPlan }) {
  const { t } = useI18n();
  const data = plan.order_readiness;
  return <section className="case-section" data-order-readiness={plan.plan_id}>
    <h2>{t("readiness.title")}</h2>
    {!data ? <p className="decision-note">{t("readiness.unknown")}</p> : <>
      <p>{t("readiness.basis", { date: data.basis.anchor_date })}</p>
      <p className="decision-note">{t("readiness.note")}</p>
      <div className="table-wrap" tabIndex={0} role="region" aria-label={t("readiness.title")}>
        <table>
          <thead><tr>
            <th scope="col">{t("readiness.order")}</th><th scope="col">{t("readiness.coverage")}</th>
            <th scope="col">{t("readiness.ready")}</th><th scope="col">{t("readiness.due")}</th>
            <th scope="col">{t("readiness.buffer")}</th><th scope="col">{t("readiness.supply")}</th>
          </tr></thead>
          <tbody>{data.orders.map(row => <tr key={row.order_ref}>
            <th scope="row">{row.order_ref}</th>
            <td>{row.covered_units} / {row.demand_units}<p className="decision-note">{t(row.status === "incomplete" ? "readiness.incomplete"
              : row.status === "late" ? "readiness.late" : row.status === "on_time" ? "readiness.planned" : "v9.unknown")}</p></td>
            <td>{row.planned_ready_date ?? t("v9.unknown")}</td><td>{row.customer_due_date}</td>
            <td>{row.buffer_days == null ? t("v9.unknown") : t(Math.abs(row.buffer_days) === 1 ? "readiness.day" : "readiness.days", { count: row.buffer_days })}
              {row.buffer_days === 0 && <p className="decision-note">{t("readiness.noBuffer")}</p>}</td>
            <td>{t("readiness.sources", { stock: row.stock_units, manufacturing: row.manufactured_units, purchasing: row.purchased_units })}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </>}
  </section>;
}
