import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { PrivateMarkdown } from "./PrivateMarkdown";
import { ApiError, askAssistant, streamAssistant, cancelAssistantTurn, type StreamTurn } from "./api";
import { curateHistory, newConversationId, readConversations, writeConversations, type SavedConversation, type PendingChatTurn } from "./conversation";
import { caseRoute, useWorkspace } from "./workspace";
import { ActivityPanel } from "./Activity";
import { KnowledgeDocumentDialog } from "./KnowledgeDocumentDialog";
import { type DocumentTarget } from "./knowledgeCitation";
import { CitationSources } from "./CitationSources";
import { DshComposer } from "./dsh/Composer";
import chatCss from "./dsh/ChatView.module.css";
import { useI18n, type Translate } from "./i18n";
import { uploadAttachment } from "./api";
import { isModelConfigComplete, loadModelConfig, supportsModelImages } from "./modelConfig";
import type {
  ActivityState,
  CaseAnswerContext,
  Answer,
  AssistantHistoryTurn,
  ChatImage,
  ConversationAttachment,
  Passage,
} from "./types";

import type { AnswerContextNotice } from "./caseAnswerContext";


export type ChatAnswerMeta = {
  mode: "model" | "retrieval" | "case" | "harness" | "reference_facts";
  warning?: string;
  note?: string;
  citations: string[];
  passages: Passage[];
  activity?: ActivityState;
  scope?: string;
  caseContext?: CaseAnswerContext;
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
  image?: ChatImage,
  attachmentText?: string,
  attachments?: ConversationAttachment[],
) => Promise<ChatReply>;

type AnswerWithMode = Answer & { mode?: "model" | "retrieval" | "harness" | "reference_facts" };

type UiMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  meta?: ChatAnswerMeta;
  image?: ChatImage;
  at?: number;
  turnId?: string;
};

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"] as const;
const IMAGE_LIMIT = 8 * 1024 * 1024;

async function chatImageFromFile(file: File): Promise<ChatImage> {
  if (!IMAGE_TYPES.includes(file.type as ChatImage["mimeType"])) throw new Error("image_type");
  if (file.size > IMAGE_LIMIT) throw new Error("image_size");
  // Keep history inside the browser storage budget. Animated GIFs keep their
  // source; static images are normalized to a bounded JPEG.
  let payload = file;
  if (file.type !== "image/gif") {
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const context = canvas.getContext("2d");
      if (context) {
        context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
        if (blob) payload = new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
      }
      bitmap.close();
    } catch {
      // Older browsers keep the original bounded image.
    }
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("image_read"));
    reader.readAsDataURL(payload);
  });
  const data = dataUrl.split(",", 2)[1] || "";
  if (!data) throw new Error("image_read");
  return {
    data,
    mimeType: payload.type as ChatImage["mimeType"],
    name: payload.name.slice(0, 120) || "image",
  };
}

function chatImageSrc(image: ChatImage): string {
  return `data:${image.mimeType};base64,${image.data}`;
}

function formatAnswer(answer: AnswerWithMode, t: Translate): string {
  if (!answer.found || answer.passages.length === 0) {
    return t("assistant.answerNoPassages");
  }
  if (answer.passages.length === 1) return answer.passages[0].text;
  const parts = [t("assistant.answerQuoteLead")];
  for (const passage of answer.passages) {
    parts.push(
      `\n${passage.path}:${passage.start_line}-${passage.end_line}\n${passage.text}`,
    );
  }
  return parts.join("\n");
}

