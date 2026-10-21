"""Bundle entry point for the root executor.

The root launcher starts this module inside the task container.  It parses the
deployment contract, builds the credential-bound Odoo connection factory and
the recovery handlers, binds the daemon's Unix socket, and exits cleanly when
the launcher sends SIGTERM or SIGINT.
"""

from __future__ import annotations

import argparse
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
    parser.add_argument("--allow-local-peer", action="store_true")
    parser.add_argument("--product-verifier-container")
    args = parser.parse_args(argv)
    if args.product_verifier_container and args.autonomous:
        parser.error("product verifier requires approval mode")
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
    )


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
            **product_options,
        )
        handlers = Handlers(
            recovery.prepare, recovery.apply, recovery.status,
            reconcile=recovery.reconcile if product_options else None,
            product=recovery.product if product_options else None,
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
