import { RecordedRules } from "./RecordedRules";
import { useEffect, useMemo, useRef, useState } from "react";
import { JsonTree } from "@forgeflow/dsh-primitives";
import { getRun, getPlans, getPlan, getApprovals } from "./api";
import { formatDateTime, shortId, stateLabel } from "./caseUi";
import { SelectMenu } from "./SelectMenu";
import { useI18n, type Translate } from "./i18n";
import { runTaskLabel, profileLabel } from "./runLabels";
import { navigate, useWorkspace } from "./workspace";
import type { Approval, Config, Plan, RunDetail, RunSummary } from "./types";

type Props = { runs: RunSummary[]; config: Config; selected: string | null; error?: string; onSelect: (id: string) => void; onRefresh: () => void };
const PAGE_SIZE = 15;
function orderedConfig(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(orderedConfig);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).filter(([key]) => key !== "label").sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, orderedConfig(item)]));
  return value;
}
const configKey = (run: RunSummary) => run.experiment ? JSON.stringify(orderedConfig(run.experiment)) : "unknown";
type Experiment = NonNullable<RunSummary["experiment"]>;
const compact = (value: unknown, max = 26) => {
  const text = value == null ? "—" : String(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
};
const configBase = (value: Experiment) => [value.model, value.budget_seconds, value.planner?.profile, value.data_profile?.name];
function configDigest(value: Experiment): string {
  let hash = 2166136261;
  for (const char of JSON.stringify(orderedConfig(value))) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  return (hash >>> 0).toString(16).padStart(8, "0");
}
// A short business configuration summary; full recorded parameters remain in
// the selected configuration disclosure. A digest disambiguates minor factors.
function contractGuidanceLabel(value: Experiment["contract_guidance"], t: Translate): string {
  if (!value || value.recorded !== true) return t("runs.contractNotRecorded");
  if (value.profile === "none") return t("runs.contractNone");
  return value.profile === "reviewed-request-v1" ? t("runs.contractReviewed") : value.profile;
}
function configurationLabel(value: Experiment, all: Experiment[], t: Translate): string {
  const baseKey = JSON.stringify(configBase(value));
  const peers = all.filter(item => JSON.stringify(configBase(item)) === baseKey);
  const base = [compact(value.model?.split("/").pop(), 34), `${compact(value.budget_seconds, 8)}s`, compact(value.planner?.profile, 24), compact(value.data_profile?.name, 24)].join(" · ");
  const factors: [string, (item: Experiment) => unknown][] = [
    ["guidance", item => item.execution_guidance?.profile],
    [t("runs.contractFactor"), item => contractGuidanceLabel(item.contract_guidance, t)],
    ["security", item => item.security_profile?.name],
    ["thinking", item => item.thinking_mode],
    ["output", item => item.output_policy?.max_output_tokens],
    ["context", item => item.context_window_tokens],
    ["temperature", item => item.sampling_params?.temperature],
    ["top_p", item => item.sampling_params?.top_p],
  ];
  const selected: typeof factors = [];
  let distinct = 1;
  for (const factor of factors) {
    const next = [...selected, factor];
    const count = new Set(peers.map(item => JSON.stringify(next.map(([, read]) => read(item) ?? null)))).size;
    if (count > distinct) { selected.push(factor); distinct = count; }
    if (distinct >= peers.length || selected.length === 2) break;
  }
  const differences = selected.map(([label, read]) => `${label}: ${compact(read(value), 22)}`);
  return [base, ...differences, `cfg-${configDigest(value)}`].join(" · ");
}
// Server projection is authoritative. Legacy prepared state alone cannot
// establish that no ERP execution occurred.
function planExecutionLabel(plan: Plan, t: Translate): string {
  const status = plan.execution?.status;
  if (status === "unknown") return t("runs.planExecutionUnconfirmed");
  if (status === "succeeded") return t("case.state.succeeded");
  if (status === "stopped") return t("runs.planStopped");
  if (status === "not_recorded") return t("runs.planPrepared");
  if (plan.success?.execution_validated === true) return t("case.state.succeeded");
  if (plan.success?.execution_validated === false) return t("runs.planStopped");
  if (plan.journal_present === true || plan.state === "unknown" || plan.state === "execution_unknown") return t("runs.planExecutionUnconfirmed");
  if (plan.state === "prepared") return t("runs.planPrepared");
  if (plan.state === "stopped") return t("runs.planStopped");
  if (plan.state === "completed" || plan.state === "succeeded") return t("case.state.succeeded");
  return stateLabel(plan.state, t);
}
const experimentCategory = (run: RunSummary) => run.agent?.category || "unclassified";
export function RunsView({ runs, selected, error, onSelect, onRefresh }: Props) {
  const { t, lang } = useI18n();
  const { scope_id } = useWorkspace();
  const filterKey = `forgeflow.evidence-view.${scope_id}`;
  const initial = useMemo(() => { try { return JSON.parse(sessionStorage.getItem(filterKey) || "{}"); } catch { return {}; } }, [filterKey]);
  const [search, setSearch] = useState<string>(typeof initial.search === "string" ? initial.search : "");
  const [filter, setFilter] = useState<string>(initial.filter || "all");
  const [category, setCategory] = useState<string>(initial.category || "all");
  const [configuration, setConfiguration] = useState<string>(initial.configuration || "all");
  const [task, setTask] = useState<string>(initial.task || "all");
  const [validity, setValidity] = useState<string>(initial.validity || "all");
  const [dateFrom, setDateFrom] = useState<string>(initial.dateFrom || "");
  const [dateTo, setDateTo] = useState<string>(initial.dateTo || "");
  const [page, setPage] = useState<number>(Number.isInteger(initial.page) ? initial.page : 1);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [planId, setPlanId] = useState<string | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [status, setStatus] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { try { sessionStorage.setItem(filterKey, JSON.stringify({ search, filter, page, category, configuration, task, validity, dateFrom, dateTo })); } catch { /* optional */ } }, [filterKey, search, filter, page, category, configuration, task, validity, dateFrom, dateTo]);
  useEffect(() => {
    let active = true;
    setDetail(null); setPlans([]); setPlanId(null); setStatus("");
    if (selected) {
      setStatus(t("runs.statusLoading"));
      Promise.all([getRun(selected), getPlans(selected)]).then(([run, list]) => {
        if (!active) return;
        setDetail(run); setPlans(list); setPlanId(list[0]?.plan_id || null); setStatus("");
        requestAnimationFrame(() => { heading.current?.focus(); heading.current?.scrollIntoView({ block: "start" }); });
      }).catch(failure => { if (active) setStatus(failure instanceof Error ? failure.message : t("runs.statusLoadFailed")); });
    }
    return () => { active = false; };
  }, [selected, t]);
  useEffect(() => {
    let active = true;
    setPlan(null); setApprovals([]);
    if (selected && planId) Promise.all([getPlan(selected, planId), getApprovals(selected, planId)]).then(([value, items]) => { if (active) { setPlan(value); setApprovals(items); } }).catch(failure => { if (active) setStatus(String(failure)); });
    return () => { active = false; };
  }, [selected, planId]);
  const title = (run: RunSummary) => runTaskLabel(run.task, run.task_label, t);
  const configs = Array.from(new Map(runs.filter(run => run.experiment).map(run => [configKey(run), run.experiment!])).entries());
  const tasks = Array.from(new Set(runs.map(run => run.task).filter((value): value is string => !!value)));
  const filtered = runs.filter(run => `${title(run)} ${run.run_id} ${run.task || ""}`.toLowerCase().includes(search.toLowerCase())
    && (category === "all" || experimentCategory(run) === category)
    && (configuration === "all" || configKey(run) === configuration)
    && (task === "all" || run.task === task)
    && (validity === "all" || (validity === "valid" ? run.evaluation_valid === true : validity === "invalid" ? run.evaluation_valid === false : run.evaluation_valid == null))
    && (!dateFrom || !!run.started_at && run.started_at.slice(0, 10) >= dateFrom)
    && (!dateTo || !!run.started_at && run.started_at.slice(0, 10) <= dateTo)
    && (filter === "all" || (filter === "verified" ? run.strict_success === true && run.evaluation_valid === true : filter === "failed" ? run.strict_success === false || run.evaluation_valid === false : run.strict_success == null || run.evaluation_valid == null)));
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const actualPage = Math.max(1, Math.min(page, pages));
  const resultLabel = (strict: boolean | null | undefined, valid: boolean | null | undefined) => t(strict === true && valid === true ? "v9.verified" : strict === false || valid === false ? "v9.failedResults" : "v9.verificationUnknown");
  const jsonLabels = { copyValue: t("v9.copy"), copyJson: t("v9.copy"), copyPath: t("v9.copy"), copyPrettyJson: t("v9.copy"), copyCompactJson: t("v9.copy"), copied: t("v9.copied"), copyFailed: t("v9.copyFailed"), collapseNode: t("sidebar.collapse"), expandNode: t("sidebar.expand"), copyButtonTitle: (action: string) => action };
  if (selected) return <div className="view evidence-detail" data-evidence-detail="true">
    <button type="button" className="secondary" onClick={() => navigate("#runs")}>{t("v9.backRuns")}</button>
    <h1 tabIndex={-1} ref={heading}>{detail ? runTaskLabel(detail.task?.id, detail.task?.label, t) : t("runs.detailTitle")}</h1>
    <p className="muted">{t("v9.historicalOnly")}</p>
    {status && <p role="status">{status}</p>}
    {detail && <>
      <section className="panel evidence-panel" data-evidence="panel" aria-label={t("runs.evidenceTitle")}>
        <h2>{resultLabel(detail.evaluation?.strict_success, detail.evaluation?.evaluation_valid)}</h2>
        <dl className="facts">
          <dt>{t("runs.colScore")}</dt><dd>{detail.evaluation?.score ?? "—"}</dd>
          <dt>{t("runs.evidenceStrict")}</dt><dd data-evidence="strict-success">{detail.evaluation?.strict_success == null ? t("v9.unknown") : t(detail.evaluation.strict_success ? "runs.yes" : "runs.no")}</dd>
          <dt>{t("runs.evidenceEvalValid")}</dt><dd data-evidence="evaluation-valid">{detail.evaluation?.evaluation_valid == null ? t("v9.unknown") : t(detail.evaluation.evaluation_valid ? "runs.yes" : "runs.no")}</dd>
          <dt>{t("runs.evidenceStarted")}</dt><dd>{formatDateTime(detail.timing?.started_at, lang)}</dd>
          <dt>{t("runs.evidenceElapsed")}</dt><dd>{detail.timing?.elapsed_seconds ?? "—"}</dd>
          <dt>{t("runs.evidenceModel")}</dt><dd>{detail.experiment?.model || "—"}</dd>
          <dt>{t("runs.evidencePlanner")}</dt><dd>{profileLabel("planner", detail.experiment?.planner?.profile, t)}</dd>
          <dt>{t("runs.evidenceDataProfile")}</dt><dd>{profileLabel("data", detail.experiment?.data_profile?.name, t)}</dd>
          <dt>{t("runs.evidenceSecurity")}</dt><dd>{detail.experiment?.security_profile?.name || "—"}</dd>
          <dt>{t("runs.contractGuidance")}</dt><dd data-contract-guidance="true">{contractGuidanceLabel(detail.experiment?.contract_guidance, t)}</dd>
          <dt>{t("runs.colRunId")}</dt><dd><span className="mono" title={selected}>{shortId(selected, 24)}</span> <button className="activity-link" type="button" onClick={() => { void navigator.clipboard?.writeText(selected).catch(() => setStatus(t("v9.copyFailed"))); }}>{t("v9.copy")}</button></dd>
        </dl>
      </section>
      <RecordedRules evaluation={detail.evaluation} />
      {plans.length > 0 && <section className="panel"><h2>{t("runs.plansTitle")}</h2><p className="muted">{t("runs.planStateScope")}</p><div className="plan-picker">{plans.map(item => <button key={item.plan_id} type="button" className="secondary" aria-pressed={planId === item.plan_id} onClick={() => setPlanId(item.plan_id)}>{shortId(item.plan_id, 16)} · {t("case.executionStatus")}: {planExecutionLabel(item, t)}</button>)}</div>
        {plan && <><dl className="facts"><dt>{t("runs.colSteps")}</dt><dd>{plan.prepared?.step_count ?? "—"}</dd><dt>{t("runs.colExpectedSpend")}</dt><dd>{plan.prepared?.expected_new_spend ?? "—"}</dd></dl>
          <details><summary>{t("v9.technical")}</summary><JsonTree data={{ plan, approvals }} label={t("v9.evidenceJson")} copyable labels={jsonLabels} /></details></>}
      </section>}
      <details className="panel" data-evidence-json="true"><summary>{t("v9.evidenceJson")}</summary><JsonTree data={detail} label={t("v9.evidenceJson")} copyable labels={jsonLabels} /></details>
    </>}
  </div>;
  return <div className="view" data-runs-list="true">
    <div className="view-head"><h1>{t("runs.title")}</h1><p>{t("v9.historicalOnly")}</p></div>
    <div className="evidence-filters">
      <input type="search" aria-label={t("v9.searchRuns")} placeholder={t("v9.searchRuns")} value={search} onChange={event => { setSearch(event.target.value); setPage(1); }} />
      <SelectMenu
        ariaLabel={t("v9.resultFilter")}
        value={filter}
        options={[
          { value: "all", label: t("v9.allResults") },
          { value: "verified", label: t("v9.verified") },
          { value: "failed", label: t("v9.failedResults") },
          { value: "unknown", label: t("v9.unknownResults") },
        ]}
        onChange={value => { setFilter(value); setPage(1); }}
      />
      <SelectMenu
        ariaLabel={t("v9.category")}
        value={category}
        options={[
          { value: "all", label: t("v9.allCategories") },
          { value: "agent_evaluation", label: t("v9.agentEvaluation") },
          { value: "environment_control", label: t("v9.environmentControl") },
          { value: "unclassified", label: t("v9.unclassified") },
        ]}
        onChange={value => { setCategory(value); setPage(1); }}
      />
      <SelectMenu
        ariaLabel={t("v9.configuration")}
        value={configuration}
        options={[
          { value: "all", label: t("v9.allConfigurations") },
          { value: "unknown", label: t("v9.unknownConfiguration") },
          ...configs.map(([key, value]) => ({ value: key, label: configurationLabel(value, configs.map(([, item]) => item), t) })),
        ]}
        onChange={value => { setConfiguration(value); setPage(1); }}
      />
      <SelectMenu
        ariaLabel={t("v9.taskFilter")}
        value={task}
        options={[
          { value: "all", label: t("v9.allTasks") },
          ...tasks.map(value => ({ value, label: runTaskLabel(value, null, t) })),
        ]}
        onChange={value => { setTask(value); setPage(1); }}
      />
      <SelectMenu
        ariaLabel={t("v9.validity")}
        value={validity}
        options={[
          { value: "all", label: t("v9.allValidity") },
          { value: "valid", label: t("v9.valid") },
          { value: "invalid", label: t("v9.invalid") },
          { value: "unknown", label: t("v9.unknownResults") },
        ]}
        onChange={value => { setValidity(value); setPage(1); }}
      />
      <label>{t("v9.dateFrom")}<input type="date" aria-label={t("v9.dateFrom")} value={dateFrom} onChange={event => { setDateFrom(event.target.value); setPage(1); }} /></label>
      <label>{t("v9.dateTo")}<input type="date" aria-label={t("v9.dateTo")} value={dateTo} onChange={event => { setDateTo(event.target.value); setPage(1); }} /></label>
      <button className="secondary" onClick={() => { setSearch(""); setFilter("all"); setCategory("all"); setConfiguration("all"); setTask("all"); setValidity("all"); setDateFrom(""); setDateTo(""); setPage(1); }}>{t("v9.clearFilters")}</button>
      <button className="secondary" onClick={onRefresh}>{t("runs.refresh")}</button>
    </div>
    <section className="evidence-counts" data-evidence-counts="true"><strong>{t("v9.filteredCounts", { passed: filtered.filter(run => run.strict_success === true && run.evaluation_valid === true).length, total: filtered.length })}</strong><p className="muted">{t("v9.countScope")}</p>{configuration !== "all" && configuration !== "unknown" && <details><summary>{t("v9.configDetails")}</summary><JsonTree data={JSON.parse(configuration)} label={t("v9.configuration")} copyable labels={jsonLabels} /></details>}</section>
    {error && <p role="alert">{error}</p>}
    <section className="panel" aria-label={t("runs.recordsAria")}><div className="table-wrap"><table><thead><tr><th>{t("runs.colTask")}</th><th>{t("runs.colScore")}</th><th>{t("v9.resultFilter")}</th></tr></thead>
      <tbody>{filtered.slice((actualPage - 1) * PAGE_SIZE, actualPage * PAGE_SIZE).map(run => <tr key={run.run_id}><td><button className="run-link" onClick={() => onSelect(run.run_id)}><strong>{title(run)}</strong><small>{shortId(run.run_id, 30)}</small></button></td><td>{run.score ?? "—"}</td><td>{resultLabel(run.strict_success, run.evaluation_valid)}</td></tr>)}</tbody></table></div>
      {filtered.length === 0 && <p>{t("v9.noRuns")}</p>}
    </section>
    <div className="pagination"><button className="secondary" disabled={actualPage <= 1} onClick={() => setPage(actualPage - 1)}>{t("v9.prev")}</button><span>{t("v9.page", { page: actualPage, pages, count: filtered.length })}</span><button className="secondary" disabled={actualPage >= pages} onClick={() => setPage(actualPage + 1)}>{t("v9.next")}</button></div>
  </div>;
}
