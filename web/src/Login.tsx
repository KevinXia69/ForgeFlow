import { useEffect, useId, useRef, useState } from "react";
import { login } from "./api";
import { useI18n, type MessageKey } from "./i18n";

type LoginError = { key: MessageKey; detail?: string; field?: boolean };

export function Login({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const [code, setCode] = useState("");
  const [error, setError] = useState<LoginError | null>(null);
  const [busy, setBusy] = useState(false);
  const errorId = useId();
  const input = useRef<HTMLInputElement>(null);
  const summary = useRef<HTMLParagraphElement>(null);
  const pendingFocus = useRef<"field" | "summary" | null>(null);
  function showError(next: LoginError) {
    pendingFocus.current = next.field ? "field" : "summary";
    setError(next);
  }
  useEffect(() => {
    const target = pendingFocus.current;
    if (!target) return;
    pendingFocus.current = null;
    if (target === "field") input.current?.focus();
    else summary.current?.focus();
  });

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!code.trim()) {
      showError({ key: "login.errorRequired", field: true });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await login(code);
      setCode("");
      onDone();
    } catch (err) {
      setCode("");
      showError(
        err instanceof Error
          ? { key: "login.errorFailedDetail", detail: err.message }
          : { key: "login.errorFailed" },
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="login-wrap">
      <div className="login-card">
        <h1>{t("login.title")}</h1>
        <form onSubmit={submit} noValidate>
          <label htmlFor="invite-code">{t("login.inviteLabel")}</label>
          <input
            id="invite-code"
            type="password"
            autoComplete="current-password"
            value={code}
            ref={input} aria-invalid={error?.field || undefined}
            aria-describedby={error?.field ? errorId : undefined}
            onChange={(event) => { setCode(event.target.value); setError(null); }}
          />
          <button className="primary" type="submit" disabled={busy}>
            {busy ? t("login.entering") : t("login.enter")}
          </button>
          {error ? (
            <p className="error" role="alert" id={errorId} ref={summary} tabIndex={-1}>
              {t(error.key, error.detail === undefined ? undefined : { detail: error.detail })}
            </p>
          ) : null}
        </form>
      </div>
    </section>
  );
}
