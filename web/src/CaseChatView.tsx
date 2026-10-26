import { useMemo } from "react";
import { useWorkspace } from "./workspace";
import { ApiError, askProductCase, streamProductCase, cancelCaseTurn } from "./api";
import {
  ChatSurface,
  type ChatAnswerMeta,
  type ChatReply,
  type ChatResponder,
} from "./Assistant";
import { useI18n } from "./i18n";
import { localCaseAnswer, caseOutcomeLabel, stateLabel, formatDateTime } from "./caseUi";
import { answerContextKind, caseAnswerContext, readAnswerContext, type AnswerContextNotice } from "./caseAnswerContext";
import type { CaseFile } from "./types";
import { readCaseCitations, readCaseEvidenceUsage, readCaseReferenceLabels, readCaseSourceMode } from "./caseAnswerEvidence";

type Props = {
  caseFile: CaseFile;
  mode: "live" | "fixture";
  onBack: () => void;
};

/**
 * Case-scoped chat as the main content surface. The first thing a reviewer
 * clicks should fill the screen, not open a narrow right rail.
 */
export function CaseChatView({ caseFile, mode, onBack }: Props) {
  const { t, lang } = useI18n();
  const { scope_id } = useWorkspace();
  // Only shorten this known case's prose references. Stored replies, Markdown
  // links, literal URLs, and structured evidence targets retain their full IDs.
  const displayAnswerText = useMemo(() => {
    const fullId = caseFile.case_id;
    const protectedOrId = new RegExp(String.raw`\[[^\]]*\]\([^)]*\)|<[^>]*>|(?:https?:\/\/|\/|#)[^\s<>]*|${fullId}`, "g");
    const withoutReference = /(?:^|\n)(?:Reference|参考|Case|案例)\s*[:：].*(?:\n|$)/gi;
    const caseLabel = lang === "zh" ? "本案例" : "this case";
    return (text: string) => text.replace(withoutReference, "").replace(protectedOrId, match => match === fullId ? caseLabel : match);
  }, [caseFile.case_id, lang]);

  const answerContextNotice = useMemo(() => (meta: ChatAnswerMeta | undefined): AnswerContextNotice => {
    const current = caseAnswerContext(caseFile);
    const kind = answerContextKind(meta?.caseContext, current);
    const recorded = readAnswerContext(meta?.caseContext);
    if (kind === "unknown" || !recorded) return { kind: "unknown", text: t("caseChat.answerUnknown") };
    const outcomes: Record<string, Parameters<typeof t>[0]> = {
      verified: "v9.verified", simulation_passed: "v9.simulatedPassed",
      execution_unknown: "v9.executionUnknown", verification_unknown: "v9.verificationUnknown",
      verification_failed: "v9.verificationFailed",
    };
    const state = recorded.selection_pending ? t("candidate.pendingTitle")
      : recorded.outcome && outcomes[recorded.outcome] ? t(outcomes[recorded.outcome]) : stateLabel(recorded.state, t);
    return { kind, text: t(kind === "earlier" ? "caseChat.answerEarlier" : "caseChat.answerRecorded", {
      version: recorded.version ?? "—", state, at: formatDateTime(recorded.updated_at, lang),
      currentVersion: current.version ?? "—", currentState: caseOutcomeLabel(caseFile, t),
    }) };
  }, [caseFile, lang, t]);

  const responder = useMemo<ChatResponder>(
    () => async (question, _history, trace, signal, conversation, turn) => {
      if (mode !== "fixture") {
        try {
          let response;
          try {
            response = turn && trace ? await streamProductCase(caseFile.case_id, question, conversation ?? "default", trace, signal, turn, lang) : await askProductCase(caseFile.case_id, question, conversation, signal, lang);
          } catch (failure) {
            if (!turn || !trace || turn.resume || signal?.aborted || !(failure instanceof ApiError && ([404, 405, 501].includes(failure.status) || failure.status === 503 && failure.message === "stream_unavailable"))) throw failure;
            response = await askProductCase(caseFile.case_id, question, conversation, signal, lang);
          }
          const text =
            typeof response.answer === "string" ? response.answer.trim() : "";
          if (response.found !== false && text) {
            const sourceMode = readCaseSourceMode(response.source_mode);
            const activity = Array.isArray(response.activity) ? {
              active: false,
              startedAt: typeof response.elapsed_ms === "number" && Number.isFinite(response.elapsed_ms) ? Date.now() - Math.max(0, response.elapsed_ms) : 0,
              finishedAt: Date.now(),
              hideDuration: typeof response.elapsed_ms !== "number" || !Number.isFinite(response.elapsed_ms),
              steps: response.activity.filter(step => step.kind !== "think" && step.kind !== "reasoning"),
            } : undefined;
            const reply: ChatReply = {
              text,
              meta: {
                mode: "case",
                caseContext: readAnswerContext(response.case_context) ?? undefined,
                sourceMode,
                note: response.fallback_reason === "model_unavailable" ? t("caseChat.fallbackUnavailable")
                  : response.fallback_reason === "model_evidence_mismatch" ? t("caseChat.fallbackMismatch")
                  : response.fallback_reason === "model_empty" ? t("caseChat.fallbackEmpty") : undefined,
                caseCitations: readCaseCitations(response.citations),
                relatedCaseLabels: readCaseReferenceLabels(response.citations),
                evidenceUsage: readCaseEvidenceUsage(response.evidence_usage),
                activity,
                citations: [],
                passages: [],
                evidenceRefs: response.evidence_refs,
              },
            };
            return reply;
          }
        } catch (failure) {
          // A stale local record must never hide revoked access, a missing case,
          // conflicting turn, or another explicit server rejection.
          const unsupported = failure instanceof ApiError && [405, 501].includes(failure.status);
          const networkFailure = failure instanceof TypeError;
          if (signal?.aborted || turn?.resume || (!unsupported && !networkFailure)) throw failure;
        }
      }
      const local = localCaseAnswer(caseFile, question, t, mode === "fixture");
      return {
        text: local.text,
        meta: {
          mode: "case",
          caseContext: caseAnswerContext(caseFile),
          note: mode === "fixture" ? t("caseChat.demoNote") : t("caseChat.localNote"),
          sourceMode: "local",
          relatedCaseLabels: local.citations,
          citations: [],
          passages: [],
        },
      };
    },
    [caseFile, mode, t, lang],
  );

  return (
    <div className="view case-chat-page"
      data-case-chat="true" data-case-chat-view="true">
      <header className="case-chat-page-head">
        <button className="case-chat-back" type="button" onClick={onBack}>
          ← {t("caseChat.back")}
        </button>
        <div className="case-chat-page-title">
          <h1>{t("caseChat.title")}</h1>
          <div className="case-chat-page-context">
            <span>{caseFile.order.product}</span>
            <span>{caseFile.order.units} {t("cases.units")}</span>
            <span>{caseOutcomeLabel(caseFile, t)}</span>
            {mode === "fixture" ? <span className="case-chat-demo">{t("cases.demoBanner")}</span> : null}
          </div>
        </div>
      </header>

      <div className="case-chat-page-canvas">
        <ChatSurface
          responder={responder}
          displayAnswerText={displayAnswerText}
          answerContextNotice={answerContextNotice}
          historyContextWarning={t("caseChat.historyContextWarning")}
          cancelTurn={(conversation, turnId) => mode === "fixture" ? Promise.resolve() : cancelCaseTurn(caseFile.case_id, conversation, turnId)}
          storageScope={`${scope_id}:case:${caseFile.case_id}`}
          presets={[
            t("caseChat.presetImpact"),
            t("caseChat.presetPlan"),
            t("caseChat.presetExecution"),
          ]}
          presetsTitle={t("caseChat.presetsTitle")}
          emptyText={t("caseChat.empty")}
          composerPlaceholder={t("caseChat.placeholder")}
          composerHint={t("caseChat.hint")}
          composerNote={t(mode === "fixture" ? "v9.scopeDemo" : "v9.scope")}
          composerAria={t("caseChat.title")}
          className="case-chat-surface case-chat-page-surface"
        />
      </div>
    </div>
  );
}
