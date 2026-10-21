"""Peer credential checks for the root executor's AF_UNIX command socket.

The executor must reject a request before parsing it when the connecting
process is not the expected agent uid. On macOS development hosts
``SO_PEERCRED`` is absent; callers get a typed, fixed-code failure instead of
a silent allow.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

_SOL_SOCKET = socket.SOL_SOCKET
_SO_PEERCRED: int | None = getattr(socket, "SO_PEERCRED", None)
SO_PEERCRED_SUPPORTED: bool = _SO_PEERCRED is not None
_PEERCRED_SIZE = 12


@dataclass(frozen=True)
class PeerCredentials:
    """The kernel-reported identity of the process on the other socket end."""

    pid: int
    uid: int
    gid: int


class PeerError(RuntimeError):
    """A failed peer check with a stable wire code.

    Observed credentials stay in attributes for local diagnostics; they are
    never formatted into the exception text, which the executor maps to a
    fixed wire code.
    """

    code: str
    pid: int | None
    uid: int | None
    gid: int | None

    def __init__(
        self,
        code: str,
        *,
        pid: int | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.pid = pid
        self.uid = uid
        self.gid = gid


def peer_credentials(sock: socket.socket) -> PeerCredentials:
    """Read one complete ``struct ucred`` from ``sock`` or raise ``PeerError``."""
    if not SO_PEERCRED_SUPPORTED or _SO_PEERCRED is None:
        raise PeerError("peer_credentials_unsupported")
    try:
        raw = sock.getsockopt(_SOL_SOCKET, _SO_PEERCRED, _PEERCRED_SIZE)
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as exc:
        raise PeerError("peer_credentials_unreadable") from exc
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def require_peer(
    sock: socket.socket,
    expected_uid: int,
    *,
    expected_gid: int | None = None,
) -> PeerCredentials:
    """Return the peer only when its uid and optional gid match."""
    credentials = peer_credentials(sock)
    if credentials.uid != expected_uid or (
        expected_gid is not None and credentials.gid != expected_gid
    ):
        raise PeerError(
            "peer_rejected",
            pid=credentials.pid,
            uid=credentials.uid,
            gid=credentials.gid,
        )
    return credentials
