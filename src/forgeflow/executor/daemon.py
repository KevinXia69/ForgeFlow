"""Root-owned executor endpoint: socket lifecycle, peer gate, and dispatch."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pwd
import signal
import socket
import stat
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgeflow.executor import protocol
from forgeflow.executor.peer import PeerError, require_peer


@dataclass
class Handlers:
    prepare: Callable[[str, dict[str, Any]], dict[str, Any]]
    apply: Callable[[str, str, dict[str, Any] | None], dict[str, Any]]
    status: Callable[[str, str], dict[str, Any]]
    health: Callable[[], dict[str, Any]] | None = None
    reconcile: Callable[[str, str], dict[str, Any]] | None = None
    product: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None


def _fsync_dir(path: Path) -> None:
    with suppress(OSError):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class ExecutorServer:
    def __init__(self, socket_path: Path, handlers: Handlers, *, agent_uid: int,
                 ready_path: Path | None = None, lock_path: Path | None = None,
                 max_connections: int = 32,
                 peer_check: Callable[[socket.socket, int], None] | None = None) -> None:
        self.socket_path, self.handlers, self.agent_uid = Path(socket_path), handlers, agent_uid
        self.peer_check = peer_check
        self.ready_path = Path(ready_path) if ready_path is not None else None
        self.lock_path = Path(lock_path or self.socket_path.with_name("executor.lock"))
        self.max_connections, self.chown_skipped = max_connections, False
        self._listener = self._lock_fd = self._socket_ino = self._temp = None
        self._started_at, self._ready_written, self._closed = "", False, False
        self._close_lock = threading.Lock()

    def bind(self) -> None:
        if self._listener is not None:
            raise RuntimeError("executor_already_running")
        self._closed, self.chown_skipped = False, False
        parent = self.socket_path.parent
        if not parent.exists():
            parent.mkdir(mode=0o711, parents=True)
            os.chmod(parent, 0o711)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError("executor_already_running") from None
        self._lock_fd = fd
        try:
            info = None
            with suppress(FileNotFoundError):
                info = os.lstat(self.socket_path)
            if info is not None and not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError("socket_path_not_socket")
            if info is not None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.25)
                    try:
                        probe.connect(str(self.socket_path))
                    except OSError as exc:
                        if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                            raise RuntimeError("executor_already_running") from None
                        with suppress(OSError):
                            os.unlink(self.socket_path)
                    else:
                        raise RuntimeError("executor_already_running")
            try:
                agent_gid = pwd.getpwuid(self.agent_uid).pw_gid
            except KeyError:
                raise RuntimeError("agent_user_not_found") from None
            self._started_at = datetime.now(UTC).isoformat()
            temp = self._temp = self.socket_path.with_name(f".executor.{os.getpid()}.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                previous = os.umask(0o077)
                try:
                    listener.bind(str(temp))
                finally:
                    os.umask(previous)
                os.chmod(temp, 0o660)
                try:
                    os.chown(temp, 0, agent_gid)
                except PermissionError:
                    self.chown_skipped = True
                listener.listen(8)
                os.replace(temp, self.socket_path)
                self._temp = None
                self._socket_ino = os.lstat(self.socket_path).st_ino
                _fsync_dir(self.socket_path.parent)
            except BaseException:
                listener.close()
                with suppress(OSError):
                    temp.unlink()
                raise
            self._listener = listener
            if self.ready_path is not None:
                ready_temp = self.ready_path.with_name(f".{self.ready_path.name}.{os.getpid()}.tmp")
                self._temp = ready_temp
                ready_temp.write_text(json.dumps({
                    "schema_version": protocol.SCHEMA_VERSION,
                    "pid": os.getpid(),
                    "socket": str(self.socket_path),
                    "socket_ino": self._socket_ino,
                    "started_at": self._started_at,
                }) + "\n")
                ready_temp.chmod(0o644)
                os.replace(ready_temp, self.ready_path)
                self._temp = None
                self._ready_written = True
                _fsync_dir(self.ready_path.parent)
        except BaseException:
            self.close()
            raise

    def serve_once(self, timeout: float | None = None) -> bool:
        if self._listener is None:
            raise RuntimeError("executor_not_bound")
        self._listener.settimeout(timeout)
        try:
            connection, _ = self._listener.accept()
        except (TimeoutError, BlockingIOError):
            return False
        self._handle(connection)
        return True

    def _handle(self, connection: socket.socket) -> None:
        op = "unknown"
        try:
            with connection, connection.makefile("rwb", buffering=0) as stream:
                try:
                    try:
                        (self.peer_check or require_peer)(connection, self.agent_uid)
                    except PeerError as exc:
                        raise protocol.ProtocolError(exc.code, 403) from None
                    op, fields = protocol.validate_envelope(protocol.read_frame(stream))
                    if op == "health":
                        payload = (
                            self.handlers.health()
                            if self.handlers.health is not None
                            else {"pid": os.getpid(), "socket": str(self.socket_path),
                                  "capabilities": {"product_versions": self.handlers.product is not None}}
                        )
                    else:
                        run_id = fields.get("run_id")
                        if not isinstance(run_id, str):
                            raise protocol.ProtocolError("invalid_field", 400)
                        if op == "prepare":
                            request = fields.get("request")
                            if not isinstance(request, dict):
                                raise protocol.ProtocolError("invalid_field", 400)
                            payload = self.handlers.prepare(run_id, request)
                        elif op == "product":
                            if self.handlers.product is None:
                                raise protocol.ProtocolError("product_operations_disabled", 404)
                            payload = self.handlers.product(run_id, fields["action"], fields["payload"])
                        else:
                            plan_id = fields.get("plan_id")
                            if not isinstance(plan_id, str):
                                raise protocol.ProtocolError("invalid_field", 400)
                            approval = fields.get("approval")
                            if op == "apply":
                                if approval is not None and not isinstance(approval, dict):
                                    raise protocol.ProtocolError("invalid_field", 400)
                                payload = self.handlers.apply(run_id, plan_id, approval)
                            elif op == "reconcile":
                                if self.handlers.reconcile is None:
                                    raise protocol.ProtocolError("product_reconciliation_disabled", 404)
                                payload = self.handlers.reconcile(run_id, plan_id)
                            else:
                                payload = self.handlers.status(run_id, plan_id)
                    response = {**payload, "schema_version": protocol.SCHEMA_VERSION,
                                "ok": True, "op": op}
                except protocol.ProtocolError as exc:
                    response = protocol.error_payload(op, exc)
                except Exception:
                    response = protocol.error_payload(
                        op, protocol.ProtocolError("internal_error", 500)
                    )
                try:
                    protocol.write_frame(stream, response)
                except protocol.ProtocolError as exc:
                    with suppress(OSError, protocol.ProtocolError):
                        protocol.write_frame(stream, protocol.error_payload(op, exc))
        except Exception:
            return

    def serve_forever(self, stop_event: threading.Event) -> None:
        if self._listener is None:
            raise RuntimeError("executor_not_bound")
        try:
            while not stop_event.is_set() and not self._closed:
                try:
                    self.serve_once(0.2)
                except Exception:
                    if self._closed:
                        break
        finally:
            self.close()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._listener is not None:
                with suppress(OSError):
                    self._listener.close()
                self._listener = None
            if self._socket_ino is not None:
                with suppress(OSError):
                    info = os.lstat(self.socket_path)
                    if stat.S_ISSOCK(info.st_mode) and info.st_ino == self._socket_ino:
                        os.unlink(self.socket_path)
                self._socket_ino = None
            for path, owned in ((self.ready_path, self._ready_written), (self._temp, True)):
                if path is not None and owned:
                    with suppress(OSError):
                        path.unlink()
            self._ready_written, self._temp = False, None
            if self._lock_fd is not None:
                with suppress(OSError):
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                with suppress(OSError):
                    os.close(self._lock_fd)
                self._lock_fd = None


def _unavailable(*_args: object) -> dict[str, Any]:
    raise protocol.ProtocolError("unavailable", 503, retryable=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forgeflow-executor")
    parser.add_argument("--socket", default="/run/forgeflow/executor.sock")
    parser.add_argument("--agent-user", default="forgeflow")
    parser.add_argument("--ready-file")
    parser.add_argument("--lock-file")
    args = parser.parse_args(argv)
    socket_path = Path(args.socket)
    try:
        agent_uid = pwd.getpwnam(args.agent_user).pw_uid
    except KeyError:
        print(f"executor: unknown agent user: {args.agent_user}", file=sys.stderr)
        return 2
    ready_path = Path(args.ready_file or socket_path.with_name("executor.ready.json"))
    server = ExecutorServer(
        socket_path, Handlers(_unavailable, _unavailable, _unavailable),
        agent_uid=agent_uid, ready_path=ready_path,
        lock_path=Path(args.lock_file) if args.lock_file else None,
    )
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(OSError, ValueError):
            signal.signal(signum, lambda *_: stop.set())
    try:
        server.bind()
    except (OSError, RuntimeError) as exc:
        print(f"executor: {exc}", file=sys.stderr)
        return 2
    print(ready_path, flush=True)
    try:
        with suppress(KeyboardInterrupt):
            server.serve_forever(stop)
        return 0
    finally:
        server.close()
