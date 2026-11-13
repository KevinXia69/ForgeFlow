import { useEffect, useState } from "react";
import { accountLogin, accountMfa, ApiError, api, login } from "./api";
import { SelectMenu } from "./SelectMenu";
import type { Config } from "./types";
import { useI18n, type Lang, type MessageKey } from "./i18n";

type LoginError = { key: MessageKey; detail?: string };
type Mode = "invite" | "account";

/** A bounded error code becomes a user-facing message; no raw server text. */
function accountError(error: unknown): LoginError {
  if (error instanceof ApiError) {
    const map: Record<string, MessageKey> = {
      credentials_invalid: "login.errorCredentials",
      too_many_attempts: "login.errorLocked",
      mfa_code_invalid: "login.errorMfa",
      mfa_challenge_invalid: "login.errorMfa",
      account_disabled: "login.errorDisabled",
    };
    return map[error.message] ? { key: map[error.message] } : { key: "login.errorFailedDetail", detail: error.message };
  }
  return { key: "login.errorFailed" };
}

export function Login({ onDone }: { onDone: () => void }) {
  const { lang, setLang, t } = useI18n();
  const [mode, setMode] = useState<Mode>("invite");
  const [code, setCode] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [challenge, setChallenge] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const [error, setError] = useState<LoginError | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    void api<Config>("/api/config")
      .then((config) => { if (live && config.accounts_enabled) setMode("account"); })
      .catch(() => { /* a public config failure keeps the local invite form */ });
    return () => { live = false; };
  }, []);

  async function submitInvite(event: React.FormEvent) {
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
      setError(err instanceof Error
        ? { key: "login.errorFailedDetail", detail: err.message }
        : { key: "login.errorFailed" });
    } finally {
      setBusy(false);
    }
  }

  async function submitAccount(event: React.FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError({ key: "login.accountRequired" });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await accountLogin(email.trim(), password);
      if (result.mfa_required && result.challenge) {
        setChallenge(result.challenge);
        setPassword("");
        return;
      }
      onDone();
    } catch (err) {
      setPassword("");
      setError(accountError(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitMfa(event: React.FormEvent) {
    event.preventDefault();
    if (!/^\d{6,8}$/.test(mfaCode)) {
      setError({ key: "login.mfaRequired" });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await accountMfa(challenge, mfaCode);
      setMfaCode("");
      setChallenge("");
      onDone();
    } catch (err) {
      setMfaCode("");
      setError(accountError(err));
    } finally {
      setBusy(false);
    }
  }

  function errorNode() {
    return error ? (
      <p className="error" role="alert">
        {t(error.key, error.detail === undefined ? undefined : { detail: error.detail })}
      </p>
    ) : null;
  }

  function language() {
    return (
      <div className="login-language">
        <label htmlFor="language-select">{t("login.language")}</label>
        <SelectMenu
          id="language-select"
          value={lang}
          ariaLabel={t("login.language")}
          options={[
            { value: "zh", label: t("login.languageZh") },
            { value: "en", label: t("login.languageEn") },
          ]}
          onChange={(value) => setLang(value as Lang)}
        />
      </div>
    );
  }

  return (
    <section className="login-wrap">
      <div className="login-card">
        <p className="login-brand" aria-label={t("sidebar.brandName")}>{t("sidebar.brandName")}</p>
        <h1>{t("login.title")}</h1>
        {mode === "account" ? (
          challenge ? (
            <form onSubmit={submitMfa} noValidate>
              {language()}
              <label htmlFor="mfa-code">{t("login.mfaLabel")}</label>
              <input id="mfa-code" inputMode="numeric" autoComplete="one-time-code"
                value={mfaCode} onChange={(event) => setMfaCode(event.target.value)} />
              <button className="primary" type="submit" disabled={busy}>
                {busy ? t("login.entering") : t("login.mfaEnter")}
              </button>
              {errorNode()}
            </form>
          ) : (
            <form onSubmit={submitAccount} noValidate>
              {language()}
              <label htmlFor="account-email">{t("login.email")}</label>
              <input id="account-email" type="email" autoComplete="username" dir="ltr"
                value={email} onChange={(event) => setEmail(event.target.value)} />
              <label htmlFor="account-password">{t("login.password")}</label>
              <input id="account-password" type="password" autoComplete="current-password"
                dir="ltr" value={password} onChange={(event) => setPassword(event.target.value)} />
              <button className="primary" type="submit" disabled={busy}>
                {busy ? t("login.entering") : t("login.accountEnter")}
              </button>
              {errorNode()}
            </form>
          )
        ) : (
          <form onSubmit={submitInvite} noValidate>
            {language()}
            <label htmlFor="invite-code">{t("login.inviteLabel")}</label>
            <input
              id="invite-code"
              type="password"
              autoComplete="current-password"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              aria-describedby="invite-code-hint"
            />
            <p className="sr-only" id="invite-code-hint">{t("login.inviteHint")}</p>
            <button className="primary" type="submit" disabled={busy}>
              {busy ? t("login.entering") : t("login.enter")}
            </button>
            {errorNode()}
          </form>
        )}
      </div>
    </section>
  );
}
