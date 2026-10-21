#!/usr/bin/env python3
"""Keep the local DSH Web shell running and publish its rotating auth URL.

DSH generates a fresh query-token URL for every Web process. ForgeFlow reads
the URL from an ignored file instead of a copied environment value, so a
sidecar restart only needs a browser refresh (and no API restart).

The supervisor waits for the URL that DSH actually prints, so ``--port 0``
works: the OS picks a free port and the published URL carries the real one.
A process that never prints its URL is treated as a startup failure, and
repeated failures back off instead of spinning when the port is already taken.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, TextIO
from urllib.parse import urlsplit

URL_LINE = re.compile(r"dsh web:\s+(https?://[^\s<>\"']+)")
TOKEN_QUERY = re.compile(r"([?&]token=)[^\s&#]+")
BIND_FAILURE = re.compile(r"EADDRINUSE|address already in use", re.IGNORECASE)
DEFAULT_URL_FILE = Path(".runtime/dsh-web-url")
DEFAULT_LOG_FILE = Path(".runtime/dsh-web.log")
DEFAULT_PORT = 59596
DEFAULT_READINESS_TIMEOUT = 30.0
INITIAL_RESTART_DELAY = 2.0
MAX_RESTART_DELAY = 60.0
STOP_GRACE_SECONDS = 5.0
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


class _Ready(NamedTuple):
    """Outcome of waiting for DSH's printed URL."""

    url: str | None
    reason: str  # "ready" | "timeout" | "exited"
    bind_failed: bool = False


class _RunResult(NamedTuple):
    """Outcome of one supervised DSH process."""

    code: int
    ready: bool
    timed_out: bool
    bind_failed: bool


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _redact_token(value: str) -> str:
    """Keep auth URLs out of logs while preserving the readiness line shape."""
    return TOKEN_QUERY.sub(r"\1[REDACTED]", value)


def _parse_ready_url(line: str) -> str | None:
    """Return the authenticated URL from DSH's readiness line.

    The URL keeps its token: it is written to the 0600 URL file for the local
    iframe, and only the log copy is redacted.  The printed URL is the source
    of truth for the port, which makes ``--port 0`` observable.
    """
    match = URL_LINE.search(line)
    if match is None:
        return None
    candidate = match.group(1).rstrip(_TRAILING_PUNCTUATION)
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return candidate


def _resolve_port(value: int | None, env: Mapping[str, str] | None = None) -> int:
    """Resolve CLI > environment > default; 0 means an OS-assigned port."""
    source = env if env is not None else os.environ
    raw: object = value if value is not None else source.get(
        "FORGEFLOW_DSH_WEB_PORT", str(DEFAULT_PORT)
    )
    try:
        port = int(str(raw))
    except ValueError as exc:
        raise ValueError(f"invalid port {raw!r}; expected an integer from 0 to 65535") from exc
    if not 0 <= port <= 65535:
        raise ValueError(f"port {port} is outside 0..65535")
    return port


def _resolve_readiness_timeout(
    value: float | None, env: Mapping[str, str] | None = None
) -> float:
    """Resolve the URL readiness deadline; it must be a positive number."""
    source = env if env is not None else os.environ
    raw: object = value if value is not None else source.get(
        "FORGEFLOW_DSH_READINESS_TIMEOUT", str(DEFAULT_READINESS_TIMEOUT)
    )
    try:
        timeout = float(str(raw))
    except ValueError as exc:
        raise ValueError(f"invalid readiness timeout {raw!r}; expected a positive number") from exc
    if timeout <= 0:
        raise ValueError(f"readiness timeout {timeout} must be greater than 0")
    return timeout


def _restart_delay(failures: int) -> float:
    """Exponential backoff for processes that exit before becoming ready."""
    if failures <= 1:
        return INITIAL_RESTART_DELAY
    exponent = min(failures - 1, 5)
    return min(MAX_RESTART_DELAY, INITIAL_RESTART_DELAY * (2**exponent))


