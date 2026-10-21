"""Environment-backed settings for the read-only ForgeFlow product API.

Error messages name the offending variable but never echo its value, and
``repr(Settings)`` hides the secret and the invite codes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DATA_ROOT_KEY = "FORGEFLOW_DATA_ROOT"
SECRET_KEY = "FORGEFLOW_SESSION_SECRET"
INVITE_KEY = "FORGEFLOW_INVITE_CODES"
TTL_KEY = "FORGEFLOW_SESSION_TTL_SEC"
SECURE_KEY = "FORGEFLOW_SECURE_COOKIE"
APPROVALS_KEY = "FORGEFLOW_APPROVALS_ROOT"
RUNNER_KEY = "FORGEFLOW_RUNNER_ENABLED"
KNOWLEDGE_KEY = "FORGEFLOW_KNOWLEDGE_ROOT"
DEPLOYMENT_KEY = "FORGEFLOW_DEPLOYMENT"
PUBLIC_RUNS_KEY = "FORGEFLOW_PUBLIC_RUNS"
TRUSTED_HOSTS_KEY = "FORGEFLOW_TRUSTED_HOSTS"
COOKIE_NAME = "ff_session"
DEFAULT_TTL_SEC = 12 * 60 * 60
MIN_SECRET_LEN = 32
MIN_INVITE_LEN = 16
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated configuration shared by the auth and app modules."""

    data_root: Path = Path("./runs")
    session_secret: str = field(default="", repr=False)
    invite_codes: tuple[str, ...] = field(default=(), repr=False)
    session_ttl_sec: int = DEFAULT_TTL_SEC
    cookie_name: str = COOKIE_NAME
    secure_cookie: bool = True
    approvals_root: Path | None = None
    runner_enabled: bool = False
    knowledge_root: Path | None = None
    deployment: str = "local"
    public_runs: tuple[str, ...] = ()
    trusted_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_root", Path(self.data_root).expanduser())
        knowledge = self.knowledge_root or (Path(__file__).resolve().parents[3])
        object.__setattr__(self, "knowledge_root", Path(knowledge).expanduser())
        root = self.approvals_root or (self.data_root.parent / "approvals")
        object.__setattr__(self, "approvals_root", Path(root).expanduser())
        object.__setattr__(self, "invite_codes", tuple(self.invite_codes))
        object.__setattr__(self, "public_runs", tuple(self.public_runs))
        object.__setattr__(self, "trusted_hosts", tuple(self.trusted_hosts))
        if not isinstance(self.session_secret, str) or len(self.session_secret) < MIN_SECRET_LEN:
            raise ValueError(f"{SECRET_KEY} must be set to at least {MIN_SECRET_LEN} characters.")
        if not self.invite_codes:
            raise ValueError(f"{INVITE_KEY} must contain at least one invite code.")
        if not all(
            isinstance(code, str) and len(code) >= MIN_INVITE_LEN
            for code in self.invite_codes
        ):
            raise ValueError(
                f"Every {INVITE_KEY} entry must be at least {MIN_INVITE_LEN} characters."
            )
        if type(self.session_ttl_sec) is not int or self.session_ttl_sec <= 0:
            raise ValueError(f"{TTL_KEY} must be a positive integer.")
        if not isinstance(self.secure_cookie, bool):
            raise ValueError(f"{SECURE_KEY} must be a boolean.")
        if self.deployment not in ("local", "review"):
            raise ValueError(f"{DEPLOYMENT_KEY} must be local or review.")
        if not all(
            isinstance(entry, str) and entry and entry != "**" for entry in self.public_runs
        ):
            raise ValueError(f"{PUBLIC_RUNS_KEY} entries must be run ids or '*'.")
        if not isinstance(self.cookie_name, str) or not self.cookie_name:
            raise ValueError("cookie_name must not be empty.")


def create_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build validated settings from ``env`` (the process environment by default)."""
    source = os.environ if env is None else env
    codes = tuple(
        part.strip() for part in source.get(INVITE_KEY, "").split(",") if part.strip()
    )
    ttl_raw = source.get(TTL_KEY, "").strip()
    try:
        ttl = int(ttl_raw) if ttl_raw else DEFAULT_TTL_SEC
    except ValueError:
        raise ValueError(f"{TTL_KEY} must be a positive integer.") from None
    runner_raw = source.get(RUNNER_KEY, "").strip().lower()
    if not runner_raw or runner_raw in _FALSE:
        runner = False
    elif runner_raw in _TRUE:
        runner = True
    else:
        raise ValueError(f"{RUNNER_KEY} must be a boolean (true or false).")
    secure_raw = source.get(SECURE_KEY, "").strip().lower()
    if not secure_raw or secure_raw in _TRUE:
        secure = True
    elif secure_raw in _FALSE:
        secure = False
    else:
        raise ValueError(f"{SECURE_KEY} must be a boolean (true or false).")
    public_runs = tuple(
        part.strip() for part in source.get(PUBLIC_RUNS_KEY, "").split(",") if part.strip()
    )
    trusted_hosts = tuple(
        part.strip() for part in source.get(TRUSTED_HOSTS_KEY, "").split(",") if part.strip()
    )
    render_host = source.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_host and render_host not in trusted_hosts:
        trusted_hosts += (render_host,)
    return Settings(
        session_secret=source.get(SECRET_KEY, "").strip(),
        invite_codes=codes,
        data_root=Path(source.get(DATA_ROOT_KEY, "").strip() or "./runs"),
        session_ttl_sec=ttl,
        secure_cookie=secure,
        approvals_root=source.get(APPROVALS_KEY, "").strip() or None,
        runner_enabled=runner,
        knowledge_root=source.get(KNOWLEDGE_KEY, "").strip() or None,
        deployment=source.get(DEPLOYMENT_KEY, "").strip() or "local",
        public_runs=public_runs,
        trusted_hosts=trusted_hosts,
    )
