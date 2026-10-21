import { useState } from "react";
import { askAssistant } from "./api";
import { conversationId } from "./conversation";
import { useI18n } from "./i18n";
import type { Answer } from "./types";

type Props = {
  onOpenChat: () => void;
};

function answerText(answer: Answer): string {
  const direct = answer.answer?.trim();
  if (direct) return direct;
  if (answer.passages.length > 0) return answer.passages[0].text;
  return "";
}

/**
 * The home hero input. It is a real, small chat entry — not a decorative box —
 * and uses the same assistant endpoint / DSH session as the Knowledge view.
 */
export function HomeAsk({ onOpenChat }: Props) {
  const { t, lang } = useI18n();
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (value = question) => {
    const text = value.trim();
    if (text.length < 2 || busy) return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const result = await askAssistant(
        text,
        undefined,
        undefined,
        conversationId(),
        lang,
      );
      setAnswer(result);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const suggestions = [
    t("cases.heroPrompt1"),
    t("cases.heroPrompt2"),
    t("cases.heroPrompt3"),
  ];
  const rendered = answer ? answerText(answer) : "";

  return (
    <section className="home-ask" data-home-ask="true">
      <form
        className="home-ask-row"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <span className="home-ask-mark" aria-hidden="true">⌕</span>
        <input
          aria-label={t("cases.heroAskPlaceholder")}
          placeholder={t("cases.heroAskPlaceholder")}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={busy}
        />
        <button className="home-ask-send" type="submit" disabled={busy || question.trim().length < 2}>
          {busy ? "…" : "↑"}
        </button>
      </form>

      <div className="home-ask-suggestions">
        {suggestions.map((suggestion) => (
          <button
            key={suggestion}
            type="button"
            className="home-ask-chip"
            onClick={() => {
              setQuestion(suggestion);
              void submit(suggestion);
            }}
            disabled={busy}
          >
            {suggestion}
            <span aria-hidden="true">↗</span>
          </button>
        ))}
      </div>

      {busy ? <p className="home-ask-status">{t("cases.heroAsking")}</p> : null}
      {error ? <p className="home-ask-error" role="alert">{error}</p> : null}
      {answer ? (
        <div className="home-ask-answer" data-home-answer="true">
          <div className="home-ask-answer-head">
            <button className="home-ask-open" type="button" onClick={onOpenChat}>
              {t("cases.heroOpenChat")} →
            </button>
          </div>
          <p>{rendered || t("cases.heroNoAnswer")}</p>
          {answer.citations.length > 0 ? (
            <div className="home-ask-cites">
              {answer.citations.slice(0, 3).map((citation) => (
                <span key={citation}>{citation}</span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
