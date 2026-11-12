"""Bundle entry point for the root executor.

The root launcher starts this module inside the task container.  It parses the
deployment contract, builds the credential-bound Odoo connection factory and
the recovery handlers, binds the daemon's Unix socket, and exits cleanly when
the launcher sends SIGTERM or SIGINT.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import os
import pwd
import signal
import stat
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forgeflow.executor.daemon import ExecutorServer, Handlers
from forgeflow.executor.handlers import RecoveryHandlers

_PATH_FLAGS = (
    ("--socket", "socket"),
    ("--store", "store"),
    ("--bundle", "bundle"),
    ("--key", "key"),
    ("--approval-store", "approval_store"),
    ("--policy-file", "policy_file"),
    ("--investigation-event-file", "investigation_event_file"),
)


@dataclass(frozen=True)
class ExecutorConfig:
    socket_path: Path
    store: Path
    bundle: Path
    key_path: Path
    agent_user: str
    approval_store: Path | None = None
    policy_file: Path | None = None
    database: str = "bench"
    host: str = "127.0.0.1"
    port: int = 8069
    require_grant: bool = True
    allow_local_peer: bool = False
    product_verifier_container: str | None = None
    retention_profile: str | None = None
    investigation: bool = False
    investigation_event_file: Path | None = None


def config_from_args(argv: Sequence[str] | None = None) -> ExecutorConfig:
    """Parse and validate the launcher's deployment arguments."""
    parser = argparse.ArgumentParser(prog="forgeflow-executor")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--agent-user", required=True)
    parser.add_argument("--approval-store", type=Path)
    parser.add_argument("--policy-file", type=Path)
    parser.add_argument("--database", default="bench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8069, type=int)
    parser.add_argument("--autonomous", action="store_true")
    parser.add_argument("--investigation", action="store_true")
    parser.add_argument("--investigation-event-file", type=Path)
    parser.add_argument("--allow-local-peer", action="store_true")
    parser.add_argument("--product-verifier-container")
    parser.add_argument("--retention-profile", choices=("fixed-totals-retention-v1",))
    args = parser.parse_args(argv)
    if args.product_verifier_container and args.autonomous:
        parser.error("product verifier requires approval mode")
    if args.investigation and (args.autonomous or args.product_verifier_container
                               or not args.policy_file or not args.investigation_event_file):
        parser.error("investigation requires a policy, a trusted event file, and prepare-only execution")
    if args.investigation_event_file and not args.investigation:
        parser.error("investigation event file requires investigation mode")
    for flag, attribute in _PATH_FLAGS:
        path = getattr(args, attribute)
        if path is not None and not path.is_absolute():
            parser.error(f"{flag} must be an absolute path")
    return ExecutorConfig(
        socket_path=args.socket,
        store=args.store,
        bundle=args.bundle,
        key_path=args.key,
        agent_user=args.agent_user,
        approval_store=args.approval_store,
        policy_file=args.policy_file,
        database=args.database,
        host=args.host,
        port=args.port,
        require_grant=not args.autonomous,
        allow_local_peer=args.allow_local_peer,
        product_verifier_container=args.product_verifier_container,
        retention_profile=args.retention_profile,
        investigation=args.investigation,
        investigation_event_file=args.investigation_event_file,
    )


def read_investigation_event(path: Path | None) -> dict[str, str]:
    """Read an executor-owned event; the production executor runs as root."""
    if path is None:
        raise RuntimeError("investigation_event_required")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise RuntimeError("investigation_event_untrusted") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o400 or not 0 < info.st_size <= 16 * 1024):
            raise RuntimeError("investigation_event_untrusted")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(16 * 1024 + 1)
        if len(raw) != info.st_size:
            raise RuntimeError("investigation_event_untrusted")
        text = raw.decode("utf-8")
        if not text.strip() or "\x00" in text:
            raise RuntimeError("investigation_event_untrusted")
        return {"text": text, "sha256": hashlib.sha256(raw).hexdigest()}
    except (OSError, UnicodeError):
        raise RuntimeError("investigation_event_untrusted") from None
    finally:
        os.close(fd)