function cleanAnswerText(text: string): string {
  // Evidence belongs to the evidence views; chat answers stay prose-only.
  let cleaned = text.replace(/<details\b[^>]*>[\s\S]*?(?:<\/details>|$)/gi, "");
  cleaned = cleaned.replace(/<view\s+evidence\b[^>]*>[\s\S]*?(?:<\/view>|$)/gi, "");
  cleaned = cleaned.replace(/<\/?(?:details|summary|view)\b[^>]*>/gi, "");
  const heading = /^[ \t]*(?:\*\*)?(?:#{1,6}[ \t]*)?(?:\u8bc1\u636e|\u53c2\u8003\u8d44\u6599|\u53c2\u8003|\u5f15\u7528|\u6765\u6e90|Evidence|References?|Sources?)[ \t]*[:\uff1a][^\n]*/gim;
  const substantive = /\u7ed3\u8bba|\u98ce\u9669|\u6ce8\u610f|\u9650\u5236|\u4f46|\u7136\u800c|\u4e0d\u8fc7|\u5c1a\u672a|\u672a\u6279\u51c6|\u672a\u6267\u884c|\u4e0d\u80fd|\u65e0\u6cd5|\u4e0d\u662f|\u5b9e\u9645|\u91d1\u989d|conclusion|risk|caveat|but\b|however|not\b|pending|unapproved|unexecuted|actual/i;
  const matches = [...cleaned.matchAll(heading)];
  for (let index = matches.length - 1; index >= 0; index -= 1) {
    const match = matches[index];
    const start = match.index ?? 0;
    const before = cleaned.slice(0, start);
    if (!/\n[ \t]*\n$/.test(before)) continue;
    const suffix = match[0] + cleaned.slice(start + match[0].length);
    if (substantive.test(suffix)) continue;
    cleaned = before.trimEnd();
    break;
  }
  return cleaned.trim();
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
  const [documentTarget, setDocumentTarget] = useState<DocumentTarget | null>(null);
  // Legacy saved harness messages did not record whether duration was measured;
  // their timestamps can be a fabricated zero. Keep the reply but hide that time.
  const activity = meta?.activity && meta.mode === "harness" && meta.activity.hideDuration === undefined
    ? { ...meta.activity, hideDuration: true } : meta?.activity;
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
      {contextNotice && <p className="case-answer-context" data-case-answer-context={contextNotice.kind}>{contextNotice.text}</p>}
      {meta?.mode === "reference_facts" && <p className="case-answer-context" data-reference-facts="true">{t("assistant.referenceFacts")}</p>}
      <div className="assistant-markdown">
        <PrivateMarkdown text={cleanAnswerText(displayText ? displayText(message.text) : message.text)} labels={labels} />
      </div>
      <CitationSources reviewedComparison={meta?.mode === "reference_facts"} citations={meta?.citations ?? []} passages={meta?.passages ?? []} onOpen={setDocumentTarget} />
      {documentTarget && <KnowledgeDocumentDialog target={documentTarget} onClose={() => setDocumentTarget(null)} />}
      {meta?.evidenceRefs?.length ? <div className="answer-citations" data-answer-evidence="true">
        {meta.evidenceRefs.map((ref) => (
          <a className="citation" href={ref.kind === "case" ? caseRoute(ref.id, "evidence")
            : ref.kind === "run" ? `#run=${encodeURIComponent(ref.id)}` : ref.href}
            key={`${ref.kind}:${ref.id}`}>{ref.label}</a>
        ))}
      </div> : null}
      {meta?.note ? <p className="assistant-fallback">{meta.note}</p> : null}
    </div>
  );
}

export type ChatPresetIcon = "cases" | "shield" | "server" | "check";

