from __future__ import annotations

import importlib.util
import queue
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dsh_web_sidecar.py"
spec = importlib.util.spec_from_file_location("dsh_web_sidecar", SCRIPT)
sidecar = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sidecar)


def test_token_query_is_redacted_from_logs() -> None:
    line = "dsh web: http://127.0.0.1:59596/?token=secret-value-123 (ready)\n"
    redacted = sidecar._redact_token(line)
    assert "secret-value-123" not in redacted
    assert "token=[REDACTED]" in redacted
    assert "dsh web: http://127.0.0.1:59596/?token=[REDACTED] (ready)" in redacted


def test_token_redaction_stops_at_query_separator() -> None:
    line = "http://127.0.0.1:59596/?token=secret-value-123&keep=1\n"
    redacted = sidecar._redact_token(line)
    assert "secret-value-123" not in redacted
    assert redacted == "http://127.0.0.1:59596/?token=[REDACTED]&keep=1\n"


def test_log_is_created_private_and_old_tokens_are_scrubbed(tmp_path) -> None:
    log = tmp_path / "dsh-web.log"
    log.write_text("dsh web: http://127.0.0.1:59596/?token=old-secret\n", encoding="utf-8")
    log.chmod(0o644)
    sidecar._secure_log(log)
    assert "old-secret" not in log.read_text(encoding="utf-8")
    assert "token=[REDACTED]" in log.read_text(encoding="utf-8")
    assert (log.stat().st_mode & 0o777) == 0o600


def test_parse_ready_url_returns_printed_url_with_actual_port() -> None:
    line = "dsh web: http://127.0.0.1:54321/?token=secret-value-123\n"
    assert sidecar._parse_ready_url(line) == "http://127.0.0.1:54321/?token=secret-value-123"


def test_parse_ready_url_accepts_ipv6_and_https() -> None:
    assert (
        sidecar._parse_ready_url("dsh web: http://[::1]:4567/?token=abc\n")
        == "http://[::1]:4567/?token=abc"
    )
    assert (
        sidecar._parse_ready_url("dsh web: https://proxy.example:8443/web/?token=abc\n")
        == "https://proxy.example:8443/web/?token=abc"
    )


def test_parse_ready_url_strips_trailing_punctuation() -> None:
    line = "dsh web: http://127.0.0.1:59596/?token=abc. (ready)\n"
    assert sidecar._parse_ready_url(line) == "http://127.0.0.1:59596/?token=abc"


def test_parse_ready_url_ignores_noise_and_invalid_urls() -> None:
    assert sidecar._parse_ready_url("dsh web: opening the default browser\n") is None
    assert sidecar._parse_ready_url("dsh web: file:///tmp/token\n") is None
    assert sidecar._parse_ready_url("dsh web: http://\n") is None
    assert sidecar._parse_ready_url("listening on http://127.0.0.1:59596/\n") is None


def test_resolve_port_supports_zero_env_and_default() -> None:
    assert sidecar._resolve_port(0, {}) == 0
    assert sidecar._resolve_port(4567, {"FORGEFLOW_DSH_WEB_PORT": "1"}) == 4567
    assert sidecar._resolve_port(None, {"FORGEFLOW_DSH_WEB_PORT": "0"}) == 0
    assert sidecar._resolve_port(None, {}) == sidecar.DEFAULT_PORT


def test_resolve_port_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        sidecar._resolve_port(65536, {})
    with pytest.raises(ValueError):
        sidecar._resolve_port(None, {"FORGEFLOW_DSH_WEB_PORT": "not-a-port"})


def test_resolve_readiness_timeout_supports_env_and_cli() -> None:
    assert sidecar._resolve_readiness_timeout(1.5, {}) == 1.5
    assert sidecar._resolve_readiness_timeout(None, {}) == sidecar.DEFAULT_READINESS_TIMEOUT
    assert (
        sidecar._resolve_readiness_timeout(None, {"FORGEFLOW_DSH_READINESS_TIMEOUT": "7.5"})
        == 7.5
    )


def test_resolve_readiness_timeout_rejects_non_positive_values() -> None:
    for bad in (0, -1, "nope"):
        with pytest.raises(ValueError):
            sidecar._resolve_readiness_timeout(bad, {})


def test_restart_delay_backs_off_and_caps() -> None:
    assert sidecar._restart_delay(0) == 2
    assert [sidecar._restart_delay(n) for n in range(1, 8)] == [2, 4, 8, 16, 32, 60, 60]


def test_cli_accepts_port_zero_timeout_and_once() -> None:
    args = sidecar._build_parser().parse_args(
        ["--port", "0", "--readiness-timeout", "1.5", "--once"]
    )
    assert args.port == 0
    assert args.readiness_timeout == 1.5
    assert args.once is True


def test_await_ready_publishes_private_url_and_redacts_log(tmp_path) -> None:
    lines: queue.Queue[str | None] = queue.Queue()
    lines.put("booting\n")
    lines.put("dsh web: http://127.0.0.1:54321/?token=secret-value-123\n")
    log_path = tmp_path / "dsh-web.log"
    url_file = tmp_path / "dsh-web-url"
    with log_path.open("w", encoding="utf-8") as log:
        ready = sidecar._await_ready(
            lines, timeout=1.0, log=log, url_file=url_file
        )

    assert ready.reason == "ready"
    assert ready.url == "http://127.0.0.1:54321/?token=secret-value-123"
    assert url_file.read_text(encoding="utf-8") == ready.url + "\n"
    assert (url_file.stat().st_mode & 0o777) == 0o600
    log_text = log_path.read_text(encoding="utf-8")
    assert "secret-value-123" not in log_text
    assert "token=[REDACTED]" in log_text


def test_await_ready_times_out_without_printing_url(tmp_path) -> None:
    lines: queue.Queue[str | None] = queue.Queue()
    url_file = tmp_path / "dsh-web-url"
    started = time.monotonic()
    with (tmp_path / "dsh-web.log").open("w", encoding="utf-8") as log:
        ready = sidecar._await_ready(lines, timeout=0.05, log=log, url_file=url_file)

    assert ready.reason == "timeout"
    assert ready.url is None
    assert time.monotonic() - started < 2
    assert not url_file.exists()


def test_await_ready_reports_bind_failure_and_eof(tmp_path) -> None:
    lines: queue.Queue[str | None] = queue.Queue()
    lines.put("Error: listen EADDRINUSE: address already in use 127.0.0.1:59596\n")
    lines.put(None)
    with (tmp_path / "dsh-web.log").open("w", encoding="utf-8") as log:
        ready = sidecar._await_ready(
            lines, timeout=1.0, log=log, url_file=tmp_path / "dsh-web-url"
        )

    assert ready.reason == "exited"
    assert ready.bind_failed is True
    assert ready.url is None