def _secure_log(path: Path) -> None:
    """Create the log 0600 and scrub tokens left by earlier versions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        original = path.read_text(encoding="utf-8", errors="replace")
        redacted = _redact_token(original)
        if redacted != original:
            temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
            temporary.write_text(redacted, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
    else:
        path.touch()
    path.chmod(0o600)


def _write_url(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(url + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _pump(stream: TextIO, lines: queue.Queue[str | None]) -> None:
    """Move DSH stdout into a queue so readiness can be waited on with a timeout."""
    try:
        for line in stream:
            lines.put(line)
    finally:
        lines.put(None)


def _await_ready(
    lines: queue.Queue[str | None],
    *,
    timeout: float,
    log: TextIO,
    url_file: Path,
) -> _Ready:
    """Write startup output and return once DSH prints its auth URL.

    Logged lines are redacted; the URL file receives the unredacted URL.  The
    timeout bounds only the wait for readiness, not the whole process lifetime.
    """
    deadline = time.monotonic() + timeout
    bind_failed = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _Ready(None, "timeout", bind_failed)
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return _Ready(None, "timeout", bind_failed)
        if line is None:
            return _Ready(None, "exited", bind_failed)
        if BIND_FAILURE.search(line):
            bind_failed = True
        log.write(_redact_token(line))
        log.flush()
        url = _parse_ready_url(line)
        if url is not None:
            _write_url(url_file, url)
            return _Ready(url, "ready", bind_failed)


def _drain(lines: queue.Queue[str | None], log: TextIO) -> None:
    """Keep copying output (redacted) until the process closes stdout."""
    while True:
        line = lines.get()
        if line is None:
            return
        log.write(_redact_token(line))
        log.flush()


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _supervise_process(
    process: subprocess.Popen[str],
    *,
    timeout: float,
    log: TextIO,
    url_file: Path,
) -> _RunResult:
    """Wait for readiness, then stream output until this one process exits."""
    assert process.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True).start()
    ready = _await_ready(lines, timeout=timeout, log=log, url_file=url_file)
    if ready.url is None:
        if ready.reason == "timeout":
            _terminate(process)
        code = process.wait()
        return _RunResult(
            code=code,
            ready=False,
            timed_out=ready.reason == "timeout",
            bind_failed=ready.bind_failed,
        )
    print(
        f"DSH web ready; auth URL updated at {url_file}",
        file=sys.stderr,
        flush=True,
    )
    _drain(lines, log)
    return _RunResult(
        code=process.wait(),
        ready=True,
        timed_out=False,
        bind_failed=ready.bind_failed,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Supervise the local DSH Web shell.")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Web port passed to DSH; 0 asks the OS for a free port "
            f"(default: $FORGEFLOW_DSH_WEB_PORT or {DEFAULT_PORT})."
        ),
    )
    parser.add_argument(
        "--readiness-timeout",
        type=float,
        default=None,
        dest="readiness_timeout",
        help=(
            "Seconds to wait for DSH's printed URL before restarting "
            f"(default: $FORGEFLOW_DSH_READINESS_TIMEOUT or {DEFAULT_READINESS_TIMEOUT:g})."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one DSH process and exit when it stops instead of restarting.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _load_env(Path(".env"))
    _load_env(Path(".runtime/dsh.env"))
    node = os.environ.get("FORGEFLOW_DSH_NODE", "node")
    cli = os.environ.get("FORGEFLOW_DSH_CLI", "")
    patch = os.environ.get("FORGEFLOW_DSH_PATCH", "")
    home = os.environ.get("FORGEFLOW_DSH_HOME", ".runtime/dsh-home")
    try:
        port = _resolve_port(args.port)
        readiness_timeout = _resolve_readiness_timeout(args.readiness_timeout)
    except ValueError as exc:
        print(f"DSH sidecar: {exc}", file=sys.stderr)
        return 2
    url_file = Path(os.environ.get("FORGEFLOW_DSH_WEB_URL_FILE", str(DEFAULT_URL_FILE)))
    log_file = Path(os.environ.get("FORGEFLOW_DSH_WEB_LOG", str(DEFAULT_LOG_FILE)))
    if not cli or not patch:
        print("DSH sidecar: FORGEFLOW_DSH_CLI/FORGEFLOW_DSH_PATCH are required", file=sys.stderr)
        return 2

    _secure_log(log_file)
    command = [
        node,
        cli,
        "--profile",
        "web",
        "--patch",
        patch,
        "--no-open",
        "--port",
        str(port),
    ]
    env = dict(os.environ)
    env["DSH_HOME"] = home
    stopping = threading.Event()
    current: subprocess.Popen[str] | None = None
    failures = 0

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()
        if current is not None and current.poll() is None:
            current.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    while not stopping.is_set():
        try:
            process = subprocess.Popen(
                command,
                cwd=Path.cwd(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            print(f"DSH sidecar: cannot start DSH: {exc}", file=sys.stderr)
            return 2
        current = process
        try:
            with log_file.open("a", encoding="utf-8") as log:
                log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting dsh web\n")
                log.flush()
                result = _supervise_process(
                    process,
                    timeout=readiness_timeout,
                    log=log,
                    url_file=url_file,
                )
        finally:
            current = None
            try:
                url_file.unlink()
            except FileNotFoundError:
                pass

        if stopping.is_set():
            break
        if result.ready:
            failures = 0
            delay = INITIAL_RESTART_DELAY
            detail = "exited"
        else:
            failures += 1
            delay = _restart_delay(failures)
            if result.timed_out:
                detail = f"did not print a URL within {readiness_timeout:g}s"
            elif result.bind_failed:
                detail = "could not bind its port"
            else:
                detail = "exited before printing a URL"
        if args.once:
            print(
                f"DSH web {detail} (code {result.code}); not restarting (--once)",
                file=sys.stderr,
                flush=True,
            )
            return 0 if result.ready and result.code == 0 else 1
        if result.bind_failed:
            print(
                "DSH sidecar: port is busy; use --port 0 for an OS-assigned port "
                "or stop the process holding it",
                file=sys.stderr,
                flush=True,
            )
        print(
            f"DSH web {detail} (code {result.code}); restarting in {delay:g}s",
            file=sys.stderr,
            flush=True,
        )
        if stopping.wait(delay):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