export type ChatPreset =
  | string
  | {
      label: string;
      prompt?: string;
      description?: string;
      icon?: ChatPresetIcon;
      judgeExample?: "policy" | "specification" | "impact";
      /** A task entry performs a real UI action instead of sending a prompt. */
      onSelect?: () => void;
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
  intake?: (text: string) => Promise<string>;
  intakePlaceholder?: string;
  mode?: "ask" | "intake";
  modelControl?: ReactNode;
  className?: string;
  storageScope: string;
  displayAnswerText?: (text: string) => string;
  answerContextNotice?: (meta: ChatAnswerMeta | undefined) => AnswerContextNotice;
  historyContextWarning?: string;
  cancelTurn?: (conversation: string, turnId: string) => Promise<void>;
  onDocument?: (file: File) => Promise<string>;
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
  intake,
  intakePlaceholder,
  mode,
  modelControl,
  className = "",
  storageScope,
  displayAnswerText,
  answerContextNotice,
  historyContextWarning,
  cancelTurn,
  onDocument,
}: ChatSurfaceProps) {
  const { t, lang } = useI18n();
  const [store, setStore] = useState(() => readConversations(storageScope));
  const current = store.sessions.find(item => item.id === store.active)!;
  const [messages, setMessages] = useState<UiMessage[]>(() => current.messages as UiMessage[]);
  const [storageFailed, setStorageFailed] = useState(false);
  const [interrupted, setInterrupted] = useState(current.pending && !current.pendingTurn);
  const [pendingTurn, setPendingTurn] = useState<PendingChatTurn | null>(current.pendingTurn ?? null);
  const [completedTurns, setCompletedTurns] = useState<string[]>(current.completedTurns ?? []);
  const [stopError, setStopError] = useState(false);
  const requestSequence = useRef(0);
  const stopRequested = useRef(false);
  const [search, setSearch] = useState("");
  const controller = useRef<AbortController | null>(null);


  const [input, setInput] = useState("");
  const [attachment, setAttachment] = useState<ChatImage | null>(null);
  const [imageError, setImageError] = useState("");
  const [documentStatus, setDocumentStatus] = useState("");
  const [documentFailed, setDocumentFailed] = useState(false);
  const [documentBusy, setDocumentBusy] = useState(false);
  const attachmentGeneration = useRef(0);
  const [pendingDocument, setPendingDocument] = useState<File | null>(null);
  const [conversationDocument, setConversationDocument] = useState<ConversationAttachment | null>(null);
  const imageInput = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const activeMode = mode ?? "ask";
  const [intakeBusy, setIntakeBusy] = useState(false);
  const [intakeNotice, setIntakeNotice] = useState("");
  const [intakeError, setIntakeError] = useState("");
  const [liveActivity, setLiveActivity] = useState<ActivityState | null>(null);
  const [following, setFollowing] = useState(true);
  useEffect(() => {
    const updated = { ...store, sessions: store.sessions.map(item => item.id === store.active ? { ...item, messages, pending: busy || pendingTurn !== null, pendingTurn, completedTurns, title: messages.find(m => m.role === "user")?.text.slice(0, 60) || item.title, updated: Date.now() } : item) };
    setStorageFailed(!writeConversations(storageScope, updated));
  }, [messages, busy, pendingTurn, completedTurns, store, storageScope]);
  const switchConversation = (id?: string) => {
    if (busy) return;
    const saved = readConversations(storageScope);
    const sessionId = id || newConversationId();
    let sessions = id ? saved.sessions : [{ id: sessionId, title: "", messages: [], pending: false, updated: Date.now() }, ...saved.sessions];
    let session = sessions.find(item => item.id === sessionId);
    if (!session && id) {
      // Another tab may have overwritten storage; keep the session shown in
      // this tab rather than dereferencing a missing record.
      session = store.sessions.find(item => item.id === sessionId);
      if (session) sessions = [session, ...sessions.filter(item => item.id !== sessionId)];
    }
    if (!session) {
      setStorageFailed(true);
      return;
    }
    const next = { active: sessionId, sessions };
    attachmentGeneration.current++;
    setAttachment(null); setConversationDocument(null); setPendingDocument(null);
    setImageError(""); setDocumentStatus(""); setDocumentFailed(false); setDocumentBusy(false);
    setIntakeNotice(""); setIntakeError("");
    setStore(next); setMessages(session.messages as UiMessage[]); setInterrupted(session.pending && !session.pendingTurn); setPendingTurn(session.pendingTurn ?? null); setCompletedTurns(session.completedTurns ?? []); setInput(""); setSearch("");
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
      const reply = await responder(turn.question, curateHistory(prior, turn.question, turn.id, resume), activity => { if (sequence === requestSequence.current) setLiveActivity(activity); }, request.signal, conversation, {
        id: turn.id, lastEventId: turn.lastEventId, resume,
        onCursor: id => { if (sequence === requestSequence.current) setPendingTurn(value => value?.id === turn.id ? { ...value, lastEventId: id } : value); },
      }, turn.image, turn.attachmentText, turn.attachments);
      if (sequence !== requestSequence.current) return;
      setMessages(value => [...value, { id: newId(), role: "assistant", text: stopRequested.current ? t("v9.stopped") : reply.text, meta: stopRequested.current ? { mode: "retrieval", citations: [], passages: [] } : reply.meta, at: Date.now(), turnId: turn.id }]);
    } catch (error) {
      if (sequence !== requestSequence.current) return;
      const unavailable = error instanceof ApiError && [409, 410].includes(error.status);
      const detail = error instanceof Error && error.message ? error.message : "unknown";
      setMessages(value => [...value, { id: newId(), role: "assistant", text: stopRequested.current ? t("v9.stopped") : unavailable && resume ? t("v9.resumeUnavailable") : t("assistant.answerFailed", { detail }), meta: { mode: "retrieval", citations: [], passages: [] }, at: Date.now(), turnId: turn.id }]);
    } finally {
      if (sequence === requestSequence.current) {
        controller.current = null; setLiveActivity(null); setBusy(false); setPendingTurn(null);
        setCompletedTurns(value => [...new Set([...value, turn.id])].slice(-64));
        requestAnimationFrame(() => { const viewport = viewportRef.current; if (viewport) viewport.scrollTop = viewport.scrollHeight; });
      }
    }
  }

  // Unmount only detaches this subscriber. The saved turn can reconnect without
  // another model call; an explicit Stop separately cancels its server output.
  useEffect(() => {
    if (current.pendingTurn) void runTurn(current.pendingTurn, true);
    return () => { requestSequence.current++; attachmentGeneration.current++; controller.current?.abort(); controller.current = null; };
  }, []);

  useEffect(() => {
    setIntakeNotice(""); setIntakeError("");
    if (activeMode === "intake") {
      attachmentGeneration.current++;
      setAttachment(null); setConversationDocument(null); setPendingDocument(null);
      setDocumentStatus(""); setDocumentFailed(false); setDocumentBusy(false); setImageError("");
    }
  }, [activeMode]);

  async function send(question: string, image = attachment) {
    if (documentBusy) return;
    const trimmed = question.trim() || (image && activeMode === "ask" ? t("assistant.imageOnly") : "")
      || (conversationDocument && activeMode === "ask" ? t("assistant.documentOnly") : "");
    if (!trimmed) return;
    if (intake && activeMode === "intake") {
      if (busy || intakeBusy) return;
      setIntakeBusy(true); setIntakeNotice(""); setIntakeError("");
      try {
        setIntakeNotice(await intake(trimmed));
        setInput(""); setAttachment(null); setImageError(""); setFollowing(true);
      } catch (failure) {
        setIntakeError(failure instanceof Error ? failure.message : "unknown");
      } finally { setIntakeBusy(false); }
      return;
    }
    if (busy) return;
    if (activeMode === "intake") image = null;
    const turn = { id: newId(), question: trimmed, lastEventId: "", image: image ?? undefined,
      attachments: conversationDocument ? [conversationDocument] : undefined };
    attachmentGeneration.current++;
    setMessages(value => [...value, { id: newId(), role: "user", text: trimmed, image: image ?? undefined, at: Date.now(), turnId: turn.id }]);
    setInput(""); setAttachment(null); setImageError(""); setFollowing(true);
    setConversationDocument(null);
    setDocumentStatus(""); setDocumentFailed(false);
    await runTurn(turn, false);
  }

  async function chooseImage(file: File | null) {
    if (!file) return;
    const generation = ++attachmentGeneration.current;
    setImageError(""); setDocumentStatus(""); setDocumentFailed(false); setDocumentBusy(false);
    setAttachment(null); setConversationDocument(null); setPendingDocument(null);
    if (!IMAGE_TYPES.includes(file.type as ChatImage["mimeType"])) {
      if (!onDocument) { setImageError(t("assistant.imageType")); return; }
      setPendingDocument(file);
      return;
    }
    setDocumentBusy(true);
    try {
      const image = await chatImageFromFile(file);
      if (generation === attachmentGeneration.current) setAttachment(image);
    } catch (error) {
      if (generation !== attachmentGeneration.current) return;
      const code = error instanceof Error ? error.message : "image_read";
      setImageError(t(code === "image_size" ? "assistant.imageTooLarge" : code === "image_type" ? "assistant.imageType" : "assistant.imageRead"));
    } finally {
      if (generation === attachmentGeneration.current) setDocumentBusy(false);
    }
  }

  async function saveDocument() {
    if (!pendingDocument || !onDocument || documentBusy) return;
    const generation = ++attachmentGeneration.current;
    setDocumentBusy(true); setDocumentFailed(false); setDocumentStatus(t("assistant.documentUploading"));
    try {
      const status = await onDocument(pendingDocument);
      if (generation === attachmentGeneration.current) setDocumentStatus(status);
    } catch {
      if (generation === attachmentGeneration.current) { setDocumentFailed(true); setDocumentStatus(t("assistant.documentFailed")); }
    } finally {
      if (generation === attachmentGeneration.current) { setPendingDocument(null); setDocumentBusy(false); }
    }
  }

  async function attachDocument() {
    if (!pendingDocument || documentBusy) return;
    const generation = ++attachmentGeneration.current;
    setDocumentBusy(true); setDocumentFailed(false); setDocumentStatus(t("assistant.documentUploading"));
    try {
      const uploaded = await uploadAttachment(pendingDocument);
      if (generation !== attachmentGeneration.current) return;
      setConversationDocument(uploaded);
      setDocumentStatus(t("assistant.documentAttached"));
    } catch {
      if (generation === attachmentGeneration.current) { setDocumentFailed(true); setDocumentStatus(t("assistant.documentExtractFailed")); }
    } finally {
      if (generation === attachmentGeneration.current) { setPendingDocument(null); setDocumentBusy(false); }
    }
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
    const clean = (text || "").replace(/\s+/g, " ").trim().replace(/[\u3002\uff1f\uff01?!.]+$/, "");
    return clean.length > 16 ? `${clean.slice(0, 16)}…` : clean;
  };
  // A conversation started from a suggestion is labelled with the suggestion's
  // short label, not with the longer prompt it sends.
  const presetLabel = (text: string) => {
    for (const preset of presets) {
      const item = typeof preset === "string" ? { label: preset, prompt: preset } : preset;
      if (item.prompt && item.prompt === text) return item.label;
    }
    return "";
  };
  const threadTitle = (item: SavedConversation) => {
    const history = item.id === store.active ? messages : item.messages;
    const raw = history.find(message => message.role === "user")?.text || item.title;
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
        <span className="chat-icon" aria-hidden="true">+</span>
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
              .filter(item => !search || threadTitle(item).toLowerCase().includes(search.toLowerCase()))
              .map(item => (
              <li key={item.id}>
                <button
                  type="button"
                  className={item.id === store.active ? "chat-thread is-active" : "chat-thread"}
                  aria-current={item.id === store.active ? "true" : undefined}
                  disabled={busy}
                  onClick={() => switchConversation(item.id)}
                  title={threadTitle(item)}
                >
                  <span>{threadTitle(item)}</span>
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
                <div className="msg user" key={message.id}>
                  {message.image ? <img className="user-image" src={chatImageSrc(message.image)} alt={message.image.name || t("assistant.imageAlt")} /> : null}
                  <p>{message.text}</p>
                </div>
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

        {attachment && activeMode === "ask" ? (
          <div className="composer-attachment" data-composer-attachment="true">
            <img src={chatImageSrc(attachment)} alt={t("assistant.imageAlt")} />
            <span>{attachment.name}</span>
            <button type="button" className="composer-attachment-remove" aria-label={t("assistant.removeImage")} disabled={busy}
              onClick={() => setAttachment(null)}>×</button>
          </div>
        ) : null}
        {imageError ? <p className="composer-image-error" role="status">{imageError}</p> : null}
        {pendingDocument ? <div className="composer-document-choice">
          <span>{pendingDocument.name}</span>
          <small>{t("assistant.documentChoice")}</small>
          <button type="button" className="secondary" disabled={documentBusy} onClick={() => void saveDocument()}>{t("assistant.documentSaveCompany")}</button>
          {/\.(pdf|docx|xlsx|pptx|csv|tsv|json|md|markdown|txt)$/i.test(pendingDocument.name)
            ? <button type="button" className="secondary" disabled={documentBusy} onClick={() => void attachDocument()}>{t("assistant.documentUseInConversation")}</button>
            : null}
          <button type="button" className="secondary" onClick={() => { attachmentGeneration.current++; setPendingDocument(null); setDocumentStatus(""); setDocumentBusy(false); }}>{t("assistant.documentCancel")}</button>
        </div> : null}
        {conversationDocument ? <div className="composer-attachment" data-composer-document="true">
          <span>{conversationDocument.name}</span>
          <button type="button" className="composer-attachment-remove" aria-label={t("assistant.documentCancel")} disabled={busy}
            onClick={() => { setConversationDocument(null); setDocumentStatus(""); }}>×</button>
        </div> : null}
        {documentStatus ? <p className={`composer-document-status${documentFailed ? " is-error" : ""}`} role="status">{documentStatus}</p> : null}
        <div className="composer composer-compact">
          <input ref={imageInput} type="file" accept={onDocument
            ? ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.csv,.tsv,.json,.md,.markdown,.txt,image/png,image/jpeg,image/webp,image/gif"
            : "image/png,image/jpeg,image/webp,image/gif"} hidden
            onChange={(event) => { void chooseImage(event.target.files?.[0] ?? null); event.currentTarget.value = ""; }} />
          <DshComposer
            value={input}
            ariaLabel={composerAria ?? t("assistant.composerAria")}
            placeholder={activeMode === "intake" ? intakePlaceholder ?? t("assistant.intakePlaceholder") : composerPlaceholder}
            disabled={busy || intakeBusy || documentBusy}
            onChange={setInput}
            onSend={() => void send(input)}
          />
          <div className="composer-bar">
            {activeMode === "ask" ? <button type="button" className="attach" aria-label={t("assistant.attachFile")} disabled={busy}
              onClick={() => imageInput.current?.click()}>+</button> : null}
            {composerHint ? <span className="composer-hint">{composerHint}</span> : <span />}
            <div className="composer-actions">
              {modelControl}
              {busy && <button type="button" className="secondary" onClick={() => void stop()}>{t("v9.stop")}</button>}
              <button
                className={`send${activeMode === "intake" ? " is-intake" : ""}`}
                type="button"
                aria-label={activeMode === "intake" ? t("assistant.intakeSend") : t("assistant.send")}
                disabled={busy || intakeBusy || documentBusy || (input.trim() === ""
                  && (activeMode !== "ask" || (!attachment && !conversationDocument)))}
                onClick={() => void send(input)}
              >
                {activeMode === "intake" ? t("assistant.intakeSend") : "↑"}
              </button>
            </div>
          </div>
        </div>
        {intakeNotice ? <p className="composer-intake-notice" role="status">{intakeNotice}</p> : null}
        {intakeError ? <p className="composer-intake-notice is-error" role="alert">{intakeError}</p> : null}
        {composerNote ? <p className="composer-note">{composerNote}</p> : null}
        {messages.length === 0 && presets.length > 0 && activeMode === "ask" ? (
          <div className="chat-presets" data-chat-presets="true">
            {[
              { key: "actions", title: presetsTitle, items: presets.filter(preset => typeof preset === "string" || !preset.judgeExample) },
              { key: "examples", title: t("home.exampleQuestions"), items: presets.filter(preset => typeof preset !== "string" && preset.judgeExample) },
            ].filter(group => group.items.length > 0).map(group => (
              <section className={`chat-preset-group chat-preset-group-${group.key}`} key={group.key}>
                {group.title ? <h2 className="chat-presets-title">{group.title}</h2> : null}
                <div className={group.key === "examples" ? "chat-examples-list" : "chat-presets-list"}>
                  {group.items.map((preset, index) => {
                    const item = typeof preset === "string" ? { label: preset, prompt: preset } : preset;
                    return (
                      <button
                        key={item.prompt ?? item.label}
                        type="button"
                        data-judge-example={item.judgeExample}
                        className={`chat-preset${item.description ? " is-rich" : ""}${item.judgeExample ? " is-example" : ""}`}
                        onClick={() => item.onSelect ? item.onSelect() : item.prompt && void send(item.prompt)}
                      >
                        {item.icon ? <span className="chat-preset-icon" aria-hidden="true"><PresetIcon name={item.icon} /></span> : null}
                        {item.judgeExample ? <span className="chat-example-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span> : null}
                        <span className="chat-preset-copy">
                          {item.description ? <><strong>{item.label}</strong><small>{item.description}</small></> : item.label}
                        </span>
                        {item.description || item.judgeExample ? <span className="chat-preset-arrow" aria-hidden="true">→</span> : null}
                      </button>
                    );
                  })}
                </div>
              </section>
            ))}
          </div>
        ) : null}

      </div>
  );
}

export function useKnowledgeResponder(): ChatResponder {
  const { t, lang } = useI18n();
  const { scope_id, deployment } = useWorkspace();
  return useMemo<ChatResponder>(
    () => async (question, history, onTrace, signal, conversation, turn, image, attachmentText, attachments) => {
      const stored = loadModelConfig(scope_id, deployment);
      const model = isModelConfigComplete(stored) ? stored : undefined;
      if (image && model && !supportsModelImages(model)) throw new Error(t("settings.imageUnavailable"));
      let answer: AnswerWithMode;
      if (turn?.resume && (model || !onTrace)) throw new ApiError("turn_unavailable", 410);
      if (!onTrace) {
        answer = await askAssistant(
          question,
          model,
          history.length > 0 ? history : undefined,
          conversation,
          lang,
          signal,
          image,
          attachmentText,
          attachments,
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
            image,
            attachmentText,
            attachments,
            model,
          );
        } catch (failure) {
          if (signal?.aborted || turn?.resume || !(failure instanceof ApiError && failure.status === 503 && failure.message === "stream_unavailable")) throw failure;
          answer = await askAssistant(
            question,
            model,
            history.length > 0 ? history : undefined,
            conversation,
            lang,
            signal,
            image,
            attachmentText,
            attachments,
          );
        }
      }
      const mode: "model" | "retrieval" | "harness" | "reference_facts" =
        answer.mode === "harness" || answer.mode === "model" || answer.mode === "reference_facts" ? answer.mode : "retrieval";
      const text = mode === "model" || mode === "harness" || mode === "reference_facts"
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
        className="home-chat"
      />
    </div>
  );
}
