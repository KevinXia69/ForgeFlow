"""Independent regression tests for secret redaction in summaries and sidecar logs."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest

from forgeflow.ui_trace import _safe_excerpt

SIDECAR_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dsh_web_sidecar.py"
_spec = importlib.util.spec_from_file_location("dsh_web_sidecar_security", SIDECAR_SCRIPT)
assert _spec is not None and _spec.loader is not None
sidecar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sidecar)

SECRETS = {
    "openai": "sk-" + "a1B2" * 5,
    "openai-project": "sk-proj-" + "A1b2" * 6,
    "github": "ghp_" + "A1b2" * 9,
    "google-api-key": "AIza" + "A1b2" * 9,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9." + "c2lnbmF0dXJl" * 2,
}
TOKEN_URL = "http://127.0.0.1:59596/?token=owner-secret-token"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("secret", SECRETS.values(), ids=SECRETS.keys())
def test_vendor_secret_formats_are_redacted(secret: str) -> None:
    rendered = _safe_excerpt(f"tool output: {secret}")

    assert secret not in rendered
    assert "[REDACTED SECRET]" in rendered


def test_multiple_vendor_secrets_in_one_result_are_all_redacted() -> None:
    rendered = _safe_excerpt("output: " + " ".join(SECRETS.values()))

    for secret in SECRETS.values():
        assert secret not in rendered
    assert rendered.count("[REDACTED SECRET]") == len(SECRETS)


def test_secret_past_the_display_truncation_is_still_redacted() -> None:
    secret = SECRETS["openai"]
    rendered = _safe_excerpt("word " * 60 + secret)

    assert secret not in rendered
    assert len(rendered) <= 221


@pytest.mark.parametrize(
    "value",
    [
        "order-12345",
        "2280_medium_repair_plan_medium",
        "PROJECT_MEMORY.md",
        "case_384585ef",
        "2026-10-22T10:00:00Z",
        "supplier:ACME-42",
    ],
)
def test_ordinary_identifiers_are_not_redacted(value: str) -> None:
    assert _safe_excerpt(value) == value


# docs/NEXT_PHASE_DSH_REUSE.md P0-3 requires UUIDs and commit hashes to survive
# summaries; the generic "32+ long value" fallback currently redacts both.
@pytest.mark.parametrize(
    "value",
    [
        "384585ef-1234-5678-9abc-def012345678",
        "0123456789abcdef0123456789abcdef01234567",
    ],
)
def test_ordinary_uuid_and_commit_hash_are_not_redacted(value: str) -> None:
    rendered = _safe_excerpt(f"artifact {value}")

    assert value in rendered


def test_sidecar_redacts_first_and_later_token_query_parameters() -> None:
    line = (
        "dsh web: http://127.0.0.1:59596/?mode=web&token=first-secret"
        "&token=second-secret (ready)"
    )
    redacted = sidecar._redact_token(line)

    assert "first-secret" not in redacted
    assert "second-secret" not in redacted
    assert redacted.count("token=[REDACTED]") == 2
    assert "mode=web" in redacted
    assert "(ready)" in redacted


def test_secure_log_scrubs_tokens_and_restricts_mode(tmp_path: Path) -> None:
    log = tmp_path / "dsh-web.log"
    log.write_text(
        "[old] starting dsh web\n"
        "dsh web: http://127.0.0.1:59596/?token=old-secret&x=1\n"
        "restart with &token=second-secret still in the log\n",
        encoding="utf-8",
    )
    log.chmod(0o644)

    sidecar._secure_log(log)

    text = log.read_text(encoding="utf-8")
    assert "old-secret" not in text
    assert "second-secret" not in text
    assert "token=[REDACTED]" in text
    assert "starting dsh web" in text
    assert "x=1" in text
    assert _mode(log) == 0o600


def test_secure_log_creates_a_private_log(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "dsh-web.log"

    sidecar._secure_log(log)

    assert log.is_file()
    assert log.read_text(encoding="utf-8") == ""
    assert _mode(log) == 0o600


def test_url_file_keeps_the_token_but_is_private(tmp_path: Path) -> None:
    url_file = tmp_path / "dsh-web-url"

    sidecar._write_url(url_file, TOKEN_URL)

    assert url_file.read_text(encoding="utf-8").strip() == TOKEN_URL
    assert _mode(url_file) == 0o600
