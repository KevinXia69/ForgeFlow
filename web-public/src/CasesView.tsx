import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createProductCase, createProductFromDraft, getCapabilities, getProductCase, getProductCases, previewProductDraft, type ProductDraft } from "./api";
import { startCaseReadWatch } from "./caseReadWatch";
import { CaseDetail } from "./CaseDetail";
import { environmentAccessNotice } from "./environmentAccess";
import { CaseChatView } from "./CaseChatView";
import { buildFixtureCase } from "./caseFixture";
import { caseOutcomeLabel, dueWindow, errorDetail, formatDateTime, exceptionText, formatQuantity, isCaseVerified, matchesSavedView, severityLabel, severityOf, type SavedCaseView } from "./caseUi";
import { SelectMenu } from "./SelectMenu";
import { useI18n, type MessageKey } from "./i18n";
import { caseRoute, navigate, useWorkspace } from "./workspace";
import type { Capabilities, CaseFile } from "./types";

const VIEWS: { id: SavedCaseView; label: MessageKey }[] = [
  { id: "decision", label: "cases.viewDecision" }, { id: "risk", label: "cases.viewRisk" },
  { id: "executing", label: "cases.viewExecuting" }, { id: "verified", label: "cases.viewVerified" }, { id: "all", label: "cases.viewAll" },
];
const matches = matchesSavedView;
type Props = {
  selectedId: string | null; chat: boolean; evidence: boolean; demo: boolean; initialView?: string | null;
  onSelectCase: (id: string | null) => void;
  onAskCase: (item: CaseFile, mode: "live" | "fixture") => void;
  onOpenEvidence: () => void;
};
export function CasesView({ selectedId, chat, evidence, demo, initialView, onSelectCase, onAskCase, onOpenEvidence }: Props) {
  const { t, lang } = useI18n();
  const identity = useWorkspace();
  const fixture = useMemo(() => buildFixtureCase(t), [t]);
  const isDemo = demo || selectedId === fixture.case_id;
  const refreshId = useRef(0);
  const caseUpdateVersion = useRef(0);
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const [operationPending, setOperationPending] = useState(false);
  const [watchGeneration, setWatchGeneration] = useState(0);
  const [watchExpired, setWatchExpired] = useState(false);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [cases, setCases] = useState<CaseFile[]>([]);
  const [selected, setSelected] = useState<CaseFile | null>(null);
  const [selectedConfirmedAt, setSelectedConfirmedAt] = useState<string | null>(null);
  const [draft, setDraft] = useState<ProductDraft>({ kind: "supplier_cancellation", product_code: "P27637E67E7-LED-CRS-009", units: 50, due_days: 8 });
  const [draftResult, setDraftResult] = useState<{ valid: boolean; errors: string[]; scenario: string | null } | null>(null);
  const [draftBusy, setDraftBusy] = useState(false);
  const [welcome, setWelcome] = useState(() => {
    try { return localStorage.getItem("forgeflow.welcome.v1") !== "dismissed"; } catch { return true; }
  });
  const dismissWelcome = () => {
    setWelcome(false);
    try { localStorage.setItem("forgeflow.welcome.v1", "dismissed"); } catch { /* storage unavailable */ }
  };
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [scenario, setScenario] = useState<"A" | "B" | null>(null);
  const [view, setView] = useState<SavedCaseView>(() => {
    // A view named in the route wins over the remembered one.
    if (initialView && VIEWS.some(v => v.id === initialView)) return initialView as SavedCaseView;
    try { const saved = sessionStorage.getItem(`forgeflow.case-view.${identity.scope_id}`); return VIEWS.some(v => v.id === saved) ? saved as SavedCaseView : "all"; }
    catch { return "all"; }
  });
  useEffect(() => { try { sessionStorage.setItem(`forgeflow.case-view.${identity.scope_id}`, view); } catch { /* optional */ } }, [identity.scope_id, view]);
  const refresh = useCallback(async () => {
    const requestId = ++refreshId.current;
    const dataVersion = caseUpdateVersion.current;
    setLoading(true); setCaps(null); setWatchExpired(false);
    setWatchGeneration(value => value + 1);
    const [capResult, caseResult, detailResult] = await Promise.allSettled([getCapabilities(), getProductCases(), selectedId && !isDemo ? getProductCase(selectedId) : Promise.resolve(null)]);
    if (requestId !== refreshId.current) return;
    if (capResult.status === "fulfilled") setCaps(capResult.value); else setCaps(null);
    if (caseResult.status === "fulfilled" && dataVersion === caseUpdateVersion.current) { setCases(caseResult.value); setError(""); }
    else if (caseResult.status === "rejected") setError(errorDetail(caseResult.reason));
    if (selectedId && !isDemo && dataVersion === caseUpdateVersion.current) {
      if (detailResult.status === "fulfilled" && detailResult.value) {
        caseUpdateVersion.current++;
        setSelected(detailResult.value); setSelectedConfirmedAt(new Date().toISOString()); setDetailError("");
      } else if (detailResult.status === "rejected") setDetailError(errorDetail(detailResult.reason));
    }
    setLoading(false);
  }, [selectedId, isDemo]);
  useEffect(() => {
    caseUpdateVersion.current++; setOperationPending(false);
    setSelected(isDemo ? fixture : null); setSelectedConfirmedAt(isDemo ? new Date().toISOString() : null); setDetailError("");
    void refresh();
    return () => { refreshId.current++; caseUpdateVersion.current++; };
  }, [refresh, isDemo, fixture]);
  const effective = isDemo ? [fixture] : cases;
  const visible = effective.filter(item => matches(item, view));
  const selectedIndex = visible.findIndex(item => item.case_id === selectedId);
  const next = selectedIndex < 0 ? null : visible[selectedIndex + 1];
  const availableScenarios = caps?.product_scenarios ?? [];
  const selectedScenario = availableScenarios.find(item => item.id === scenario) ?? availableScenarios[0];
  const canCreate = caps?.case_create === true && selectedScenario?.can_create === true && !loading && !isDemo;
  const create = async () => {
    if (!canCreate || !selectedScenario || busy) return;
    setBusy(true); setError("");
    try { const item = await createProductCase(selectedScenario.id); setCases(previous => [item, ...previous]); onSelectCase(item.case_id); }
    catch (failure) { setError(errorDetail(failure)); }
    finally { setBusy(false); }
  };
  const updated = useCallback((item: CaseFile) => {
    if (item.case_id !== selectedIdRef.current) return;
    caseUpdateVersion.current++;
    setSelected(item); setSelectedConfirmedAt(new Date().toISOString()); setDetailError("");
    setCases(previous => previous.some(old => old.case_id === item.case_id)
      ? previous.map(old => old.case_id === item.case_id ? item : old) : [item, ...previous]);
  }, []);
  const shouldWatch = !isDemo && selected?.case_id === selectedId &&
    (operationPending || selected?.state === "executing" || selected?.state === "preparing");
  useEffect(() => {
    if (!shouldWatch || !selectedId) return;
    setWatchExpired(false);
    return startCaseReadWatch({
      read: signal => getProductCase(selectedId, signal),
      version: () => caseUpdateVersion.current,
      accept: updated,
      onError: failure => setDetailError(errorDetail(failure)),
      onExpired: () => setWatchExpired(true),
    });
  }, [shouldWatch, selectedId, updated, watchGeneration]);
  const updateDraft = (kind: string) => {
    setDraftResult(null);
    setDraft(kind === "workcenter_outage"
      ? { kind, product_code: "P1223565D60-SOL-CSC-007", units: 228, due_days: 8 }
      : { kind, product_code: "P27637E67E7-LED-CRS-009", units: 50, due_days: 8 });
  };
  const previewDraft = async () => {
    if (isDemo) return;
    setDraftBusy(true); setDraftResult(null);
    try { setDraftResult(await previewProductDraft(draft)); }
    catch (failure) { setError(errorDetail(failure)); }
    finally { setDraftBusy(false); }
  };
  const createDraft = async () => {
    if (isDemo || !draftResult?.valid) return;
    setDraftBusy(true);
    try { const created = await createProductFromDraft(draft); onSelectCase(created.case_id); }
    catch (failure) { setError(errorDetail(failure)); }
    finally { setDraftBusy(false); }
  };

  const statusKey: MessageKey = identity.deployment === "review" ? "v9.review" : loading ? "v10.connectionChecking" : caps?.environment?.execution === "live_sandbox" && caps.environment.ready === true ? "v9.ready" : caps?.environment?.execution === "synthetic_executor" && caps.environment.ready === true ? "v9.synthetic" : "v9.offline";
  const environmentNotice = !isDemo ? environmentAccessNotice(t, caps, selected) : null;
  const environment = <div className="environment-banner" role="status" data-environment="true" data-environment-restriction={environmentNotice?.cause}><span>{environmentNotice ? `${environmentNotice.reason} ${environmentNotice.recovery}` : isDemo ? t("v11.demoBanner") : t(statusKey)}{!isDemo && !environmentNotice && caps?.environment?.execution !== "synthetic_executor" && caps?.environment?.scenario ? ` · ${t(caps.environment.scenario === "B" ? "v9.scenarioB" : "v9.scenarioA")}` : ""}</span><button type="button" className="activity-link" disabled={loading} onClick={() => void refresh()}>{t("v9.reconnect")}</button></div>;
  // Freshness is exposed as data, not as prose on the page: the human-readable
  // form lives inside the collapsed case details.
  if (selectedId) return <div className={`view cases-detail-view${chat ? " with-case-chat" : ""}`} data-cases-view="true" data-cases-mode={isDemo ? "fixture" : "live"} data-cases-ready="true" data-case-read-at={selectedConfirmedAt ?? undefined}>
    {environment}

    {watchExpired && <p role="alert" data-case-watch-expired="true">{t("v10.autoReadStopped")}</p>}
    {detailError && <div role="alert"><p>{t(selected ? "v9.staleCase" : "v9.readError")} {detailError}</p>{!selected && <button className="secondary" onClick={() => onSelectCase(null)}>{t("case.back")}</button>}</div>}
    {selected ? <>
      {!chat && <div>
        <CaseDetail key={selected.case_id} caseFile={selected} mode={isDemo ? "fixture" : "live"} capabilities={caps} caseReadBlocked={!isDemo && (loading || detailError !== "" || watchExpired)} onOperationPendingChange={setOperationPending} evidenceOpen={evidence}
          nextCaseId={next?.case_id ?? null}
          onBack={() => onSelectCase(null)} onAsk={() => onAskCase(selected, isDemo ? "fixture" : "live")}
          onUpdated={updated} onNext={() => onSelectCase(next?.case_id ?? null)}
          onOpenEvidence={() => navigate(caseRoute(selected.case_id, "evidence"))} lastReadAt={selectedConfirmedAt} />
      </div>}
      {chat && <aside className="case-chat-drawer" aria-label={t("caseChat.title")}><CaseChatView key={`${identity.scope_id}:${selected.case_id}`} caseFile={selected} mode={isDemo ? "fixture" : "live"} onBack={() => navigate(caseRoute(selected.case_id))} /></aside>}
    </> : !detailError ? <p>{t("cases.loading")}</p> : null}
  </div>;
  return <div className="view cases-view cases-home" data-cases-view="true" data-cases-mode={isDemo ? "fixture" : "live"} data-cases-ready={!loading}>
    <header className="cases-head"><div><h1>{t("v9.title")}</h1><p className="muted">{t("v9.subtitle")}</p></div></header>
    {environment}
    {welcome ? <section className="cases-welcome" data-first-run-welcome="true">
      <div><strong>{t("v11.welcomeTitle")}</strong><p>{t("v11.welcomeBody")}</p></div>
      <div className="cases-welcome-actions">
        <button type="button" className="secondary" data-open-demo="true" onClick={() => navigate(caseRoute(fixture.case_id))}>{t("v11.welcomeDemo")}</button>
        <button type="button" className="activity-link" onClick={dismissWelcome}>{t("v11.welcomeDismiss")}</button>
      </div>
    </section> : null}
    {!isDemo && caps?.case_create === true ? <details className="cases-draft" data-case-draft="true">
      <summary>{t("v11.draftTitle")}</summary>
      <p className="settings-note">{t("v11.draftNote")}</p>
      <div className="cases-draft-grid">
        <label>{t("v11.draftKind")}
          <SelectMenu
            value={draft.kind}
            ariaLabel={t("v11.draftKind")}
            options={[
              { value: "supplier_cancellation", label: t("v11.draftSupplier") },
              { value: "workcenter_outage", label: t("v11.draftWorkcenter") },
            ]}
            onChange={updateDraft}
          />
        </label>
        <label>{t("v11.draftProduct")}<input value={draft.product_code} onChange={event => setDraft({ ...draft, product_code: event.currentTarget.value })} /></label>
        <label>{t("v11.draftUnits")}<input type="number" min={1} value={draft.units} onChange={event => setDraft({ ...draft, units: Number(event.currentTarget.value) })} /></label>
        <label>{t("v11.draftDue")}<input type="number" min={0} value={draft.due_days} onChange={event => setDraft({ ...draft, due_days: Number(event.currentTarget.value) })} /></label>
      </div>
      <div className="cases-draft-actions">
        <button type="button" className="secondary" disabled={draftBusy} onClick={() => void previewDraft()}>{t("v11.draftPreview")}</button>
        <button type="button" className="primary" disabled={draftBusy || !draftResult?.valid} onClick={() => void createDraft()}>{t("v11.draftCreate")}</button>
      </div>
      {draftResult ? <p role="status" data-draft-result={draftResult.valid ? "valid" : "invalid"}>{draftResult.valid ? t("v11.draftValid", { scenario: draftResult.scenario ?? "—" }) : draftResult.errors.join(" · ")}</p> : null}
    </details> : null}
    <div className="cases-actions">
      {availableScenarios.length > 1 && <SelectMenu
        ariaLabel={t("v9.scenarioSelect")}
        value={selectedScenario?.id || ""}
        options={availableScenarios.map(item => ({ value: item.id, label: t(item.id === "B" ? "v9.scenarioB" : "v9.scenarioA") }))}
        onChange={value => setScenario(value as "A" | "B")}
        disabled={busy || loading}
      />}
      <button className={cases.length ? "secondary" : "primary"} type="button" disabled={!canCreate || busy} onClick={() => void create()} data-create-case="true">{busy ? t("cases.creating") : t(selectedScenario?.id === "B" ? "v9.newExampleB" : "v9.newExample")}</button>
      <button className="secondary" type="button" onClick={() => void refresh()} disabled={loading} data-cases-refresh="true">{t("cases.refresh")}</button>
    </div>
    {/* The note must not disappear with the button: a disabled action needs its reason. */}
    <p className="muted" data-create-note={canCreate ? "available" : "unavailable"}>
      {canCreate
        ? t(caps?.environment?.execution === "synthetic_executor" ? "v9.syntheticExampleNote" : "v9.exampleNote", { scenario: selectedScenario?.id ?? "—" })
        : environmentNotice ? `${environmentNotice.reason} ${environmentNotice.recovery}`
          : caps?.deployment === "hosted" ? `${t("environment.unavailable")} ${t("environment.unavailableRecovery")}`
            : t("v13.createUnavailableNote")}
    </p>
    {isDemo && <div className="cases-demo-banner" data-demo-banner="true">{t("case.fixtureLabel")}</div>}
    {error && <p className="case-error" role="alert">{t("v9.readError")} {error}</p>}
    <div className="cases-filterbar" role="group" aria-label={t("cases.savedViews")}>
      {VIEWS.map(item => <button key={item.id} className={`cases-filter${view === item.id ? " is-active" : ""}`} type="button" data-saved-view={item.id} aria-pressed={view === item.id} onClick={() => setView(item.id)}>{t(item.label)} <span>{effective.filter(row => matches(row, item.id)).length}</span></button>)}
    </div>
    {loading && <p role="status">{t("cases.loading")}</p>}
    {!loading && visible.length === 0 && <div className="cases-empty" data-cases-empty="true"><p>{t(cases.length ? "cases.emptyFilteredTitle" : "v9.noCases")}</p>{view !== "all" && <button className="secondary" onClick={() => setView("all")}>{t("cases.showAll")}</button>}</div>}
    <div className="cases-cards">{visible.map(item => <button key={item.case_id} type="button" className="case-row case-card" data-case-row="true" data-case-id={item.case_id} data-case-state={item.state} onClick={() => onSelectCase(item.case_id)}>
      <span className="case-card-main">
        <strong className="case-card-title">{exceptionText(item, t)}</strong>
        <span className="case-card-meta">{item.order.product} · {formatQuantity(item.order.units)} {t("cases.units")} · {dueWindow(item, t)}</span>
        {/* Two cases can share exception, product, units and due date; the
            record time is what tells them apart without reading the pill. */}
        <span className="case-card-when" data-case-when="true">
          {t("v13.caseRecordedAt", { time: formatDateTime(item.updated_at ?? item.created_at, lang) })}
        </span>
      </span>
      <span className="case-card-side">
        <span className="case-severity is-secondary">{severityLabel(severityOf(item), t)}</span>
        <span className={`case-state-pill is-${isCaseVerified(item) ? "green" : "neutral"}`}>{caseOutcomeLabel(item, t)}</span>
      </span>
    </button>)}</div>
    <p><button type="button" className="activity-link" onClick={onOpenEvidence}>{t("v9.historyEvidence")}</button></p>
    <footer className="cases-disclaimer">{t(identity.deployment === "review" ? "cases.disclaimer" : "cases.disclaimerLocal")}</footer>
  </div>;
}
