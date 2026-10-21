"""Private, local-only model profile settings for the ForgeFlow API.

The profile and API key live in one JSON file below the approvals root, which
the hosted review deployment never reads. The key is written with mode 0600
through an exclusive temporary file and is never exposed by ``masked()`` or by
``repr(ModelSettings)``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

FILENAME = "model-settings.json"
MAX_BASE_URL_CHARS = 2048
MAX_MODEL_CHARS = 120
MIN_API_KEY_CHARS = 8
MAX_API_KEY_CHARS = 512
PROFILES: dict[str, dict[str, str]] = {
    "nemotron": {
        "base_url": "https://api.tokenfactory.nebius.com/v1",
        "model": "nvidia/Nemotron-3.5-Super-VL",
    },
    "deepseek_flash": {
        "label": "DeepSeek V4.1 Flash 1017 (rolling alias)",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
    },
    "gemini": {
        "label": "Google Gemini (OpenAI-compatible)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3-flash-preview",
    },
    "custom": {
        "base_url": "",
        "model": "",
    },
}


class ModelSettingsError(Exception):
    """A stable, client-safe model settings failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ModelSettings:
    """A saved OpenAI-compatible model profile; the key stays out of repr."""

    profile: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    updated_at: str


def build_settings(
    profile: object,
    base_url: object,
    model: object,
    api_key: object,
    now: datetime | float | int | str | None = None,
) -> ModelSettings:
    """Validate a transient model configuration without persisting it.

    The review instance uses this path: each browser sends its own key with
    the request and the server never writes it to disk.
    """
    return ModelSettings(
        profile=_validate_profile(profile),
        base_url=_validate_base_url(base_url),
        model=_validate_model(model),
        api_key=_validate_api_key(api_key),
        updated_at=_stamp(now),
    )


def _stamp(now: datetime | float | int | str | None) -> str:
    if now is None:
        return datetime.now(UTC).isoformat()
    if isinstance(now, datetime):
        stamp = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        return stamp.isoformat()
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, UTC).isoformat()
    return str(now)


def _validate_profile(profile: object) -> str:
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ModelSettingsError("invalid_profile")
    return profile


def _validate_base_url(base_url: object) -> str:
    if not isinstance(base_url, str):
        raise ModelSettingsError("invalid_base_url")
    if (
        not base_url
        or len(base_url) > MAX_BASE_URL_CHARS
        or any(character.isspace() for character in base_url)
    ):
        raise ModelSettingsError("invalid_base_url")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        raise ModelSettingsError("invalid_base_url") from None
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or (port is not None and not 0 < port < 65536)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelSettingsError("invalid_base_url")
    return base_url


def _validate_model(model: object) -> str:
    if (
        not isinstance(model, str)
        or not 1 <= len(model) <= MAX_MODEL_CHARS
        or any(character.isspace() for character in model)
    ):
        raise ModelSettingsError("invalid_model")
    return model


def _validate_api_key(api_key: object) -> str:
    if (
        not isinstance(api_key, str)
        or not MIN_API_KEY_CHARS <= len(api_key) <= MAX_API_KEY_CHARS
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in api_key
        )
    ):
        raise ModelSettingsError("invalid_api_key")
    return api_key


