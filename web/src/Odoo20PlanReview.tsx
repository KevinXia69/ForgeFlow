import { useI18n, type MessageKey } from "./i18n";
import { snapshotComplete, type PlanAction, type PreparedPlan } from "./planReviewIdentity";

const fieldLabels: Record<string, MessageKey> = {
  product_qty: "case.colQty", quantity: "case.colQty", price_unit: "case.diffUnitPrice",
  unit_price: "case.diffUnitPrice", date_planned: "case.diffPlannedDate", date_start: "case.diffStartDate",
  date_finished: "case.diffFinishDate", state: "cases.colState", product_id: "case.diffProduct",
  product_code: "case.diffProduct", partner_id: "case.factVendor", supplier_ref: "case.factVendor",
  assumptions: "odoo20.reviewAssumptions", binding_constraints: "odoo20.reviewConstraints",
  currency: "odoo20.reviewCurrency", unit: "odoo20.reviewUnit", lines: "odoo20.reviewLines",
};

/** Render the complete server snapshot, including nested fields not known by this client. */
function ReviewValue({ value }: { value: unknown }) {
  const { t } = useI18n();
  if (value === null || value === undefined || value === "") return <span>{t("v9.unknown")}</span>;
  if (Array.isArray(value)) return value.length ? <ol>{value.map((item, index) => <li key={index}><ReviewValue value={item} /></li>)}</ol> : <span>{t("odoo20.reviewNoneRecorded")}</span>;
  if (typeof value === "object") return <dl className="change-diff-fields">{Object.entries(value).map(([key, item]) =>
    <div key={key}><dt>{fieldLabels[key] ? t(fieldLabels[key]) : key}</dt><dd><ReviewValue value={item} /></dd></div>)}</dl>;
  return <span>{String(value)}</span>;
}
function named(value: unknown): unknown { return Array.isArray(value) && value.length === 2 ? value[1] : value; }
function amount(action: PlanAction): number | null {
  const lines = action.params?.lines;
  if (!Array.isArray(lines) || !lines.length) return null;
  let total = 0;
  for (const line of lines) {
    if (!line || typeof line.product_qty !== "number" || typeof line.price_unit !== "number") return null;
    total += line.product_qty * line.price_unit;
  }
  return Number.isFinite(total) ? total : null;
}
function reviewNotes(value: unknown, prefix = ""): Record<string, unknown> {
  if (!value || typeof value !== "object") return {};
  const notes: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix} · ${key}` : key;
    if (["assumptions", "binding_constraints", "unknowns", "missing_facts", "warnings", "unmet"].includes(key)) notes[path] = item;
    else if (item && typeof item === "object") Object.assign(notes, reviewNotes(item, path));
  }
  return notes;
}
export function Odoo20PlanReview({ plan }: { plan: PreparedPlan }) {
  const { t } = useI18n();
  const actions = Array.isArray(plan.actions) ? plan.actions.filter(action => action && typeof action === "object") : [];
  const lines = Array.isArray(plan.lines) ? plan.lines.filter(line => line && typeof line === "object") : [];
  const na = t("odoo20.reviewNotApplicable");
  const unknown = t("v9.unknown");
  const notes = reviewNotes(plan.facts);
  return <section className="odoo20-review" data-plan-review>
    <h3>{t("odoo20.reviewTitle")}</h3>
    <p data-review-version>{t("v9.planVersion", { version: plan.plan_version ?? unknown })} · {t("odoo20.changeSummary", { count: actions.length })}</p>
    <div className="change-table-wrap"><table className="change-table" data-plan-action-table>
      <thead><tr><th>{t("case.colDocument")}</th><th>{t("case.colAction")}</th><th>{t("case.colQty")}</th><th>{t("odoo20.reviewAmount")}</th><th>{t("case.diffPlannedDate")}</th></tr></thead>
      <tbody>{actions.map((action, index) => {
        const cancel = String(action.type).startsWith("cancel_");
        const total = amount(action);
        return <tr key={action.action_id ?? index} data-review-action>
          <td>{action.target_model ?? unknown} · {action.target_id ? action.target_id : String(action.type).startsWith("create_") ? t("odoo20.reviewNewObject") : unknown}</td>
          <td>{action.summary || action.type || unknown}</td>
          <td>{action.quantity !== undefined ? `${action.quantity} ${action.unit || unknown}` : cancel ? na : unknown}</td>
          <td>{total !== null ? `${total.toFixed(2)} ${action.currency || unknown}` : cancel ? na : unknown}</td>
          <td><ReviewValue value={action.params?.date_planned ?? action.params?.date_start ?? (cancel ? na : undefined)} /></td>
        </tr>;
      })}</tbody>
    </table></div>
    <h3>{t("case.diffView")}</h3>
    <div className="change-diff" data-plan-action-diffs>{actions.map((action, index) =>
      <details key={action.action_id ?? index} data-review-action-diff>
        <summary>{index + 1}. {action.summary || action.type || unknown} · {action.action_id || unknown}</summary>
        <div className="change-diff-cols">
          <div className="change-diff-col is-before"><h4>{t("odoo20.reviewBefore")}</h4><ReviewValue value={action.preconditions} /></div>
          <div className="change-diff-col is-after"><h4>{t("case.after")}</h4><ReviewValue value={{ ...action.params, ...(action.quantity !== undefined ? { quantity: action.quantity, unit: action.unit } : {}), currency: action.currency }} /></div>
        </div>
        <details><summary>{t("odoo20.reviewActionDetails")}</summary><ReviewValue value={action} /></details>
      </details>)}</div>
    <h3>{t("odoo20.reviewOriginalLines", { count: lines.length })}</h3>
    {!snapshotComplete(plan.facts) && <p role="alert" data-review-completeness>{t("odoo20.reviewPartial")}</p>}
    <div className="change-table-wrap"><table className="change-table" data-plan-original-lines>
      <thead><tr><th>{t("case.diffProduct")}</th><th>{t("case.colQty")}</th><th>{t("case.diffUnitPrice")}</th><th>{t("odoo20.reviewCurrency")}</th><th>{t("case.diffPlannedDate")}</th></tr></thead>
      <tbody>{lines.map((line, index) => <tr key={String(line.id ?? index)} data-review-line>
        <td><ReviewValue value={named(line.product_id) ?? line.name} /></td><td><ReviewValue value={line.product_qty} /> <ReviewValue value={named(line.uom_id ?? line.product_uom_id ?? line.product_uom)} /></td>
        <td><ReviewValue value={line.price_unit} /></td><td><ReviewValue value={named(line.currency_id ?? plan.order?.currency_id)} /></td><td><ReviewValue value={line.date_planned} /></td>
      </tr>)}</tbody>
    </table></div>
    <h3>{t("odoo20.reviewAssumptions")}</h3>
    <p>{t("odoo20.reviewUnknownNote")}</p>
    <ReviewValue value={Object.keys(notes).length ? notes : undefined} />
    <details data-review-facts><summary>{t("odoo20.reviewFacts")}</summary><ReviewValue value={plan.facts} /></details>
  </section>;
}
