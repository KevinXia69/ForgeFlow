"""Invite-only independent accounts, roles, MFA and revocable sessions.

The previous authentication derived a permanent identity from a shared invite
code: rotating one code logged out everybody, and no session could be revoked
without removing the code.  Here an account is a row with its own password, role
and MFA secret; a session is a database row that can be revoked, and every request
checks it.  Invite codes only create the account; they are not a login identity.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROLES = ("admin", "planner", "approver", "observer")
#: Roles that must present a second factor.
MFA_ROLES = frozenset({"admin", "approver"})
SESSION_TTL_SECONDS = 8 * 3600
CHALLENGE_TTL_SECONDS = 300
INVITE_TTL_SECONDS = 7 * 24 * 3600
RESET_TTL_SECONDS = 3600
MAX_FAILURES = 5
MAX_MFA_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60


class AccountError(ValueError):
    """A bounded, client-safe rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    tenant_id: str
    email: str
    display_name: str
    role: str
    state: str
    principal_id: str
    mfa_enabled: bool

    @property
    def active(self) -> bool:
        return self.state == "active"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant_id": self.tenant_id, "email": self.email,
                "display_name": self.display_name, "role": self.role,
                "state": self.state, "mfa_enabled": self.mfa_enabled}


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    account: Account
    expires_at: int


# --------------------------------------------------------------------- crypto
def _b64(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def new_mfa_secret() -> str:
    return _b64(secrets.token_bytes(20))


def totp(secret: str, *, at: int | None = None, step: int = 30, digits: int = 6) -> str:
    counter = int((time.time() if at is None else at) // step)
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % (10 ** digits):0{digits}d}"


def verify_totp(secret: str, code: str, *, at: int | None = None) -> bool:
    if not isinstance(code, str) or not code.isdigit():
        return False
    moment = int(time.time() if at is None else at)
    # Accept one step of clock drift on either side.
    return any(hmac.compare_digest(totp(secret, at=moment + delta), code)
               for delta in (-30, 0, 30))


_PASSWORD_N = 2 ** 14
_PASSWORD_R = 8
_PASSWORD_P = 1


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 10:
        raise AccountError("password_too_short")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_PASSWORD_N, r=_PASSWORD_R, p=_PASSWORD_P,
                            dklen=32)
    return (f"scrypt${_PASSWORD_N}${_PASSWORD_R}${_PASSWORD_P}$"
            f"{salt.hex()}${digest.hex()}")