def read_key_text(path: Path) -> str:
    """Read the Odoo write key without following links or exposing its bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise RuntimeError("key_not_readable") from None
    try:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o400:
                raise RuntimeError("key_not_readable")
            raw = os.read(fd, info.st_size + 1)
        except OSError:
            raise RuntimeError("key_not_readable") from None
    finally:
        os.close(fd)
    if len(raw) != info.st_size:
        raise RuntimeError("key_not_readable")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("key_not_readable") from None
    return text[:-1] if text.endswith("\n") else text


def connection_factory(config: ExecutorConfig) -> Callable[[], object]:
    """Return a lazy factory; ``odoolib`` is imported only when connecting."""

    def connect() -> object:
        import odoolib

        return odoolib.get_connection(
            hostname=config.host,
            protocol="json2",
            port=config.port,
            database=config.database,
            password=read_key_text(config.key_path),
        )

    return connect


def _agent_uid(user: str) -> int:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        raise RuntimeError("agent_user_not_found") from None


def build_server(
    config: ExecutorConfig,
    *,
    handlers: Handlers | None = None,
    peer_check: Callable[[Any, int], None] | None = None,
) -> ExecutorServer:
    """Build the daemon around recovery handlers, unless handlers are injected."""
    if handlers is None:
        product_options = {}
        if config.investigation:
            product_options["investigation_event"] = read_investigation_event(config.investigation_event_file)
        if config.product_verifier_container:
            if not config.require_grant:
                raise ValueError("product_verifier_requires_approval_mode")
            # This host-only module is intentionally not in benchmark bundles.
            from forgeflow.product.verifier import OriginalProductVerifier

            product_options["product_verifier"] = OriginalProductVerifier(config.product_verifier_container)
        recovery = RecoveryHandlers(
            connection_factory=connection_factory(config),
            store=config.store,
            bundle=config.bundle,
            approval_store=config.approval_store,
            require_grant=config.require_grant,
            policy_file=config.policy_file,
            retention_profile=config.retention_profile,
            investigation_mode=config.investigation,
            **product_options,
        )
        handlers = Handlers(
            recovery.prepare, recovery.apply, recovery.status,
            reconcile=recovery.reconcile if config.product_verifier_container else None,
            product=recovery.product if config.product_verifier_container else None,
            investigation=recovery.investigation if config.investigation else None,
        )
    options: dict[str, Any] = {
        "agent_uid": _agent_uid(config.agent_user),
        # The launcher waits for this file, so the daemon must always publish it.
        "ready_path": config.socket_path.with_name("executor.ready"),
    }
    if "peer_check" in inspect.signature(ExecutorServer.__init__).parameters:
        options["peer_check"] = peer_check
    return ExecutorServer(config.socket_path, handlers, **options)


def run(
    argv: Sequence[str] | None = None,
    *,
    stop_event: threading.Event | None = None,
    serve: Callable[[threading.Event], None] | None = None,
) -> int:
    """Run the executor until stopped; return a process exit code."""
    config = config_from_args(argv)
    stop = stop_event if stop_event is not None else threading.Event()
    peer_check = (lambda *_args: None) if config.allow_local_peer else None
    server = build_server(config, peer_check=peer_check)

    def request_stop(*_args: object) -> None:
        stop.set()

    previous: dict[int, Any] = {}
    try:
        try:
            server.bind()
        except (OSError, RuntimeError) as exc:
            print(f"executor: {exc}", file=sys.stderr)
            return 2
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[signum] = signal.signal(signum, request_stop)
            except (OSError, ValueError):
                pass
        (serve or server.serve_forever)(stop)
        return 0
    finally:
        server.close()
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    raise SystemExit(run())