class ModelSettingsStore:
    """One private ``model-settings.json`` under a root directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.path = self.root / FILENAME

    def load(self) -> ModelSettings | None:
        """Return the saved settings, or None when absent or unreadable."""
        if self.root.is_symlink() or self.path.is_symlink() or not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        profile = payload.get("profile")
        base_url = payload.get("base_url")
        model = payload.get("model")
        api_key = payload.get("api_key", "")
        updated_at = payload.get("updated_at", "")
        if not isinstance(profile, str) or profile not in PROFILES:
            return None
        if not all(
            isinstance(value, str) for value in (base_url, model, api_key, updated_at)
        ):
            return None
        if not base_url or not model:
            return None
        return ModelSettings(
            profile=profile,
            base_url=base_url,
            model=model,
            api_key=api_key,
            updated_at=updated_at,
        )

    def save(
        self,
        profile: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        now: datetime | float | int | str | None = None,
    ) -> ModelSettings:
        """Validate and atomically persist a profile, keeping an existing key."""
        profile = _validate_profile(profile)
        base_url = _validate_base_url(base_url)
        model = _validate_model(model)
        existing = self.load()
        if api_key is None or api_key == "":
            if existing is None or not existing.api_key:
                raise ModelSettingsError("api_key_required")
            api_key = existing.api_key
        else:
            api_key = _validate_api_key(api_key)
        stored = ModelSettings(
            profile=profile,
            base_url=base_url,
            model=model,
            api_key=api_key,
            updated_at=_stamp(now),
        )
        self._write(
            {
                "profile": stored.profile,
                "base_url": stored.base_url,
                "model": stored.model,
                "api_key": stored.api_key,
                "updated_at": stored.updated_at,
            }
        )
        return stored

    def clear(self) -> None:
        """Remove the saved file; a missing file is already clear."""
        if self.root.is_symlink():
            raise ModelSettingsError("settings_unavailable")
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            raise ModelSettingsError("settings_unavailable") from None

    def masked(self) -> dict[str, Any]:
        """Return the client-safe view, never the key."""
        stored = self.load()
        if stored is None:
            return {
                "configured": False,
                "profile": None,
                "base_url": "",
                "model": "",
                "has_key": False,
                "updated_at": None,
            }
        return {
            "configured": True,
            "profile": stored.profile,
            "base_url": stored.base_url,
            "model": stored.model,
            "has_key": bool(stored.api_key),
            "updated_at": stored.updated_at or None,
        }

    def _prepare_root(self) -> None:
        try:
            if self.root.is_symlink():
                raise ModelSettingsError("settings_unavailable")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
        except OSError:
            raise ModelSettingsError("settings_unavailable") from None

    def _write(self, payload: dict[str, Any]) -> None:
        self._prepare_root()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        descriptor = -1
        temporary: Path | None = None
        try:
            # mkstemp opens the temp file with O_CREAT | O_EXCL in this directory.
            descriptor, name = tempfile.mkstemp(
                dir=self.root, prefix=".model-settings-", suffix=".tmp"
            )
            temporary = Path(name)
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "wb")
            descriptor = -1
            with handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise ModelSettingsError("settings_unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass



PLATFORM_ENV = {
    "profile": "FORGEFLOW_MODEL_PROFILE",
    "base_url": "FORGEFLOW_MODEL_BASE_URL",
    "model": "FORGEFLOW_MODEL_NAME",
    "api_key": "FORGEFLOW_MODEL_API_KEY",
}


def platform_settings(env: Mapping[str, str] | None = None) -> ModelSettings | None:
    """Read the operator-provided model from the environment.

    The hosted instance uses this when a browser does not supply its own key.
    The key is only ever held in memory and is never written to the store or
    returned by an endpoint.
    """
    source = os.environ if env is None else env
    api_key = (
        source.get(PLATFORM_ENV["api_key"], "").strip()
        or source.get("NEBIUS_API_KEY", "").strip()
    )
    if not api_key:
        return None
    profile = source.get(PLATFORM_ENV["profile"], "").strip() or "nemotron"
    if profile not in PROFILES:
        profile = "nemotron"
    spec = PROFILES[profile]
    base_url = source.get(PLATFORM_ENV["base_url"], "").strip() or spec["base_url"]
    model = source.get(PLATFORM_ENV["model"], "").strip() or spec["model"]
    try:
        return build_settings(profile, base_url, model, api_key)
    except ModelSettingsError:
        return None


def platform_masked(settings: ModelSettings | None) -> dict[str, Any]:
    """Describe the platform model without ever returning its key."""
    if settings is None:
        return {"configured": False, "source": "none", "can_override": True}
    return {
        "configured": True,
        "source": "platform",
        "profile": settings.profile,
        "base_url": settings.base_url,
        "model": settings.model,
        "has_key": True,
        "key_hidden": True,
        "can_override": True,
        "updated_at": settings.updated_at,
    }
