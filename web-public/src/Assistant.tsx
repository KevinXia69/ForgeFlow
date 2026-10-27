import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MarkdownText } from "@forgeflow/dsh-primitives";
import { ApiError, askAssistant, streamAssistant, cancelAssistantTurn, type StreamTurn } from "./api";
import { newConversationId, newStreamTurnId, readConversations, writeConversations, type PendingChatTurn } from "./conversation";
import { useWorkspace } from "./workspace";
import { ActivityPanel } from "./Activity";
import { DshComposer } from "./dsh/Composer";
import chatCss from "./dsh/ChatView.module.css";
import { useI18n, type Translate, type MessageKey } from "./i18n";
import { isModelConfigComplete, loadModelConfig } from "./modelConfig";
import type {
  ActivityState,
  CaseAnswerContext,
  CaseSourceMode,
  CaseEvidenceUsage,
  ProductAskCitation,
  Answer,
  AssistantHistoryTurn,
  Passage,
} from "./types";

import type { AnswerContextNotice } from "./caseAnswerContext";
import { caseCitationValue, readCaseCitations, readCaseEvidenceUsage, readCaseReferenceLabels, readCaseSourceMode } from "./caseAnswerEvidence";

const MAX_HISTORY_TURNS = 6;
const MAX_HISTORY_CHARS = 1000;

export type ChatAnswerMeta = {
  mode: "model" | "retrieval" | "case" | "harness";
  warning?: string;
  note?: string;
  citations: string[];
  passages: Passage[];
  activity?: ActivityState;
  scope?: string;
  caseContext?: CaseAnswerContext;
  sourceMode?: CaseSourceMode;
  caseCitations?: ProductAskCitation[];
  relatedCaseLabels?: string[];
  evidenceUsage?: CaseEvidenceUsage;
  evidenceRefs?: { kind: string; id: string; href: string; label: string }[];
};

export type ChatReply = {
  text: string;
  meta: ChatAnswerMeta;
};

export type ChatResponder = (
  question: string,
  history: AssistantHistoryTurn[],
  onTrace?: (activity: ActivityState) => void,
  signal?: AbortSignal,
  conversation?: string,
  turn?: StreamTurn,
) => Promise<ChatReply>;

type AnswerWithMode = Answer & { mode?: "model" | "retrieval" | "harness" };

type UiMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  meta?: ChatAnswerMeta;
};

function formatAnswer(answer: AnswerWithMode, t: Translate): string {
  if (!answer.found || answer.passages.length === 0) {
    return t("assistant.answerNoPassages");
  }
  const parts = [t("assistant.answerQuoteLead")];
  for (const passage of answer.passages) {
    parts.push(
      `\n${passage.path}:${passage.start_line}-${passage.end_line}\n${passage.text}`,
    );
  }
  return parts.join("\n");
}

function curateHistory(
  messages: readonly UiMessage[],
  question: string,
): AssistantHistoryTurn[] {
  const turns: AssistantHistoryTurn[] = [];
  for (const message of messages) {
    const content = message.text.slice(0, MAX_HISTORY_CHARS).trim();
    if (content) turns.push({ role: message.role, content });
  }
  const recent = turns.slice(-MAX_HISTORY_TURNS);
  while (
    recent.length > 0 &&
    recent[recent.length - 1].role === "user" &&
    recent[recent.length - 1].content === question
  ) {
    recent.pop();
  }
  return recent;
}

function answerCitations(meta: ChatAnswerMeta): string[] {
  const labels =
    meta.citations.length > 0
      ? meta.citations
      : meta.passages.map(
          (passage) => `${passage.path}:${passage.start_line}-${passage.end_line}`,
        );
  return [...new Set(labels.filter(Boolean))];
}

