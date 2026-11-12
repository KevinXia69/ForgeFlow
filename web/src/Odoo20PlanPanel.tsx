import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { useI18n } from "./i18n";
import { useWorkspace } from "./workspace";
import { attentionNeedKey, taskStateKey, taskStateTone } from "./taskState";

type Prepared = {
  plan_id: number;
  order: { name: string; state: string; amount_total?: number; date_planned?: string };
  task?: { attention: string; need: string };
  action_count?: number;
  actions?: Array<{ type?: string; quantity?: number; unit?: string; currency?: string;
                    params?: Record<string, unknown> }>;
  lines?: Array<{ product_id?: [number, string]; product_qty?: number;
                  price_unit?: number; date_planned?: string }>;
};
type PlanTask = {
  plan_id: number; ticket_id?: string; goal: string; attention: string;
  attention_id?: string; need: string; blocking?: boolean; state: string;
  user_state?: string;
};
type FailureTask = { ticket_id: string; goal: string; question: string; attention_id?: string; need?: string };
type Rule = { name: string; version: number; source_ref: string | null };
type TaskInfo = { task_id: string; goal: string; state: string; user_state?: string; spent_tokens?: number; spent_effects?: number };
type AttentionInfo = { attention_id: string; ticket_id: string; need: string; question: string;
  goal: string; blocking?: boolean; plan_id?: number | null };

