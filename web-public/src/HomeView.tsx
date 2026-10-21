import { useEffect, useState } from "react";
import { ChatSurface, useKnowledgeResponder } from "./Assistant";
import { cancelAssistantTurn, getCapabilities, getProductCases, getRuns } from "./api";
import { matchesSavedView } from "./caseUi";
import { useI18n } from "./i18n";
import { navigate, useWorkspace } from "./workspace";
import type { CaseFile, RunSummary } from "./types";

type CardId = "decision" | "executing" | "verified" | "evidence";
type Counts = Record<CardId, number> | null;

/**
 * Start page. The composer is the main surface; everything the product can do
 * without typing lives in one row of small cards underneath it, so the closed
 * loop stays visible without competing with the question box.
 */
export function HomeView() {
  const { t } = useI18n();
  const responder = useKnowledgeResponder();
  const { scope_id } = useWorkspace();
  const [counts, setCounts] = useState<Counts>(null);
  const [countFailed, setCountFailed] = useState(false);
  const [approvalReady, setApprovalReady] = useState<boolean | null>(null);

  const [envChecking, setEnvChecking] = useState(false);

  useEffect(() => {
    let live = true;
    void (async () => {
      const [caseResult, runResult, capResult] = await Promise.allSettled([getProductCases(), getRuns(), getCapabilities()]);
      if (!live) return;
      // Whether approval is possible at all is a deployment fact, so the start
      // page states it instead of letting the reader discover it on a case.
      if (capResult.status === "fulfilled") setApprovalReady(capResult.value.case_approve === true);
      if (caseResult.status === "rejected") {
        setCountFailed(true);
        return;
      }
      const cases = caseResult.value;
      const runs = runResult.status === "fulfilled" ? (runResult.value as RunSummary[]) : null;
      setCounts({
        decision: cases.filter((item: CaseFile) => matchesSavedView(item, "decision")).length,
        executing: cases.filter((item: CaseFile) => matchesSavedView(item, "executing")).length,
        verified: cases.filter((item: CaseFile) => matchesSavedView(item, "verified")).length,
        evidence: runs ? runs.length : 0,
      });
    })();
    return () => { live = false; };
  }, []);

  /** Re-read whether this workspace can approve, without leaving the page. */
  const refreshEnvironment = async () => {
    setEnvChecking(true);
    try {
      const caps = await getCapabilities();
      setApprovalReady(caps.case_approve === true);
    } catch {
      setApprovalReady(null);
    } finally {
      setEnvChecking(false);
    }
  };

  /** The chosen view travels in the route, not in a side channel. */
  const openCases = (view: "decision" | "executing" | "verified") => {
    navigate(`#cases&view=${view}`);
  };

  // In a read-only workspace the decision card still shows the backlog, but it
  // says up front that acting on it is not possible here.
  const decisionHint = approvalReady === false ? t("home.cardDecisionHintReadOnly") : t("home.cardDecisionHint");
  const cards: { id: CardId; title: string; hint: string; unit: "cases" | "runs"; onOpen: () => void }[] = [
    { id: "decision", title: t("home.cardDecision"), hint: decisionHint, unit: "cases", onOpen: () => openCases("decision") },
    { id: "executing", title: t("home.cardExecuting"), hint: t("home.cardExecutingHint"), unit: "cases", onOpen: () => openCases("executing") },
    { id: "verified", title: t("home.cardVerified"), hint: t("home.cardVerifiedHint"), unit: "cases", onOpen: () => openCases("verified") },
    { id: "evidence", title: t("home.cardEvidence"), hint: t("home.cardEvidenceHint"), unit: "runs", onOpen: () => navigate("#runs") },
  ];
  const environment = approvalReady === null
    ? { tone: "unknown", text: t("home.envUnknown") }
    : approvalReady
      ? { tone: "ready", text: t("home.envReady") }
      : { tone: "readonly", text: t("home.envReadOnly") };

  return (
    <div className="view home-view" data-knowledge-view="true" data-home-view="true">
      <section className="home-hero">
        <span className={`home-env is-${environment.tone}`} data-home-environment={environment.tone} role="status">
          {environment.text}
          <button
            type="button"
            className="home-env-refresh"
            data-home-env-refresh="true"
            onClick={refreshEnvironment}
            disabled={envChecking}
          >
            {envChecking ? t("home.envChecking") : t("home.envRefresh")}
          </button>
        </span>
        <h1>{t("home.title")}</h1>
        <p className="home-lede">{t("home.subtitle")}</p>
      </section>

      <ChatSurface
        responder={responder}
        cancelTurn={cancelAssistantTurn}
        storageScope={`${scope_id}:knowledge`}
        presets={[
          { icon: "cases", label: t("home.preset1Title"), prompt: t("home.preset1") },
          { icon: "check", label: t("home.preset2Title"), prompt: t("home.preset2") },
          { icon: "shield", label: t("home.preset3Title"), prompt: t("home.preset3") },
          { icon: "server", label: t("home.preset4Title"), prompt: t("home.preset4") },
        ]}
        presetsTitle={t("home.presetsTitle")}
        emptyText={t("home.empty")}
        composerPlaceholder={t("home.composerPlaceholder")}
        composerAria={t("home.composerAria")}
        composerNote={t("assistant.composerNote")}
        className="home-chat"
      />

      <section className="home-cards" aria-label={t("home.cardsTitle")} data-home-cards="true">
        <h2 className="home-cards-title">{t("home.cardsTitle")}</h2>
        <div className="home-cards-row">
          {cards.map((card) => {
            const value = counts ? counts[card.id] : null;
            return (
              <button
                key={card.id}
                type="button"
                className="home-card"
                data-home-card={card.id}
                onClick={card.onOpen}
              >
                <span className="home-card-head">
                  <span className="home-card-title">{card.title}</span>
                  <span className="home-card-count" data-home-card-count={card.id}>
                    {counts
                      ? t(
                        card.unit === "runs"
                          ? value === 1 ? "home.cardRunsOne" : "home.cardRuns"
                          : value === 1 ? "home.cardCountOne" : "home.cardCount",
                        { count: String(value) },
                      )
                      : countFailed
                        ? t("home.cardFailed")
                        : t("home.cardLoading")}
                  </span>
                </span>
                <span className="home-card-hint">{card.hint}</span>
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
}