function newId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `m-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }
}

function AssistantMessage({ message, displayText, contextNotice }: {
  message: UiMessage; displayText?: (text: string) => string; contextNotice?: AnswerContextNotice;
}) {
  const { t } = useI18n();
  const meta = message.meta;
  // Legacy saved harness messages did not record whether duration was measured;
  // their timestamps can be a fabricated zero. Keep the reply but hide that time.
  const activity = meta?.activity && meta.mode === "harness" && meta.activity.hideDuration === undefined
    ? { ...meta.activity, hideDuration: true } : meta?.activity;
  const isCase = meta?.mode === "case";
  const sourceMode = readCaseSourceMode(meta?.sourceMode);
  const factCitations = isCase ? readCaseCitations(meta?.caseCitations) : [];
  const caseLabels = isCase ? readCaseReferenceLabels(meta?.relatedCaseLabels ?? meta?.citations) : [];
  const topicLabels: Record<string, MessageKey> = { status: "caseChat.topicStatus", actions: "caseChat.topicActions", preserved: "caseChat.topicPreserved", costs: "caseChat.topicCosts", execution: "caseChat.topicExecution", delivery: "caseChat.topicDelivery" };
  const statusLabels: Record<string, MessageKey> = { available: "caseChat.coverageAvailable", not_recorded: "caseChat.coverageMissing", omitted: "caseChat.coverageOmitted", unavailable: "caseChat.coverageUnavailable" };
  const usage = isCase ? readCaseEvidenceUsage(meta?.evidenceUsage) : undefined;
  const evidenceRefs = Array.isArray(meta?.evidenceRefs) ? meta.evidenceRefs.filter(ref => ref && ["case", "run"].includes(ref.kind) && /^[A-Za-z0-9_.-]+$/.test(ref.id)) : [];
  const citations = meta && !isCase && (meta.mode === "model" || meta.mode === "harness")
    ? answerCitations(meta)
    : [];
  const labels = useMemo(
    () => ({
      code: { copyLabel: t("assistant.copyCode"), copiedLabel: t("assistant.copiedCode") },
      footnotes: t("assistant.footnotes"),
    }),
    [t],
  );
  return (
    <div className="msg assistant">
      {activity && <ActivityPanel state={activity} lang={useI18n().lang} />}
      {meta?.warning ? (
        <p className="assistant-fallback">{t("assistant.modelFallback")}</p>
      ) : null}
      {isCase && <p className="case-answer-context" data-case-answer-source={sourceMode ?? "unknown"}>{t(sourceMode === "model" ? "caseChat.sourceModel" : sourceMode === "deterministic" ? "caseChat.sourceDeterministic" : sourceMode === "local" ? "caseChat.sourceLocal" : "caseChat.sourceUnknown")}</p>}
      {contextNotice && <p className="case-answer-context" data-case-answer-context={contextNotice.kind}>{contextNotice.text}</p>}
      <div className="assistant-markdown">
        <MarkdownText text={displayText ? displayText(message.text) : message.text} streaming={false} labels={labels} />
      </div>
      {meta?.note ? <p className="assistant-fallback">{meta.note}</p> : null}
      {isCase && factCitations.length > 0 && <section data-case-fact-citations="true" aria-label={t("caseChat.factCitations")}>
        <details><summary>{t(sourceMode ? "caseChat.factCitations" : "caseChat.legacyFactCitations")} ({factCitations.length})</summary>
        {usage?.source === "case_snapshot" && <p className="assistant-fallback">{t("caseChat.snapshotFacts")}</p>}
        <ul className="answer-citations">
          {factCitations.map((citation, index) => <li className="citation" key={`${citation.field}:${index}`}>
            <details><summary>{citation.label}: {caseCitationValue(citation, t("runs.notRecorded"))}</summary>
              <p>{t("caseChat.evidenceField")}: <code>{citation.field}</code></p>
              {(citation.snapshot_id || usage?.snapshot_id) && <p>{t("caseChat.evidenceSnapshot")}: <code>{citation.snapshot_id || usage?.snapshot_id}</code></p>}
            </details>
          </li>)}
        </ul></details>
      </section>}
      {isCase && Boolean(usage?.coverage?.length) && <details data-case-coverage="true">
        <summary>{t("caseChat.coverage")}</summary>
        <ul>{usage?.coverage?.map((item, index) => <li key={index}>{t(topicLabels[item.topic ?? ""] ?? "caseChat.topicOther")}: {t(statusLabels[item.status] ?? "caseChat.coverageUnknown")}{item.shown !== undefined && item.total !== undefined ? ` (${t("caseChat.coverageCounts", { shown: item.shown, total: item.total })})` : ""}</li>)}</ul>
      </details>}
      {(evidenceRefs.length > 0 || caseLabels.length > 0) && <section data-related-evidence="true" aria-label={t("caseChat.relatedEvidence")}>
        <p>{t("caseChat.relatedEvidence")}</p>
        {isCase && <p className="assistant-fallback">{t("caseChat.relatedEvidenceNote")}</p>}
        {evidenceRefs.map(ref => <a className="citation" key={`${ref.kind}:${ref.id}`} href={`#${ref.kind}=${encodeURIComponent(ref.id)}${ref.kind === "case" ? "&evidence=1" : ""}`}>{ref.label}</a>)}
        {caseLabels.length > 0 && <ul className="answer-citations">{caseLabels.map((label, index) => <li className="citation" key={index}>{label}</li>)}</ul>}
      </section>}
      {citations.length > 0 ? (
        <ul className="answer-citations" aria-label={t("assistant.citationsAria")}>
          {citations.map((citation) => (
            <li key={citation} className="citation"><details><summary>{citation}</summary>{meta?.passages.filter(passage => citation.startsWith(`${passage.path}:`)).length ? meta.passages.filter(passage => citation.startsWith(`${passage.path}:`)).map(passage => <pre key={`${passage.path}:${passage.start_line}`}>{passage.path}:{passage.start_line}–{passage.end_line}{"\n"}{passage.text}</pre>) : <p>{t("v9.referenceMissing")}</p>}</details></li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export type ChatPresetIcon = "cases" | "shield" | "server" | "check";

export type ChatPreset =
  | string
  | {
      label: string;
      prompt: string;
      description?: string;
      icon?: ChatPresetIcon;
    };

export type ChatSurfaceProps = {
  responder: ChatResponder;
  presets?: ChatPreset[];
  presetsTitle?: string;
  emptyText: string;
  composerPlaceholder: string;
  composerHint?: string;
  composerNote?: string;
  composerAria?: string;
  className?: string;
  storageScope: string;
  displayAnswerText?: (text: string) => string;
  answerContextNotice?: (meta: ChatAnswerMeta | undefined) => AnswerContextNotice;
  historyContextWarning?: string;
  cancelTurn?: (conversation: string, turnId: string) => Promise<void>;
};

/** Shared DSH-rendered conversation surface used by Ask and CaseChat. */
export function ChatSurface({
  responder,
  presets = [],
  presetsTitle,
  emptyText,
  composerPlaceholder,
  composerHint,
  composerNote,
  composerAria,
  className = "",
  storageScope,
  displayAnswerText,
  answerContextNotice,
  historyContextWarning,
  cancelTurn,
}: ChatSurfaceProps) {
  const { t, lang } = useI18n();
  const [store, setStore] = useState(() => readConversations(storageScope));
  const current = store.sessions.find(item => item.id === store.active)!;
  const [messages, setMessages] = useState<UiMessage[]>(() => current.messages as UiMessage[]);
  const [storageFailed, setStorageFailed] = useState(false);
  const [interrupted, setInterrupted] = useState(current.pending && !current.pendingTurn);
  const [pendingTurn, setPendingTurn] = useState<PendingChatTurn | null>(current.pendingTurn ?? null);
  const [stopError, setStopError] = useState(false);
  const requestSequence = useRef(0);
  const stopRequested = useRef(false);
  const [search, setSearch] = useState("");
  const controller = useRef<AbortController | null>(null);


  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [liveActivity, setLiveActivity] = useState<ActivityState | null>(null);
  const [following, setFollowing] = useState(true);
  useEffect(() => {
    const updated = { ...store, sessions: store.sessions.map(item => item.id === store.active ? { ...item, messages, pending: busy || pendingTurn !== null, pendingTurn, title: messages.find(m => m.role === "user")?.text.slice(0, 60) || item.title, updated: Date.now() } : item) };
    setStorageFailed(!writeConversations(storageScope, updated));
  }, [messages, busy, pendingTurn, store, storageScope]);
  const switchConversation = (id?: string) => {
    if (busy) return;
    const saved = readConversations(storageScope);
    const sessionId = id || newConversationId();
    const sessions = id ? saved.sessions : [{ id: sessionId, title: "", messages: [], pending: false, updated: Date.now() }, ...saved.sessions];
    const next = { active: sessionId, sessions };
    const session = sessions.find(item => item.id === sessionId)!;
    setStore(next); setMessages(session.messages as UiMessage[]); setInterrupted(session.pending && !session.pendingTurn); setPendingTurn(session.pendingTurn ?? null); setInput(""); setSearch("");
    setStorageFailed(!writeConversations(storageScope, next));
    if (session.pendingTurn) void runTurn(session.pendingTurn, true, sessionId, session.messages as UiMessage[]);
  };
  const viewportRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !following) return;
    viewport.scrollTop = viewport.scrollHeight;
  }, [messages, liveActivity, following]);

  async function runTurn(turn: PendingChatTurn, resume: boolean, conversation = store.active, prior = messages) {
    const sequence = ++requestSequence.current;
    const request = new AbortController();
    controller.current = request;
    stopRequested.current = false;
    setStopError(false); setBusy(true); setInterrupted(false); setPendingTurn(turn);
    setLiveActivity({ active: true, startedAt: Date.now(), hideDuration: true, steps: [{ id: "pending", kind: "think", label: t("assistant.analyzing"), status: "active" }] });
    try {
      const reply = await responder(turn.question, curateHistory(prior, turn.question), activity => { if (sequence === requestSequence.current) setLiveActivity(activity); }, request.signal, conversation, {
        id: turn.id, lastEventId: turn.lastEventId, lang: turn.lang, resume,
        onCursor: id => { if (sequence === requestSequence.current) setPendingTurn(value => value?.id === turn.id ? { ...value, lastEventId: id } : value); },
      });
      if (sequence !== requestSequence.current) return;
      setMessages(value => [...value, { id: newId(), role: "assistant", text: stopRequested.current ? t("v9.stopped") : reply.text, meta: stopRequested.current ? { mode: "retrieval", citations: [], passages: [] } : reply.meta }]);
    } catch (error) {
      if (sequence !== requestSequence.current) return;
      const unavailable = error instanceof ApiError && [409, 410].includes(error.status);
      const upgradeRequired = error instanceof ApiError && error.status === 410 && error.message === "turn_id_upgrade_required";
      const expired = error instanceof ApiError && error.status === 410 && error.message === "turn_expired";
      const cancelled = error instanceof ApiError && error.status === 409 && error.message === "turn_cancelled";
      const detail = error instanceof Error && error.message ? error.message : "unknown";
      // Admission rejected this new request before generation. Keep its question
      // editable, but never replace the identity and retry it automatically.
      if (upgradeRequired && !resume && !stopRequested.current) setInput(turn.question);
      const failure = upgradeRequired ? t("v9.turnUpgradeRequired") : expired ? t("v9.turnExpired") : cancelled ? t("v9.turnCancelled") : unavailable && resume ? t("v9.resumeUnavailable") : t("assistant.answerFailed", { detail });
      setMessages(value => [...value, { id: newId(), role: "assistant", text: stopRequested.current ? t("v9.stopped") : failure, meta: { mode: "retrieval", citations: [], passages: [] } }]);
    } finally {
      if (sequence === requestSequence.current) {
        controller.current = null; setLiveActivity(null); setBusy(false); setPendingTurn(null);
        requestAnimationFrame(() => { const viewport = viewportRef.current; if (viewport) viewport.scrollTop = viewport.scrollHeight; });
      }
    }
  }

  // Unmount only detaches this subscriber. The saved turn can reconnect without
  // another model call; an explicit Stop separately cancels its server output.
  useEffect(() => {
    if (current.pendingTurn) void runTurn(current.pendingTurn, true);
    return () => { requestSequence.current++; controller.current?.abort(); controller.current = null; };
  }, []);

  async function send(question: string) {
    const trimmed = question.trim();
    if (!trimmed || busy) return;
    setMessages(value => [...value, { id: newId(), role: "user", text: trimmed }]);
    setInput(""); setFollowing(true);
    await runTurn({ id: newStreamTurnId(), question: trimmed, lastEventId: "", lang }, false);
  }

  async function stop() {
    if (!pendingTurn || stopRequested.current) return;
    stopRequested.current = true;
    const request = controller.current;
    try { await cancelTurn?.(store.active, pendingTurn.id); }
    catch { setStopError(true); }
    finally { request?.abort(); }
  }

  // The ask surface publishes a slot in the sidebar; the case chat drawer keeps
  // the controls inline because it has no sidebar of its own.
  const [historySlot, setHistorySlot] = useState<HTMLElement | null>(null);
  useEffect(() => {
    const find = () => setHistorySlot(document.querySelector<HTMLElement>("[data-chat-history-slot]"));
    find();
    const observer = new MutationObserver(find);
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, []);

  // A thread row is a label, not the question. Long preset prompts used to be
  // printed in full and read like a paragraph inside the sidebar.
  const shortTitle = (text: string) => {
    const clean = (text || "").replace(/\s+/g, " ").trim().replace(/[。？！?!.]+$/, "");
    return clean.length > 16 ? `${clean.slice(0, 16)}…` : clean;
  };
  // A conversation started from a suggestion is labelled with the suggestion's
  // short label, not with the longer prompt it sends.
  const presetLabel = (text: string) => {
    for (const preset of presets) {
      const item = typeof preset === "string" ? { label: preset, prompt: preset } : preset;
      if (item.prompt === text) return item.label;
    }
    return "";
  };
  const threadTitle = (id: string) => {
    const raw = id === store.active
      ? messages.find(m => m.role === "user")?.text ?? ""
      : store.sessions.find(item => item.id === id)?.title ?? "";
    return presetLabel(raw) || shortTitle(raw) || t("sidebar.newChat");
  };
  // Only conversations that hold something are listed. An empty one is not a
  // place to switch back to, and titling it "New chat" would collide with the
  // action of the same name.
  const listedThreads = store.sessions.filter(item =>
    item.id === store.active ? messages.length > 0 : (item.messages?.length ?? 0) > 0);

  const toolbar = (
    <div className="chat-history-toolbar" data-chat-history-toolbar="true">
      <button type="button" className="chat-new" aria-label={t("v9.newConversation")} disabled={busy} onClick={() => switchConversation()} data-chat-new="true">
        <span className="chat-icon" aria-hidden="true">＋</span>
        <span>{t("v9.newConversation")}</span>
      </button>
      {listedThreads.length > 0 || messages.length > 0 ? (
        <>
          <label className="chat-search">
            <span className="chat-icon" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
                <circle cx="7" cy="7" r="4.4" stroke="currentColor" strokeWidth="1.4" />
                <path d="M10.4 10.4 14 14" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            </span>
            <input type="search" aria-label={t("sidebar.searchChats")} placeholder={t("sidebar.searchChats")} value={search} onChange={event => setSearch(event.target.value)} />
          </label>
          <ul className="chat-threads" aria-label={t("v9.conversationHistory")}>
            {listedThreads
              .filter(item => !search || threadTitle(item.id).toLowerCase().includes(search.toLowerCase()))
              .map(item => (
              <li key={item.id}>
                <button
                  type="button"
                  className={item.id === store.active ? "chat-thread is-active" : "chat-thread"}
                  aria-current={item.id === store.active ? "true" : undefined}
                  disabled={busy}
                  onClick={() => switchConversation(item.id)}
                  title={threadTitle(item.id)}
                >
                  <span>{threadTitle(item.id)}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );

  return (
    <div className={`chat-surface ${className}`.trim()}>
      {historySlot ? createPortal(toolbar, historySlot) : toolbar}
      {historyContextWarning && messages.some(message => message.role === "assistant"
        && answerContextNotice?.(message.meta).kind !== "recorded") &&
        <p className="case-answer-history-warning" role="status" data-case-history-context="true">{historyContextWarning}</p>}
      {storageFailed && <p role="status">{t("v9.storageFailed")}</p>}
      {interrupted && <p role="status">{t("v9.historyInterrupted")}</p>}
      {stopError && <p role="status">{t("v9.stopUnconfirmed")}</p>}
      <div className={chatCss.frame}>
        <div className={chatCss.root} data-chat-following-tail={following ? "" : undefined}>
          <div
            className={chatCss.scroll}
            ref={viewportRef}
            onScroll={(event) => {
              const el = event.currentTarget;
              setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
            }}
          >
            <div className={chatCss.column}>
              {messages.length === 0 && presets.length === 0 ? <p className="muted">{emptyText}</p> : null}
              {messages.map((message) => message.role === "user" ? (
                <div className="msg user" key={message.id}><p>{message.text}</p></div>
              ) : (
                <AssistantMessage message={message} displayText={displayAnswerText} contextNotice={answerContextNotice?.(message.meta)} key={message.id} />
              ))}
              {liveActivity ? (
                <div className="live-reasoning" data-live-reasoning="true">
                  <ActivityPanel state={liveActivity} lang={lang} />
                </div>
              ) : null}
            </div>
          </div>
        </div>
        {!following && messages.length > 0 ? (
          <div className={chatCss.toBottomSlot}>
            <button
              type="button"
              className={chatCss.toBottom}
              aria-label={t("assistant.toBottom")}
              onClick={() => {
                setFollowing(true);
                const viewport = viewportRef.current;
                if (viewport) viewport.scrollTop = viewport.scrollHeight;
              }}
            >↓</button>
          </div>
        ) : null}
      </div>

        {messages.length === 0 && presets.length > 0 ? (
          <div className="chat-presets" data-chat-presets="true">
            {presetsTitle ? <span className="chat-presets-title">{presetsTitle}</span> : null}
            <div className="chat-presets-list">
              {presets.map((preset) => {
                const item = typeof preset === "string" ? { label: preset, prompt: preset } : preset;
                return (
                  <button
                    key={item.prompt}
                    type="button"
                    className={`chat-preset${item.description ? " is-rich" : ""}`}
                    onClick={() => void send(item.prompt)}
                  >
                    {item.icon ? <span className="chat-preset-icon" aria-hidden="true"><PresetIcon name={item.icon} /></span> : null}
                    <span className="chat-preset-copy">
                      {item.description ? <><strong>{item.label}</strong><small>{item.description}</small></> : item.label}
                    </span>
                    {item.description ? <span className="chat-preset-arrow" aria-hidden="true">→</span> : null}
                  </button>
                );
              })}
            </div>
          </div>
        ) : null}

        <div className="composer composer-compact">
          <DshComposer
            value={input}
            ariaLabel={composerAria ?? t("assistant.composerAria")}
            placeholder={composerPlaceholder}
            disabled={busy}
            onChange={setInput}
            onSend={() => void send(input)}
          />
          <div className="composer-bar">
            {composerHint ? <span className="composer-hint">{composerHint}</span> : <span />}
            {busy && <button type="button" className="secondary" onClick={() => void stop()}>{t("v9.stop")}</button>}
            <button
              className="send"
              type="button"
              aria-label={t("assistant.send")}
              disabled={busy || input.trim() === ""}
              onClick={() => void send(input)}
            >
              ↑
            </button>
          </div>
        </div>
        {composerNote ? <p className="composer-note">{composerNote}</p> : null}
      </div>
  );
}

export function useKnowledgeResponder(): ChatResponder {
  const { t, lang } = useI18n();
  const { scope_id, deployment } = useWorkspace();
  return useMemo<ChatResponder>(
    () => async (question, history, onTrace, signal, conversation, turn) => {
      const stored = loadModelConfig(scope_id, deployment);
      const model = isModelConfigComplete(stored) ? stored : undefined;
      let answer: AnswerWithMode;
      if (turn?.resume && (model || !onTrace)) throw new ApiError("turn_unavailable", 410);
      if (model || !onTrace) {
        answer = await askAssistant(
          question,
          model,
          history.length > 0 ? history : undefined,
          conversation,
          lang,
          signal,
        );
      } else {
        try {
          answer = await streamAssistant(
            question,
            history.length > 0 ? history : undefined,
            conversation ?? "default",
            lang,
            onTrace,
            signal,
            turn,
          );
        } catch (failure) {
          if (signal?.aborted || turn?.resume || !(failure instanceof ApiError && failure.status === 503 && failure.message === "stream_unavailable")) throw failure;
          answer = await askAssistant(
            question,
            undefined,
            history.length > 0 ? history : undefined,
            conversation,
            lang,
            signal,
          );
        }
      }
      const mode: "model" | "retrieval" | "harness" =
        answer.mode === "harness" || answer.mode === "model" ? answer.mode : "retrieval";
      const text = mode === "model" || mode === "harness"
        ? answer.answer?.trim() || t("assistant.answerNoPassages")
        : formatAnswer(answer, t);
      const harnessActivity = mode === "harness" && Array.isArray(answer.activity)
        ? {
            active: false,
            startedAt: typeof answer.elapsed_ms === "number" ? Date.now() - Math.max(0, answer.elapsed_ms) : 0,
            hideDuration: typeof answer.elapsed_ms !== "number" || !Number.isFinite(answer.elapsed_ms),
            finishedAt: Date.now(),
            outcome: "success" as const,
            steps: answer.activity.filter(step => step.kind !== "think" && step.kind !== "reasoning").map((step, index) => ({
              id: `harness-${index}`,
              kind: step.kind,
              label: step.label,
              detail: step.detail,
              status: step.status,
            })),
          }
        : undefined;
      return {
        text,
        meta: {
          mode,
          warning: answer.warning,
          citations: Array.isArray(answer.citations) ? answer.citations : [],
          passages: Array.isArray(answer.passages) ? answer.passages : [],
          activity: harnessActivity,
          scope: answer.scope,
          evidenceRefs: answer.evidence_refs,
        },
      };
    },
    [t, lang, scope_id, deployment],
  );
}

function PresetIcon({ name }: { name: ChatPresetIcon }) {
  const paths: Record<ChatPresetIcon, React.ReactNode> = {
    cases: (<><rect x="3.5" y="4.5" width="13" height="11" rx="2" /><path d="M7 8h6M7 11h4" /></>),
    shield: (<><path d="M10 3.2 15.5 5v4.1c0 3.4-2.2 5.9-5.5 7.2-3.3-1.3-5.5-3.8-5.5-7.2V5L10 3.2Z" /><path d="m7.8 9.7 1.5 1.5 3-3" /></>),
    server: (<><rect x="3.5" y="4" width="13" height="5" rx="1.4" /><rect x="3.5" y="11" width="13" height="5" rx="1.4" /><path d="M6.5 6.5h.01M6.5 13.5h.01" /></>),
    check: (<><circle cx="10" cy="10" r="7" /><path d="m6.8 10.1 2.1 2.1 4.3-4.5" /></>),
  };
  return (
    <svg viewBox="0 0 20 20" width="17" height="17" fill="none"
      stroke="currentColor" strokeWidth="1.45" strokeLinecap="round"
      strokeLinejoin="round">{paths[name]}</svg>
  );
}

/** Knowledge is the only view that keeps the general documentation assistant. */
export function KnowledgeView() {
  const { t } = useI18n();
  const responder = useKnowledgeResponder();
  const { scope_id } = useWorkspace();
  return (
    <div className="view home-view" data-knowledge-view="true">
      <section className="home-hero">
        <h1>{t("home.title")}</h1>
      </section>

      <ChatSurface
        responder={responder}
        cancelTurn={cancelAssistantTurn}
        storageScope={`${scope_id}:knowledge`}
        presets={[
          { icon: "cases", label: t("home.preset1Title"), description: t("home.preset1Body"), prompt: t("home.preset1") },
          { icon: "shield", label: t("home.preset2Title"), description: t("home.preset2Body"), prompt: t("home.preset3") },
          { icon: "server", label: t("home.preset3Title"), description: t("home.preset3Body"), prompt: t("home.preset4") },
          { icon: "check", label: t("home.preset4Title"), description: t("home.preset4Body"), prompt: t("home.preset2") },
        ]}
        emptyText={t("home.empty")}
        composerPlaceholder={t("home.composerPlaceholder")}
        composerAria={t("home.title")}
        composerNote={t("assistant.composerNote")}
        className="home-chat"
      />
    </div>
  );
}
