"""The public API can project its own plan without access to the root store."""

import base64
import hashlib
from io import BytesIO

import pytest

pytest.importorskip("fcntl")

from forgeflow.executor.handlers import (  # noqa: E402
    ARTIFACT_CHUNK_BYTES,
    MAX_ARTIFACT_BYTES,
    RecoveryHandlers,
)
from forgeflow.executor.protocol import ProtocolError, validate_envelope, write_frame  # noqa: E402
from forgeflow.recovery.product_versions import ProductVersions, VersionError  # noqa: E402

PLAN = "a" * 64
OWNER = "b" * 64


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    store = tmp_path / "store"
    directory = store / PLAN
    directory.mkdir(parents=True, mode=0o700)
    handler = RecoveryHandlers(connection_factory=lambda: None, store=store,
                               bundle=tmp_path / "bundle", product_verifier=object())
    calls = []

    def status(_self, run_id, owner):
        calls.append((run_id, owner))
        if run_id != "local-case-1" or owner != OWNER:
            raise VersionError("product_owner_mismatch")
        return {"plans": {PLAN: {"state": "selected"}}}

    monkeypatch.setattr(ProductVersions, "status", status)
    return handler, directory, calls


def fetch(handler, name="snapshot.json", offset=0, **changes):
    payload = {"owner_sha256": OWNER, "plan_id": PLAN, "name": name, "offset": offset}
    payload.update(changes)
    return handler.product("local-case-1", "artifact", payload)


def test_artifact_chunks_fit_wire_and_preserve_complete_digest(artifacts):
    handler, directory, calls = artifacts
    raw = b'{"content":"' + b"x" * (ARTIFACT_CHUNK_BYTES * 2) + b'"}\n'
    (directory / "snapshot.json").write_bytes(raw)
    actual = bytearray()
    while len(actual) < len(raw):
        response = fetch(handler, offset=len(actual))
        chunk = response["artifact"]
        write_frame(BytesIO(), {"schema_version": 1, "ok": True, "op": "product", **response})
        assert chunk["offset"] == len(actual)
        assert chunk["total_bytes"] == len(raw)
        assert chunk["sha256"] == hashlib.sha256(raw).hexdigest()
        actual.extend(base64.b64decode(chunk["data_base64"], validate=True))
        assert chunk["eof"] is (len(actual) == len(raw))
    assert actual == raw
    assert calls == [("local-case-1", OWNER)] * 3


@pytest.mark.parametrize("changes", [
    {"name": "../api_key"}, {"name": "approval.json"}, {"name": "/etc/passwd"},
    {"name": "scope.lock"}, {"name": "journal.jsonl"}, {"offset": -1},
    {"offset": True}, {"offset": MAX_ARTIFACT_BYTES + 1}, {"plan_id": "../key"},
])
def test_artifact_refuses_unlisted_paths_and_bad_offsets(artifacts, changes):
    handler, _, _ = artifacts
    with pytest.raises(ProtocolError) as error:
        fetch(handler, **changes)
    assert error.value.code == "invalid_field"


def test_artifact_requires_owner_and_plan_binding_before_read(artifacts):
    handler, directory, calls = artifacts
    (directory / "snapshot.json").write_text("{}")
    with pytest.raises(ProtocolError):
        fetch(handler, owner_sha256="c" * 64)
    with pytest.raises(ProtocolError) as error:
        fetch(handler, plan_id="c" * 64)
    assert error.value.code == "plan_not_found"
    assert calls[0] == ("local-case-1", "c" * 64)


def test_artifact_reports_optional_missing_file(artifacts):
    handler, _, _ = artifacts
    with pytest.raises(ProtocolError) as error:
        fetch(handler, name="product-costs.json")
    assert (error.value.code, error.value.status) == ("artifact_missing", 404)


def test_artifact_refuses_file_symlink(artifacts, tmp_path):
    handler, directory, _ = artifacts
    secret = tmp_path / "secret"
    secret.write_text("private-canary")
    (directory / "snapshot.json").symlink_to(secret)
    with pytest.raises(ProtocolError) as error:
        fetch(handler)
    assert error.value.code == "artifact_unavailable"


def test_artifact_refuses_directory_symlink(artifacts, tmp_path):
    handler, directory, _ = artifacts
    directory.rmdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "snapshot.json").write_text("private-canary")
    directory.symlink_to(secret, target_is_directory=True)
    with pytest.raises(ProtocolError) as error:
        fetch(handler)
    assert error.value.code == "artifact_unavailable"


def test_artifact_unavailable_without_product_verifier(artifacts):
    handler, _, _ = artifacts
    handler.product_verifier = None
    with pytest.raises(ProtocolError) as error:
        fetch(handler)
    assert error.value.code == "product_operations_disabled"


def test_artifact_envelope_uses_existing_peer_gated_product_operation():
    operation, fields = validate_envelope({"schema_version": 1, "op": "product",
        "run_id": "local-case-1", "action": "artifact", "payload": {}})
    assert operation == "product"
    assert fields["action"] == "artifact"
