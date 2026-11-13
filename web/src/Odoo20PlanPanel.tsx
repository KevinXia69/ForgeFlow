import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { useI18n } from "./i18n";
import { useWorkspace } from "./workspace";
import { attentionNeedKey, taskStateKey, taskStateTone } from "./taskState";
import { planActionState } from "./odoo20PlanState";

import { Odoo20PlanReview } from "./Odoo20PlanReview";
import { completeReview, reviewCanApprove, reviewIdentity, type PreparedPlan as Prepared } from "./planReviewIdentity";

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
  const [loadedPlan, setPlan] = useState<Prepared | null>(null);
  const context = `${deployment}:${initialPlanId ?? "new"}`;
  const [loadedContext, setLoadedContext] = useState(context);
  const plan = loadedContext === context ? loadedPlan : null;
  const requestEpoch = useRef(0);
  const contextRef = useRef(context);
  contextRef.current = context;
  const [reviewed, setReviewed] = useState(false);
  const [reviewUnavailable, setReviewUnavailable] = useState(false);
  const [approved, setApproved] = useState(false);
  const [busy, setBusy] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [tasks, setTasks] = useState<PlanTask[]>([]);
  const [tasksLoaded, setTasksLoaded] = useState(false);
  const [failures, setFailures] = useState<FailureTask[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [openTasks, setOpenTasks] = useState<TaskInfo[]>([]);
  const [attentions, setAttentions] = useState<AttentionInfo[]>([]);
  const [tasksUnavailable, setTasksUnavailable] = useState(false);
  const [attentionDrafts, setAttentionDrafts] = useState<Record<string, string>>({});
  const selectedTask = tasks.find((task) => task.plan_id === plan?.plan_id);
  const actionState = planActionState(plan?.task, selectedTask, approved, tasksLoaded);
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
    if (deployment !== "local") return;
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
    void api<PlanTask[]>("/api/odoo20/tasks").then((rows) => {
      setTasks(rows); setTasksLoaded(true);
    }).catch(() => {
      setTasks([]); setTasksLoaded(false); setTasksUnavailable(true);
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
  useEffect(() => { refreshTasks(); }, [deployment]);
  useEffect(() => {
    const epoch = ++requestEpoch.current;
    setPlan(null); setApproved(false); setReviewed(false); setReviewUnavailable(false);
    setStatus(""); setError(""); setBusy(initialPlanId ? "load" : "");
    if (initialPlanId) void api<Prepared>(`/api/odoo20/plans/${initialPlanId}`)
      .then((value) => {
        if (requestEpoch.current !== epoch || contextRef.current !== context) return;
        if (value.plan_id !== initialPlanId) throw new Error("plan_identity_mismatch");
        setLoadedContext(context); setPlan(value); setOrderId(String(value.order?.name ?? ""));
      })
      .catch(() => {
        if (requestEpoch.current !== epoch || contextRef.current !== context) return;
        setReviewUnavailable(true); setError(t("odoo20.failed"));
      })
      .finally(() => { if (requestEpoch.current === epoch) setBusy(""); });
    return () => { requestEpoch.current += 1; };
  }, [context, initialPlanId, t]);
  const refreshReview = async () => {
    const id = plan?.plan_id ?? initialPlanId;
    if (!id) return;
    const epoch = ++requestEpoch.current;
    try {
      const current = await api<Prepared>(`/api/odoo20/plans/${id}`);
      if (requestEpoch.current !== epoch || contextRef.current !== context) return;
      if (current.plan_id !== id) throw new Error("plan_identity_mismatch");
      if (!plan || reviewIdentity(current) !== reviewIdentity(plan)) {
        setReviewed(false); setApproved(false); setStatus(t("odoo20.reviewChanged"));
      }
      setPlan(current); setLoadedContext(context); setReviewUnavailable(false); setError("");
    } catch {
      if (requestEpoch.current !== epoch || contextRef.current !== context) return;
      setReviewed(false); setReviewUnavailable(true); setError(t("odoo20.reviewUnavailable"));
    }
  };
  useEffect(() => {
    if (!plan || actionState !== "awaiting_approval") return;
    const timer = setInterval(() => { if (!busy) void refreshReview(); }, 5000);
    return () => clearInterval(timer);
  }, [plan, actionState, busy, context]);
  useEffect(() => {
    if (!plan?.expires_at) return;
    const delay = Date.parse(plan.expires_at) - Date.now();
    if (!Number.isFinite(delay) || delay <= 0) return;
    const timer = setTimeout(() => { setReviewed(false); setStatus(t("odoo20.reviewInactive")); }, Math.min(delay + 1, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [plan, t]);
  // Approval queues a durable execution effect; the supervisor advances it.  The
  // page polls the same matter instead of asking the person to press "apply".
  useEffect(() => {
    if (actionState !== "executing" || !plan) return;
    const timer = setInterval(() => {
      void api<PlanTask[]>("/api/odoo20/tasks").then((rows) => {
        setTasks(rows); setTasksLoaded(true);
        const row = rows.find((item) => item.plan_id === plan.plan_id);
        if (!row) return;
        if (row.state === "completed") {
          setStatus(t("odoo20.verified"));
          setApproved(false);
        } else if (row.state === "blocked" || row.state === "failed") {
          setStatus(t("odoo20.executionNeedsAction"));
        }
      }).catch(() => undefined);
    }, 2500);
    return () => clearInterval(timer);
  }, [actionState, plan, t]);
  const prepare = async () => {
    if (busy || reason.trim().length < 2) return;
    const epoch = ++requestEpoch.current;
    setBusy("prepare"); setError(""); setPlan(null); setApproved(false); setReviewed(false); setReviewUnavailable(false); setStatus("");
    try {
      let id = Number(orderId);
      if (!Number.isInteger(id) || id <= 0) {
        const data = await api<{ records: Array<{ id: number; name: string }> }>(
          `/api/odoo20/business-data?kind=purchase_order&limit=50&q=${encodeURIComponent(orderId.trim())}`);
        if (requestEpoch.current !== epoch || contextRef.current !== context) return;
        if (data.records.length !== 1) {
          setError(t(data.records.length > 1 ? "odoo20.orderAmbiguous" : "odoo20.orderMissing"));
          return;
        }
        id = Number(data.records[0].id);
      }
      if (!Number.isInteger(id) || id <= 0) return;
      const prepared = await api<Prepared>("/api/odoo20/plans", {
        method: "POST", body: JSON.stringify({ purchase_order_id: id, reason: reason.trim() }),
      });
      if (requestEpoch.current !== epoch || contextRef.current !== context) return;
      setLoadedContext(context); setPlan(prepared); refreshTasks();
    } catch (failure) {
      if (requestEpoch.current !== epoch || contextRef.current !== context) return;
      setError(failure instanceof ApiError ? failure.message : t("odoo20.failed"));
    } finally { if (requestEpoch.current === epoch) setBusy(""); }
  };
  const approve = async () => {
    if (!plan || !reviewCanApprove(plan) || !reviewed || reviewUnavailable || busy
      || actionState !== "awaiting_approval" || deployment === "review") return;
    const epoch = ++requestEpoch.current;
    setBusy("approve"); setError("");
    try {
      const current = await api<Prepared>(`/api/odoo20/plans/${plan.plan_id}`);
      if (requestEpoch.current !== epoch || contextRef.current !== context) return;
      if (!reviewCanApprove(current) || reviewIdentity(current) !== reviewIdentity(plan)
        || current.task?.attention !== "open" || current.task?.need !== "approval") {
        setPlan(current.plan_id === plan.plan_id ? current : null);
        setReviewed(false); setApproved(false); setError(t("odoo20.reviewChanged"));
        return;
      }
      await api(`/api/odoo20/plans/${plan.plan_id}/approve`, {
        method: "POST", body: JSON.stringify({ comment: reason.trim(),
          expected_plan_version: plan.plan_version, expected_action_digest: plan.action_digest,
          expected_plan_digest: plan.plan_digest }),
      });
      if (requestEpoch.current !== epoch || contextRef.current !== context) return;
      setApproved(true);
      setStatus(t("odoo20.approved"));
      refreshTasks();
    } catch (failure) {
      if (requestEpoch.current !== epoch || contextRef.current !== context) return;
      setReviewed(false); setReviewUnavailable(true);
      setError(failure instanceof ApiError ? failure.message : t("odoo20.failed"));
    } finally { if (requestEpoch.current === epoch) setBusy(""); }
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
  return <section className="odoo20-panel" data-odoo20-plan>
    <div className="odoo20-fields">
      <input aria-label={t("odoo20.purchaseOrder")} placeholder={t("odoo20.purchaseOrder")} value={orderId} onChange={event => setOrderId(event.currentTarget.value)} />
      <input aria-label={t("odoo20.reason")} placeholder={t("odoo20.reason")} value={reason} maxLength={1000} onChange={event => setReason(event.currentTarget.value)} />
      <button type="button" className="secondary" disabled={busy !== "" || !orderId || reason.trim().length < 2} onClick={() => void prepare()}>{t("odoo20.prepare")}</button>
      {deployment === "local" && <button type="button" className="secondary" disabled={busy !== ""} onClick={() => void scanSources()}>{t("odoo20.scanSources")}</button>}
    </div>
    {plan && <div className="odoo20-summary" data-odoo20-summary>
      <strong>{plan.order?.name ?? t("v9.unknown")} · {plan.order?.state ?? t("v9.unknown")}</strong>
      <Odoo20PlanReview plan={plan} />
      {!completeReview(plan) && <p role="alert">{t("odoo20.reviewIncomplete")}</p>}
      {completeReview(plan) && actionState === "awaiting_approval" && !reviewCanApprove(plan) && <p role="alert">{t("odoo20.reviewInactive")}</p>}
      {actionState === "awaiting_approval" && <label className="approval-acknowledgement">
        <input type="checkbox" data-review-ack checked={reviewed} disabled={!reviewCanApprove(plan) || reviewUnavailable || !!busy}
          onChange={event => setReviewed(event.currentTarget.checked)} />{t("odoo20.reviewAcknowledgement")}
      </label>}
    </div>}
    {reviewUnavailable && <p role="alert">{t("odoo20.reviewUnavailable")} <button type="button" className="secondary" disabled={!!busy} onClick={() => void refreshReview()}>{t("odoo20.retry")}</button></p>}
    {plan && <div className="odoo20-actions" data-plan-action-state={actionState}>
      {actionState === "loading" && <span>{t("cases.loading")}</span>}
      {actionState === "awaiting_approval" && <span>{t("odoo20.awaitingApproval")}</span>}
      {actionState === "awaiting_approval" && deployment !== "review" && <button type="button" className="primary" data-plan-approve disabled={busy !== "" || !reviewed || reviewUnavailable || !reviewCanApprove(plan)} onClick={() => void approve()}>{t("odoo20.approveAndExecute")}</button>}
      {actionState === "executing" && <span data-execution-pending="true">{t("odoo20.executing")}</span>}
      {actionState === "executing" && deployment !== "review" && <button type="button" className="secondary" disabled={busy !== ""} onClick={() => void revoke()}>{t("odoo20.revokeApproval")}</button>}
      {actionState === "completed" && <span>{t("odoo20.verified")}</span>}
      {actionState === "closed" && <span>{t("task.state.completed")}</span>}
      {actionState === "needs_action" && <span>{t("odoo20.executionNeedsAction")}</span>}
      {actionState === "read_only" && <span>{t("odoo20.approvalHandled")}</span>}
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
        {item.user_state !== "completed" && !["completed", "cancelled", "closed"].includes(item.state) && <button type="button" className="secondary"
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
