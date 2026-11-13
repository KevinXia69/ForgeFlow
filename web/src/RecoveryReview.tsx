import { useEffect, useRef, useState } from "react";
import { ApiError, api, getProductCase } from "./api";
import { useI18n } from "./i18n";
import type { CaseFile, RecoveryReview } from "./types";

type Snapshot = {
  plan_id: string; snapshot_sha256: string; captured_at: string;
  rows: { model: string; id: number; fields: Record<string, string | number | boolean> }[];
};
type Investigation = {
  operation_id: string; status: "running" | "completed" | "unknown" | "failed";
  text: string; answer?: string; observation?: { reference?: string }; detail?: string;
};
type Input = { operation_id: string; plan_id: string; expected_version: number; order_id: number };
type Draft = ({ route: "review"; kind: RecoveryReview["kind"]; note: string }
  | { route: "investigate"; text: string }) & Input;

export function RecoveryReviewPanel({ caseFile, disabled, onUpdated }: {
  caseFile: CaseFile; disabled: boolean; onUpdated: (next: CaseFile) => void;
}) {
  const { lang } = useI18n();
  const label = (zh: string, en: string) => lang === "zh" ? zh : en;
  const id = encodeURIComponent(caseFile.case_id);
  const base = `/api/product/cases/${id}`;
  const investigationBase = `/api/recovery-review/products/${id}/investigate`;
  const key = `recovery-review-${id}`;
  const orders = caseFile.handoff!.sales_order_ids;
  const [order, setOrder] = useState(orders[0]);
  const [kind, setKind] = useState<RecoveryReview["kind"]>("reason");
  const [note, setNote] = useState("");
  const [prompt, setPrompt] = useState("");
  const [source, setSource] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<Draft | null>(() => {
    try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch { return null; }
  });
  const [watch, setWatch] = useState(saved?.route === "investigate" ? saved.operation_id : sessionStorage.getItem(key + "-last") || "");
  const [receipt, setReceipt] = useState<Investigation | null>(null);
  const update = useRef(onUpdated);
  update.current = onUpdated;
  const selected = caseFile.plan;
  const plans = caseFile.comparison?.status === "ready" ? caseFile.comparison.plans : selected ? [selected] : [];
  const currentSource = plans.some(p => p.plan_id === source?.plan_id) ? source : null;
  const available = !disabled && !busy && !saved && !watch && caseFile.decision?.can_review === true && !!selected;
  const tradeoff = caseFile.review_comparison;
  const reviews = caseFile.reviews ?? [];
  const latest = reviews.at(-1);
  const pending = latest && ["held", "pending", "repreparing"].includes(latest.status) ? latest : null;
  const statusLabels: Record<string, [string, string]> = {
    noted: ["已记录理由", "Reason recorded"], pending: ["待核查 · 已暂停批准", "Pending · approval paused"],
    held: ["证据不足 · 暂缓", "Held for evidence"], repreparing: ["重新准备中", "Preparing a new version"],
    retained: ["原依据仍有效", "Original field confirmed"], corrected: ["已核实更正", "Correction verified"],
    failed: ["准备未完成", "Preparation failed"], rejected: ["案例已拒绝", "Case rejected"],
  };
  function remember(value: Draft | null) {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
    setSaved(value);
  }
  useEffect(() => {
    if (!watch) return;
    let cancelled = false;
    let timer: number | undefined;
    async function read() {
      try {
        const result = await api<Investigation>(`${investigationBase}/${encodeURIComponent(watch)}`);
        if (cancelled) return;
        setReceipt(result);
        const next = await getProductCase(caseFile.case_id);
        if (cancelled) return;
        update.current(next);
        if (result.status === "running") timer = window.setTimeout(read, 1500);
        else {
          if (result.status === "completed" || result.status === "failed") {
            sessionStorage.setItem(key + "-last", watch);
            remember(null);
          }
          setWatch("");
        }
      } catch (e) {
        if (!cancelled) { setError(String(e)); setWatch(""); }
      }
    }
    void read();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [watch, investigationBase, caseFile.case_id]);
  async function inspect(planId: string) {
    setBusy(true); setError(""); setSource(null);
    try { setSource(await api<Snapshot>(`${base}/review-sources/${planId}`)); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function submit(draft: Draft) {
    setError(""); setBusy(true);
    try {
      remember(draft);
      const { route, ...body } = draft;
      const options = { method: "POST", body: JSON.stringify(body) };
      if (route === "review") {
        update.current(await api<CaseFile>(`${base}/reviews`, options));
        setNote(""); remember(null);
      } else {
        setReceipt(await api<Investigation>(investigationBase, options));
        setWatch(draft.operation_id);
      }
    } catch (e) {
      setError(String(e));
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) remember(null);
    } finally { setBusy(false); }
  }
  function start(route: Draft["route"]) {
    if (!available || !selected || !caseFile.revision) return;
    const input = { operation_id: crypto.randomUUID(), plan_id: selected.plan_id,
      expected_version: caseFile.revision.version, order_id: order };
    void submit(route === "review" ? { ...input, route, kind, note } : { ...input, route, text: prompt });
  }
  async function check(operation: string) {
    setError(""); setBusy(true);
    try { update.current(await api<CaseFile>(`${base}/reviews/${encodeURIComponent(operation)}/check`, { method: "POST" })); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <section className="case-section" data-recovery-review="true">
    <h2>{label("核查方案依据", "Review the decision")}</h2>
    <p>{label("查看方案对应的原始业务行、数量与计算时间，核对每项选择的依据。", "Inspect the original business rows, quantities, and calculation time behind each plan to review the basis for each choice.")}</p>
    {tradeoff && <div data-review-tradeoff="true">
      <p>{label("保留数量方案相对低成本方案的计划成本差额", "Planned cost of preserving quantities minus the low-cost plan")} · <strong>{tradeoff.preserve_minus_low_cost ?? label("未测得", "Unknown")} {tradeoff.currency}</strong></p>
      {tradeoff.differences.map(row => <p key={[row.kind, row.document, row.product_code].join(".")} data-review-difference={row.document}>
        <strong>{row.document} · {row.product_code}</strong> · {label("原数量", "Original")} {row.baseline} {row.unit} · {label("保留方案", "Preserving quantities")} {row.preserve.action} {row.preserve.quantity} · {label("低成本方案", "Low cost")} {row.low_cost.action} {row.low_cost.quantity}
      </p>)}</div>}
    {plans.map(p => <button className="secondary" key={p.plan_id} data-review-source={p.plan_id}
      disabled={busy} onClick={() => void inspect(p.plan_id)}>{label("查看方案来源", "Inspect sources")} · {p.plan_id.slice(0, 10)}</button>)}
    {currentSource && <div data-review-snapshot={currentSource.snapshot_sha256}>
      <p>{label("快照", "Snapshot")} {currentSource.captured_at} · {currentSource.snapshot_sha256.slice(0, 12)}</p>
      <details open><summary>{label("业务行与字段", "Business rows and fields")} ({currentSource.rows.length})</summary>
        <div className="review-source-list">{currentSource.rows.map(row => <p key={[row.model, row.id, row.fields.product_code].join(".")} data-review-row={row.model + ":" + row.id}>
          <strong>{row.model} #{row.id}</strong> · {Object.entries(row.fields).map(([k, v]) => k + "=" + String(v)).join(" · ")}</p>)}</div>
      </details></div>}
    {selected && <fieldset disabled={disabled || busy} className="review-form">
      <legend>{label("当前方案", "Selected plan")} · v{caseFile.revision?.version} · {selected.plan_id.slice(0, 10)}</legend>
      <label>{label("受影响销售订单", "Affected sales order")}
        <select data-review-order value={order} onChange={e => setOrder(Number(e.target.value))}>{orders.map(n => <option key={n} value={n}>sale.order #{n}</option>)}</select></label>
      <label>{label("评审动作", "Review action")} <select data-review-kind value={kind} onChange={e => setKind(e.target.value as RecoveryReview["kind"])}>
        <option value="reason">{label("记录或修订当前选择的理由", "Record or revise the reason for this selection")}</option>
        <option value="deadline">{label("质疑销售订单承诺交期", "Challenge the committed delivery date")}</option>
        <option value="unmodeled">{label("提交缺少证据的现场因素并暂缓", "Hold for an unverified external factor")}</option>
      </select></label>
      <label>{label("理由或待查线索", "Reason or evidence to check")} <textarea data-review-note maxLength={2000} value={note} onChange={e => setNote(e.target.value)} /></label>
      <button className="secondary" data-review-open disabled={!available || !note.trim()} onClick={() => start("review")}>{label("提交评审记录", "Submit review")}</button>
      <label>{label("请调查 Agent 核查当前交期", "Ask the investigation agent to check the current deadline")} <textarea data-review-prompt maxLength={2000} value={prompt} onChange={e => setPrompt(e.target.value)} /></label>
      <button className="secondary" data-review-investigate disabled={!available || !prompt.trim()} onClick={() => start("investigate")}>{label("发起实际调查", "Investigate")}</button>
    </fieldset>}
    {saved && !watch && <button className="secondary" disabled={disabled || busy} onClick={() => void submit(saved)}>{label("恢复同一操作", "Resume same operation")} · {saved.operation_id.slice(0, 8)}</button>}
    {receipt && <p role="status" data-review-investigation-status={receipt.status}>{receipt.status} · {receipt.text} {receipt.detail} {receipt.observation?.reference} {receipt.answer}</p>}
    {reviews.map(r => <article key={r.operation_id} data-review-disposition={r.status}>
      <p><strong>{label(...(statusLabels[r.status] ?? [r.status, r.status] as [string,string]))}</strong> · v{r.version} · {r.plan_id.slice(0, 10)} · {r.operation_id}</p>
      <p>{r.note}</p>
      {r.supersedes && <p>{label("修订先前理由", "Revises reason")} · {r.supersedes}</p>}
      {r.evidence && <p>{r.evidence.model} #{r.evidence.id} · {r.evidence.field} · {r.evidence.prepared_value} → {r.evidence.current_value} · write_date {r.evidence.current_write_date}</p>}
      {r.result_version && <p>{label("后续版本", "Resulting version")} v{r.result_version} · {r.preparation_status}</p>}
      {(r.error || r.resolution) && <p>{r.error || r.resolution}</p>}
    </article>)}
    {pending?.kind === "deadline" && <button className="secondary" data-review-check disabled={disabled || busy || !!watch} onClick={() => void check(pending.operation_id)}>{label("读取登记来源并处置", "Check registered source and resolve")}</button>}
    {error && <p role="alert" className="case-error">{error}</p>}
  </section>;
}