export function Odoo20PlanPanel({ initialPlanId = null }: { initialPlanId?: number | null }) {
  const { t } = useI18n();
  const { deployment } = useWorkspace();
  const [orderId, setOrderId] = useState("");
  const [reason, setReason] = useState("");
  const [plan, setPlan] = useState<Prepared | null>(null);
  const [approved, setApproved] = useState(false);
  const [busy, setBusy] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [tasks, setTasks] = useState<PlanTask[]>([]);
  const [failures, setFailures] = useState<FailureTask[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [openTasks, setOpenTasks] = useState<TaskInfo[]>([]);
  const [attentions, setAttentions] = useState<AttentionInfo[]>([]);
  const [tasksUnavailable, setTasksUnavailable] = useState(false);
  const [attentionDrafts, setAttentionDrafts] = useState<Record<string, string>>({});
  // One row per matter: a plan task and the same task in the queue are the same
  // thing, and showing both made the layout repeat itself.
  const matters = (() => {
    const seen = new Set<string>();
    const rows: { key: string; goal: string; state: string; user_state?: string;
                  need: string; attentionOpen: boolean }[] = [];
    for (const task of tasks) {
      const key = task.ticket_id ?? `plan:${task.plan_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ key, goal: task.goal, state: task.state, user_state: task.user_state,
                  need: task.need, attentionOpen: task.attention === "open" });
    }
    for (const task of openTasks) {
      if (seen.has(task.task_id)) continue;
      seen.add(task.task_id);
      rows.push({ key: task.task_id, goal: task.goal, state: task.state,
                  user_state: task.user_state, need: "fact", attentionOpen: false });
    }
    return rows.slice(0, 10);
  })();
  const scanSources = async () => {
    setBusy("prepare");
    setError("");
    try {
      await api("/api/sources/subscriptions", {
        method: "POST",
        body: JSON.stringify({ source_type: "odoo20.purchase_order", interval_seconds: 60 }),
      });
      const result = await api<{ scanned: { events: number; tickets: number }[] }>("/api/sources/scan", { method: "POST" });
      const events = result.scanned.reduce((total, row) => total + row.events, 0);
      setStatus(t("odoo20.scanned", { count: events }));
      refreshTasks();
    } catch {
      setError(t("odoo20.failed"));
    } finally {
      setBusy("");
    }
  };
  const refreshTasks = () => {
    setTasksUnavailable(false);
    void api<PlanTask[]>("/api/odoo20/tasks").then(setTasks).catch(() => {
      setTasks([]); setTasksUnavailable(true);
    });
    void api<FailureTask[]>("/api/tasks/failures").then(setFailures).catch(() => {
      setFailures([]); setTasksUnavailable(true);
    });
    void api<Rule[]>("/api/rules").then(setRules).catch(() => {
      setRules([]); setTasksUnavailable(true);
    });
    void api<TaskInfo[]>("/api/tasks").then(setOpenTasks).catch(() => {
      setOpenTasks([]); setTasksUnavailable(true);
    });
    void api<AttentionInfo[]>("/api/tasks/attentions").then(setAttentions).catch(() => {
      setAttentions([]); setTasksUnavailable(true);
    });
  };
  useEffect(() => { if (deployment !== "review") refreshTasks(); }, [deployment]);
  useEffect(() => {
    if (deployment === "review" || !initialPlanId) return;
    void api<Prepared>(`/api/odoo20/plans/${initialPlanId}`)
      .then((value) => { setPlan(value); setOrderId(String(value.order?.name ?? "")); })
      .catch(() => setError(t("odoo20.failed")));
  }, [deployment, initialPlanId, t]);
  // Approval queues a durable execution effect; the supervisor advances it.  The
  // page polls the same matter instead of asking the person to press "apply".
  useEffect(() => {
    if (deployment === "review" || !approved || !plan) return;
    const timer = setInterval(() => {
      void api<PlanTask[]>("/api/odoo20/tasks").then((rows) => {
        const row = rows.find((item) => item.plan_id === plan.plan_id);
        if (!row) return;
        if (row.state === "completed") {
          setStatus(t("odoo20.verified"));
          setPlan(null);
          setApproved(false);
        } else if (row.state === "blocked" || row.state === "failed") {
          setStatus(t("odoo20.executionNeedsAction"));
        }
      }).catch(() => undefined);
    }, 2500);
    return () => clearInterval(timer);
  }, [approved, plan, t, deployment]);
  // The public review surface remains read-only even when an API exposes plans.
  if (deployment === "review") return null;
  const prepare = async () => {
    let id = Number(orderId);
    if (!Number.isInteger(id) || id <= 0) {
      try {
        const needle = orderId.trim();
        const data = await api<{ records: Array<{ id: number; name: string }> }>(
          `/api/odoo20/business-data?kind=purchase_order&limit=50&q=${encodeURIComponent(needle)}`);
        const matches = data.records;
        if (matches.length !== 1) {
          setError(t(matches.length > 1 ? "odoo20.orderAmbiguous" : "odoo20.orderMissing"));
          return;
        }
        id = Number(matches[0].id);
      } catch { setError(t("odoo20.failed")); return; }
    }
    if (!Number.isInteger(id) || id <= 0 || reason.trim().length < 2) return;
    setBusy("prepare"); setError(""); setPlan(null); setApproved(false); setStatus("");
    try {
      setPlan(await api<Prepared>("/api/odoo20/plans", {
        method: "POST",
        body: JSON.stringify({ purchase_order_id: id, reason: reason.trim() }),
      }));
      refreshTasks();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : t("odoo20.failed"));
    } finally { setBusy(""); }
  };
  const approve = async () => {
    if (!plan) return;
    setBusy("approve"); setError("");
    try {
      await api(`/api/odoo20/plans/${plan.plan_id}/approve`, {
        method: "POST", body: JSON.stringify({ comment: reason.trim() }),
      });
      setApproved(true);
      setStatus(t("odoo20.approved"));
      refreshTasks();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : t("odoo20.failed"));
    } finally { setBusy(""); }
  };
  const revoke = async () => {
    if (!plan) return;
    setBusy("revoke"); setError("");
    try {
      await api(`/api/odoo20/plans/${plan.plan_id}/revoke`, {
        method: "POST", body: JSON.stringify({ reason: reason.trim() }),
      });
      setApproved(false);
      setStatus(t("odoo20.revoked"));
      refreshTasks();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.message : t("odoo20.failed"));
    } finally { setBusy(""); }
  };
  const plannedLines = (plan?.actions ?? []).flatMap((action) => {
    const lines = (action.params as { lines?: Array<{ product_qty?: number; price_unit?: number }> } | undefined)?.lines;
    return Array.isArray(lines) ? lines : [];
  });
  const plannedTotal = plannedLines.reduce((total, line) =>
    total + Number(line.product_qty ?? 0) * Number(line.price_unit ?? 0), 0);
  const plannedCurrency = plan?.actions?.find((action) => action.currency)?.currency ?? "";
  return <section className="odoo20-panel" data-odoo20-plan>
    <div className="odoo20-fields">
      <input aria-label={t("odoo20.purchaseOrder")} placeholder={t("odoo20.purchaseOrder")} value={orderId} onChange={event => setOrderId(event.currentTarget.value)} />
      <input aria-label={t("odoo20.reason")} placeholder={t("odoo20.reason")} value={reason} maxLength={1000} onChange={event => setReason(event.currentTarget.value)} />
      <button type="button" className="secondary" disabled={busy !== "" || !orderId || reason.trim().length < 2} onClick={() => void prepare()}>{t("odoo20.prepare")}</button>
      <button type="button" className="secondary" disabled={busy !== ""} onClick={() => void scanSources()}>{t("odoo20.scanSources")}</button>
    </div>
    {plan && <div className="odoo20-summary" data-odoo20-summary>
      <strong>{plan.order.name} · {plan.order.state}</strong>
      <span>{t("odoo20.changeSummary", { count: String(plan.action_count ?? 0) })}</span>
      {plan.actions?.length ? <ul>{plan.actions.slice(0, 6).map((action, index) =>
        <li key={`action-${index}`}>{String(action.type ?? "")} {action.quantity !== undefined ? `· ${action.quantity} ${action.unit ?? ""}` : ""}</li>)}</ul> : null}
      {plan.lines?.length ? <ul>{plan.lines.slice(0, 4).map((line, index) =>
        <li key={`line-${line.product_id?.[0] ?? index}`}>{Array.isArray(line.product_id) ? line.product_id[1] : t("odoo20.item")} · {line.product_qty ?? 0}{line.price_unit !== undefined ? ` · ${line.price_unit}` : ""}{line.date_planned ? ` · ${line.date_planned}` : ""}</li>)}</ul> : null}
      {plannedLines.length ? <span>{t("odoo20.plannedAmount", { currency: plannedCurrency, amount: plannedTotal.toFixed(2) })}</span> : null}
      {plan.order.amount_total !== undefined ? <span>{t("odoo20.originalAmount", { amount: String(plan.order.amount_total) })}</span> : null}
      {plan.order.date_planned ? <span>{t("odoo20.orderDate", { date: plan.order.date_planned })}</span> : null}
    </div>}
    {plan && <div className="odoo20-actions">
      {!approved && <span>{t("odoo20.awaitingApproval")}</span>}
      {!approved && <button type="button" className="primary" disabled={busy !== ""} onClick={() => void approve()}>{t("odoo20.approveAndExecute")}</button>}
      {approved && <span data-execution-pending="true">{t("odoo20.executing")}</span>}
      {approved && <button type="button" className="secondary" disabled={busy !== ""} onClick={() => void revoke()}>{t("odoo20.revokeApproval")}</button>}
    </div>}
    {tasksUnavailable && <p role="alert">{t("odoo20.tasksUnavailable")} <button type="button" className="secondary" onClick={refreshTasks}>{t("odoo20.retry")}</button></p>}
    {status && <p role="status">{status}</p>}
    {error && <p role="alert">{error}</p>}
    {(failures.length > 0 || attentions.length > 0) && <div className="odoo20-tasks" data-needs-person="true">
      <ul>
        {failures.map(task => <li key={`f:${task.ticket_id}`}>
          <span>{task.goal}<small>{t(attentionNeedKey(task.need))}</small></span>
          <button type="button" className="secondary" onClick={() => void api(`/api/tasks/${task.ticket_id}/retry`, { method: "POST" }).then(refreshTasks).catch(() => setError(t("odoo20.failed")))}>{t("odoo20.retry")}</button>
        </li>)}
        {attentions.map(item => <li key={`a:${item.attention_id}`} data-attention-need={item.need}>
          <span>{item.goal}<small>{t(attentionNeedKey(item.need))} · {item.question}</small></span>
          {item.need === "fact" ? <div className="odoo20-fact-answer">
            <input aria-label={t("odoo20.provideInfo")} value={attentionDrafts[item.attention_id] ?? ""}
              onChange={(event) => setAttentionDrafts((values) => ({ ...values, [item.attention_id]: event.currentTarget.value }))} />
            <button type="button" className="secondary" disabled={!attentionDrafts[item.attention_id]?.trim()}
              onClick={() => void api(`/api/tasks/attentions/${item.attention_id}/answer`, { method: "POST", body: JSON.stringify({ text: attentionDrafts[item.attention_id].trim() }) }).then(refreshTasks).catch(() => setError(t("odoo20.failed")))}>{t("odoo20.provideInfo")}</button>
          </div> : item.need === "rule" ? <button type="button" className="secondary"
            onClick={() => void api(`/api/tasks/attentions/${item.attention_id}/answer`, { method: "POST", body: JSON.stringify({ confirmed: true }) }).then(refreshTasks).catch(() => setError(t("odoo20.failed")))}>{t("odoo20.confirmRule")}</button>
            : item.need === "result_unknown" && item.plan_id ? <button type="button" className="secondary"
              onClick={() => void api(`/api/odoo20/plans/${item.plan_id}/reconcile`, { method: "POST", body: JSON.stringify({ attention_id: item.attention_id }) }).then(refreshTasks).catch(() => setError(t("odoo20.failed")))}>{t("odoo20.reconcileAgain")}</button>
            : <span className="task-need">{t(item.need === "approval" ? "odoo20.viewApproval" : "odoo20.reconcileResult")}</span>}
        </li>)}
      </ul>
    </div>}
    {matters.length > 0 && <div className="odoo20-tasks" data-matters="true">
      <ul>{matters.map(item => <li key={item.key}>
        <span>{item.goal}</span>
        <span data-user-state={item.user_state ?? item.state} className={`task-state task-state-${taskStateTone(item.user_state ?? item.state)}`}>{t(taskStateKey(item.user_state ?? item.state))}</span>
        {item.attentionOpen && <span className="task-need">{t(attentionNeedKey(item.need))}</span>}
        {item.user_state !== "completed" && <button type="button" className="secondary"
          title={t("odoo20.cancelInvestigationHint")}
          aria-label={t("odoo20.cancelInvestigation")}
          onClick={() => void api(`/api/tasks/${item.key}/cancel`, { method: "POST" }).then(refreshTasks).catch(() => setError(t("odoo20.failed")))}>{t("odoo20.cancelInvestigation")}</button>}
      </li>)}</ul>
    </div>}
    {rules.length > 0 && <div className="odoo20-tasks">
      <h3>{t("odoo20.rules")}</h3>
      <ul>{rules.slice(0, 6).map(rule => <li key={`${rule.name}:${rule.version}`}>
        <span>{rule.name}</span><span>v{rule.version}</span>
      </li>)}</ul>
    </div>}
  </section>;
}