def verify_password(encoded: str, password: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = str(encoded).split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token() -> str:
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class AccountStore:
    """A small SQLite store; no external service is required for accounts."""

    def __init__(self, path: str | Path, *, now=None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._now = now or (lambda: int(time.time()))
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def _migrate(self) -> None:
        with self._connection:
            self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL,
                display_name TEXT NOT NULL, role TEXT NOT NULL, state TEXT NOT NULL,
                password_hash TEXT NOT NULL, principal_id TEXT NOT NULL UNIQUE,
                mfa_secret TEXT, mfa_enabled INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS accounts_email
                ON accounts(lower(email));
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                revoked_at INTEGER, mfa_verified INTEGER NOT NULL DEFAULT 1,
                client TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS invites (
                code_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, email TEXT NOT NULL,
                role TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER);
            CREATE TABLE IF NOT EXISTS resets (
                code_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL, used_at INTEGER);
            CREATE TABLE IF NOT EXISTS challenges (
                token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL, used_at INTEGER);
            CREATE TABLE IF NOT EXISTS attempts (
                key TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT,
                action TEXT NOT NULL, at INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '');
            """)

    # ---------------------------------------------------------------- helpers
    def _row_account(self, row: sqlite3.Row | None) -> Account | None:
        if row is None:
            return None
        return Account(id=str(row["id"]), tenant_id=str(row["tenant_id"]),
                       email=str(row["email"]), display_name=str(row["display_name"]),
                       role=str(row["role"]), state=str(row["state"]),
                       principal_id=str(row["principal_id"]),
                       mfa_enabled=bool(row["mfa_enabled"]))

    def _audit(self, cursor, account_id: str | None, action: str,
               detail: dict | None = None) -> None:
        cursor.execute("INSERT INTO audit(account_id, action, at, detail) VALUES (?,?,?,?)",
                       (account_id, action, self._now(),
                        json.dumps(detail or {}, sort_keys=True)))

    def _failures(self, key: str) -> int:
        cut = self._now() - FAILURE_WINDOW_SECONDS
        row = self._connection.execute(
            "SELECT count(*) AS count FROM attempts WHERE key=? AND at>=?",
            (key, cut)).fetchone()
        return int(row["count"])

    # ------------------------------------------------------------- lifecycle
    def bootstrap(self, *, email: str, password: str, tenant_id: str = "default",
                  display_name: str = "") -> tuple[Account, str]:
        """Create the first administrator; only valid while the store is empty."""
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT count(*) AS count FROM accounts").fetchone()["count"]
            if int(existing):
                raise AccountError("accounts_already_initialized")
            return self._create(self._connection, email=email, password=password,
                                tenant_id=tenant_id, display_name=display_name,
                                role="admin")

    def invite(self, *, admin_principal_id: str, email: str, role: str) -> str:
        admin = self._require_admin(admin_principal_id)
        if role not in ROLES:
            raise AccountError("role_invalid")
        email = _email(email)
        with self._lock, self._connection:
            if self._connection.execute(
                    "SELECT 1 FROM accounts WHERE lower(email)=?",
                    (email,)).fetchone():
                raise AccountError("account_exists")
            code = _token()
            self._connection.execute(
                "INSERT INTO invites(code_hash, tenant_id, email, role, expires_at) "
                "VALUES (?,?,?,?,?)",
                (_hash_token(code), admin.tenant_id, email, role,
                 self._now() + INVITE_TTL_SECONDS))
            self._audit(self._connection, admin.id, "invite_created",
                        {"email": email, "role": role})
            return code

    def activate(self, *, code: str, password: str,
                 display_name: str = "") -> tuple[Account, str | None]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM invites WHERE code_hash=?", (_hash_token(code),)).fetchone()
            if row is None or row["used_at"] is not None:
                raise AccountError("invite_invalid")
            if int(row["expires_at"]) <= self._now():
                raise AccountError("invite_expired")
            account, secret = self._create(
                self._connection, email=str(row["email"]), password=password,
                tenant_id=str(row["tenant_id"]), display_name=display_name,
                role=str(row["role"]))
            self._connection.execute("UPDATE invites SET used_at=? WHERE code_hash=?",
                                     (self._now(), row["code_hash"]))
            self._audit(self._connection, account.id, "account_activated",
                        {"role": account.role})
            return account, secret

    def _create(self, cursor, *, email: str, password: str, tenant_id: str,
                display_name: str, role: str) -> tuple[Account, str | None]:
        if role not in ROLES:
            raise AccountError("role_invalid")
        email = _email(email)
        encoded = hash_password(password)
        identifier = secrets.token_hex(16)
        principal = secrets.token_hex(13)
        secret = new_mfa_secret() if role in MFA_ROLES else None
        now = self._now()
        cursor.execute(
            "INSERT INTO accounts(id, tenant_id, email, display_name, role, state, "
            "password_hash, principal_id, mfa_secret, mfa_enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, tenant_id, email, display_name or email, role, "active",
             encoded, principal, secret, 1 if secret else 0, now, now))
        account = Account(identifier, tenant_id, email, display_name or email, role,
                          "active", principal, bool(secret))
        return account, secret

    # ---------------------------------------------------------------- login
    def login(self, *, email: str, password: str,
              client: str = "") -> dict[str, Any]:
        email = _email(email)
        if self._failures(email) >= MAX_FAILURES:
            raise AccountError("too_many_attempts")
        failure_account: str | None = None
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE lower(email)=?", (email,)).fetchone()
            account = self._row_account(row)
            valid = (account is not None and account.state == "active"
                     and row is not None
                     and verify_password(str(row["password_hash"]), password))
            if not valid:
                # Remember the id so the failure can be committed *after* this
                # transaction rolls back; raising inside it would erase the
                # attempt and defeat the rate limit.
                failure_account = account.id if account else None
            else:
                self._connection.execute("DELETE FROM attempts WHERE key=?", (email,))
                self._audit(self._connection, account.id, "login_succeeded",
                            {"client": client[:64]})
                if account.mfa_enabled:
                    challenge = _token()
                    self._connection.execute(
                        "INSERT INTO challenges(token_hash, account_id, expires_at) "
                        "VALUES (?,?,?)",
                        (_hash_token(challenge), account.id,
                         self._now() + CHALLENGE_TTL_SECONDS))
                    return {"mfa_required": True, "challenge": challenge,
                            "account": account.as_dict()}
                return {"mfa_required": False,
                        "session": self._issue_session(self._connection, account,
                                                       client)}
        with self._lock, self._connection:
            self._connection.execute("INSERT INTO attempts(key, at) VALUES (?,?)",
                                     (email, self._now()))
            self._audit(self._connection, failure_account, "login_failed",
                        {"email": email, "client": client[:64]})
        raise AccountError("credentials_invalid")

    def verify_mfa(self, *, challenge: str, code: str, client: str = "") -> Session:
        failure: tuple[str, str, str, str] | None = None
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM challenges WHERE token_hash=?",
                (_hash_token(challenge),)).fetchone()
            if row is None or row["used_at"] is not None or int(row["expires_at"]) <= self._now():
                raise AccountError("mfa_challenge_invalid")
            account_row = self._connection.execute(
                "SELECT * FROM accounts WHERE id=? AND state='active'",
                (row["account_id"],)).fetchone()
            account = self._row_account(account_row)
            if account is None or account_row is None:
                raise AccountError("account_disabled")
            account_key = f"mfa:{account.id}"
            challenge_key = f"mfa:{row['token_hash']}"
            if (self._failures(account_key) >= MAX_MFA_FAILURES
                    or self._failures(challenge_key) >= MAX_MFA_FAILURES):
                raise AccountError("too_many_attempts")
            secret = account_row["mfa_secret"]
            if not secret or not verify_totp(str(secret), code):
                # Raise only after this transaction rolls back; the attempt and
                # audit must survive the failed verification.
                failure = (account.id, str(row["token_hash"]), account_key, challenge_key)
            else:
                self._connection.execute("DELETE FROM attempts WHERE key IN (?,?)",
                                         (account_key, challenge_key))
                self._connection.execute(
                    "UPDATE challenges SET used_at=? WHERE token_hash=?",
                    (self._now(), row["token_hash"]))
                self._audit(self._connection, account.id, "mfa_succeeded")
                return self._issue_session(self._connection, account, client)
        if failure is not None:
            account_id, token_hash, account_key, challenge_key = failure
            with self._lock, self._connection:
                for key in (account_key, challenge_key):
                    self._connection.execute(
                        "INSERT INTO attempts(key, at) VALUES (?,?)", (key, self._now()))
                self._audit(self._connection, account_id, "mfa_failed")
                limit_hit = self._failures(challenge_key) >= MAX_MFA_FAILURES
                if limit_hit:
                    self._connection.execute(
                        "UPDATE challenges SET used_at=? WHERE token_hash=?",
                        (self._now(), token_hash))
            raise AccountError("too_many_attempts" if limit_hit else "mfa_code_invalid")
        raise AccountError("mfa_code_invalid")

    def _issue_session(self, cursor, account: Account, client: str) -> Session:
        token = _token()
        now = self._now()
        expires = now + SESSION_TTL_SECONDS
        cursor.execute(
            "INSERT INTO sessions(id, account_id, token_hash, issued_at, expires_at, "
            "client) VALUES (?,?,?,?,?,?)",
            (secrets.token_hex(16), account.id, _hash_token(token), now, expires,
             client[:120]))
        self._audit(cursor, account.id, "session_issued",
                    {"expires_at": expires, "client": client[:64]})
        return Session(token=token, account=account, expires_at=expires)

    def verify_session(self, token: str | None) -> tuple[Account, str] | None:
        if not token:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT s.id AS session_id, s.expires_at, s.revoked_at, a.* "
                "FROM sessions s JOIN accounts a ON a.id=s.account_id "
                "WHERE s.token_hash=?", (_hash_token(token),)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if int(row["expires_at"]) <= self._now():
            return None
        account = self._row_account(row)
        if account is None or account.state != "active":
            return None
        return account, str(row["session_id"])

    def logout(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (self._now(), _hash_token(token)))
            return cursor.rowcount > 0

    def revoke_account(self, principal_id: str) -> int:
        account = self.account_for_principal(principal_id)
        if account is None:
            raise AccountError("account_missing")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
                (self._now(), account.id))
            self._audit(self._connection, account.id, "sessions_revoked",
                        {"count": cursor.rowcount})
            return cursor.rowcount

    def account_for_principal(self, principal_id: str) -> Account | None:
        row = self._connection.execute(
            "SELECT * FROM accounts WHERE principal_id=?", (principal_id,)).fetchone()
        return self._row_account(row)

    # ------------------------------------------------------------ management
    def _require_admin(self, principal_id: str) -> Account:
        account = self.account_for_principal(principal_id)
        if account is None or not account.active or account.role != "admin":
            raise AccountError("admin_required")
        return account

    def members(self, *, admin_principal_id: str) -> list[dict[str, Any]]:
        admin = self._require_admin(admin_principal_id)
        rows = self._connection.execute(
            "SELECT * FROM accounts WHERE tenant_id=? ORDER BY created_at",
            (admin.tenant_id,)).fetchall()
        return [self._row_account(row).as_dict() for row in rows if row is not None]

    def issue_mfa_secret(self, *, admin_principal_id: str, account_id: str) -> str:
        """Reset an account's TOTP secret and enable MFA, returning it once.

        Only an administrator can do this; it is how a promoted approver receives
        a second factor before the role change takes effect.
        """
        admin = self._require_admin(admin_principal_id)
        with self._lock, self._connection:
            target = self._connection.execute(
                "SELECT * FROM accounts WHERE id=? AND tenant_id=?",
                (account_id, admin.tenant_id)).fetchone()
            if target is None:
                raise AccountError("account_missing")
            secret = new_mfa_secret()
            self._connection.execute(
                "UPDATE accounts SET mfa_secret=?, mfa_enabled=1, updated_at=? WHERE id=?",
                (secret, self._now(), account_id))
            self._audit(self._connection, admin.id, "mfa_secret_issued",
                        {"account_id": account_id})
            return secret

    def set_role(self, *, admin_principal_id: str, account_id: str, role: str) -> dict[str, Any]:
        admin = self._require_admin(admin_principal_id)
        if role not in ROLES:
            raise AccountError("role_invalid")
        with self._lock, self._connection:
            target = self._connection.execute(
                "SELECT * FROM accounts WHERE id=? AND tenant_id=?",
                (account_id, admin.tenant_id)).fetchone()
            if target is None:
                raise AccountError("account_missing")
            if role in MFA_ROLES and not target["mfa_secret"]:
                # An approver without a second factor must not be able to log in
                # with a password alone; issue one explicitly first.
                raise AccountError("mfa_enrollment_required")
            self._connection.execute(
                "UPDATE accounts SET role=?, mfa_enabled=?, updated_at=? WHERE id=?",
                (role, 1 if role in MFA_ROLES else int(target["mfa_enabled"]),
                 self._now(), account_id))
            self._audit(self._connection, admin.id, "role_changed",
                        {"account_id": account_id, "role": role})
            updated = self._connection.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return self._row_account(updated).as_dict()

    def disable(self, *, admin_principal_id: str, account_id: str) -> int:
        admin = self._require_admin(admin_principal_id)
        if account_id == admin.id:
            raise AccountError("cannot_disable_self")
        with self._lock, self._connection:
            target = self._connection.execute(
                "SELECT id FROM accounts WHERE id=? AND tenant_id=?",
                (account_id, admin.tenant_id)).fetchone()
            if target is None:
                raise AccountError("account_missing")
            self._connection.execute(
                "UPDATE accounts SET state='disabled', updated_at=? WHERE id=?",
                (self._now(), account_id))
            cursor = self._connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
                (self._now(), account_id))
            self._audit(self._connection, admin.id, "account_disabled",
                        {"account_id": account_id, "revoked": cursor.rowcount})
            return cursor.rowcount

    def create_reset(self, *, admin_principal_id: str, email: str) -> str:
        admin = self._require_admin(admin_principal_id)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT id FROM accounts WHERE lower(email)=? AND tenant_id=?",
                (_email(email), admin.tenant_id)).fetchone()
            if row is None:
                raise AccountError("account_missing")
            token = _token()
            self._connection.execute(
                "INSERT INTO resets(code_hash, account_id, expires_at) VALUES (?,?,?)",
                (_hash_token(token), row["id"], self._now() + RESET_TTL_SECONDS))
            self._audit(self._connection, admin.id, "password_reset_created",
                        {"account_id": str(row["id"])})
            return token

    def reset_password(self, *, token: str, password: str) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM resets WHERE code_hash=?", (_hash_token(token),)).fetchone()
            if row is None or row["used_at"] is not None or int(row["expires_at"]) <= self._now():
                raise AccountError("reset_invalid")
            encoded = hash_password(password)
            self._connection.execute(
                "UPDATE accounts SET password_hash=?, updated_at=? WHERE id=?",
                (encoded, self._now(), row["account_id"]))
            self._connection.execute("UPDATE resets SET used_at=? WHERE code_hash=?",
                                     (self._now(), row["code_hash"]))
            self._connection.execute(
                "UPDATE sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
                (self._now(), row["account_id"]))
            self._audit(self._connection, str(row["account_id"]),
                        "password_reset_completed")

    def audit(self, *, admin_principal_id: str, limit: int = 100) -> list[dict[str, Any]]:
        admin = self._require_admin(admin_principal_id)
        rows = self._connection.execute(
            "SELECT * FROM audit WHERE account_id IN "
            "(SELECT id FROM accounts WHERE tenant_id=?) ORDER BY id DESC LIMIT ?",
            (admin.tenant_id, max(1, min(int(limit), 500)))).fetchall()
        return [{"id": int(row["id"]), "account_id": row["account_id"],
                 "action": str(row["action"]), "at": int(row["at"]),
                 "detail": json.loads(row["detail"] or "{}")} for row in rows]

    def sessions(self, *, principal_id: str) -> list[dict[str, Any]]:
        account = self.account_for_principal(principal_id)
        if account is None:
            raise AccountError("account_missing")
        rows = self._connection.execute(
            "SELECT id, issued_at, expires_at, revoked_at, client FROM sessions "
            "WHERE account_id=? ORDER BY issued_at DESC", (account.id,)).fetchall()
        return [dict(row) for row in rows]


def _email(value: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) < 3 or len(text) > 254 or "@" not in text or text.startswith("@"):
        raise AccountError("email_invalid")
    return text
