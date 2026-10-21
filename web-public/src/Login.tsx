import { useState } from "react";
import { login } from "./api";
import { useI18n, type MessageKey } from "./i18n";

type LoginError = { key: MessageKey; detail?: string };

export function Login({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const [code, setCode] = useState("");
  const [error, setError] = useState<LoginError | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!code) {
      setError({ key: "login.errorRequired" });
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
      setError(
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
        <p className="muted">{t("login.copy")}</p>
        <form onSubmit={submit} noValidate>
          <label htmlFor="invite-code">{t("login.inviteLabel")}</label>
          <input
            id="invite-code"
            type="password"
            autoComplete="current-password"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
          <button className="primary" type="submit" disabled={busy}>
            {busy ? t("login.entering") : t("login.enter")}
          </button>
          {error ? (
            <p className="error" role="alert">
              {t(error.key, error.detail === undefined ? undefined : { detail: error.detail })}
            </p>
          ) : null}
        </form>
      </div>
    </section>
  );
}
