"""Tests for executor peer credential verification."""

from __future__ import annotations

import errno
import os
import socket
import struct

import pytest

from forgeflow.executor import peer

PID = 4242
UID = 1000
GID = 2000


def _payload(pid: int = PID, uid: int = UID, gid: int = GID) -> bytes:
    return struct.pack("3i", pid, uid, gid)


class FakeSocket:
    """A socket stand-in; tests never bind or open a filesystem socket."""

    def __init__(self, payload: bytes | None = None, error: OSError | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[int, int, int]] = []

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.calls.append((level, option, size))
        if self.error is not None:
            raise self.error
        if self.payload is None:
            raise AssertionError("FakeSocket has no payload")
        return self.payload


@pytest.fixture
def linux_peercred(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the SO_PEERCRED path reachable on non-Linux development hosts."""
    monkeypatch.setattr(peer, "SO_PEERCRED_SUPPORTED", True)
    monkeypatch.setattr(peer, "_SO_PEERCRED", 17)


@pytest.mark.skipif(
    not peer.SO_PEERCRED_SUPPORTED,
    reason="SO_PEERCRED is only available on Linux",
)
def test_socketpair_reports_this_process() -> None:
    client, server = socket.socketpair()
    try:
        credentials = peer.require_peer(server, os.getuid(), expected_gid=os.getgid())
    finally:
        client.close()
        server.close()
    assert credentials == peer.PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())


def test_accepts_matching_uid(linux_peercred: None) -> None:
    fake = FakeSocket(_payload(pid=77))
    credentials = peer.require_peer(fake, UID)
    assert credentials == peer.PeerCredentials(pid=77, uid=UID, gid=GID)
    assert fake.calls == [(socket.SOL_SOCKET, 17, 12)]


def test_rejects_wrong_uid_with_observed_credentials(linux_peercred: None) -> None:
    fake = FakeSocket(_payload(pid=77))
    with pytest.raises(peer.PeerError) as caught:
        peer.require_peer(fake, UID + 1)
    error = caught.value
    assert error.code == "peer_rejected"
    assert (error.pid, error.uid, error.gid) == (77, UID, GID)
    assert str(error) == "peer_rejected"


def test_enforces_optional_gid(linux_peercred: None) -> None:
    fake = FakeSocket(_payload())
    assert peer.require_peer(fake, UID, expected_gid=GID).gid == GID
    with pytest.raises(peer.PeerError) as caught:
        peer.require_peer(fake, UID, expected_gid=GID + 1)
    assert caught.value.code == "peer_rejected"
    assert (caught.value.pid, caught.value.uid, caught.value.gid) == (PID, UID, GID)


def test_unreadable_credentials_are_reported(linux_peercred: None) -> None:
    fake = FakeSocket(error=OSError(errno.EPERM, "denied"))
    with pytest.raises(peer.PeerError) as caught:
        peer.peer_credentials(fake)
    assert caught.value.code == "peer_credentials_unreadable"
    assert caught.value.uid is None
    assert isinstance(caught.value.__cause__, OSError)


def test_short_credentials_are_never_returned(linux_peercred: None) -> None:
    fake = FakeSocket(struct.pack("2i", PID, UID))
    with pytest.raises(peer.PeerError) as caught:
        peer.peer_credentials(fake)
    assert caught.value.code == "peer_credentials_unreadable"
    assert caught.value.pid is None


def test_unsupported_platform_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(peer, "SO_PEERCRED_SUPPORTED", False)
    fake = FakeSocket(_payload())
    with pytest.raises(peer.PeerError) as caught:
        peer.peer_credentials(fake)
    assert caught.value.code == "peer_credentials_unsupported"
    assert (caught.value.pid, caught.value.uid, caught.value.gid) == (None, None, None)
    assert fake.calls == []
