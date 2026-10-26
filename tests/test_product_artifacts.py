from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.product.artifacts import CHUNK_BYTES, OPTIONAL, REQUIRED, _fetch, prepared_directory
from forgeflow.product.executor import ProductExecutionError

CASE = {"case_id": "case-one", "_owner_sha256": "a" * 64}
PLAN = "b" * 64


def remote(data, mutate=None):
    calls = []

    def product(case, action, payload):
        assert case == CASE and action == "artifact"
        assert payload["owner_sha256"] == CASE["_owner_sha256"]
        assert payload["plan_id"] == PLAN
        calls.append(payload)
        offset = payload["offset"]
        chunk = data[offset:offset + CHUNK_BYTES]
        row = {"name": payload["name"], "offset": offset, "total_bytes": len(data),
               "sha256": hashlib.sha256(data).hexdigest(),
               "data_base64": base64.b64encode(chunk).decode(),
               "eof": offset + len(chunk) == len(data)}
        if mutate:
            mutate(row, len(calls))
        return {"artifact": row}

    return SimpleNamespace(_product=product, artifact_rpc=True), calls


def test_chunked_artifact_is_bound_and_verified():
    data = b"x" * (CHUNK_BYTES * 2 + 3)
    executor, calls = remote(data)
    assert _fetch(executor, CASE, PLAN, "snapshot.json") == data
    assert [c["offset"] for c in calls] == [0, CHUNK_BYTES, CHUNK_BYTES * 2]


@pytest.mark.parametrize("change", [
    {"sha256": "0" * 64}, {"offset": 1}, {"total_bytes": 16 * 1024 * 1024 + 1},
    {"data_base64": "?invalid"}, {"eof": False}, {"name": "../api_key"},
    {"total_bytes": True}, {"offset": False}, {"unexpected": "value"},
])
def test_tampered_artifact_is_rejected(change):
    executor, _ = remote(b"{}", lambda row, _: row.update(change))
    with pytest.raises(ProductExecutionError, match="local_artifact_unavailable"):
        _fetch(executor, CASE, PLAN, "snapshot.json")


def test_changed_digest_between_chunks_is_rejected():
    executor, _ = remote(b"x" * (CHUNK_BYTES + 1),
                         lambda row, count: row.update(sha256="0" * 64) if count == 2 else None)
    with pytest.raises(ProductExecutionError):
        _fetch(executor, CASE, PLAN, "snapshot.json")


def test_projection_temporary_directory_is_removed_and_only_optional_absence_is_allowed():
    executor, _ = remote(b"{}")
    original = executor._product

    def product(case, action, payload):
        if payload["name"] in OPTIONAL:
            raise ProductExecutionError("artifact_missing")
        return original(case, action, payload)

    executor._product = product
    with prepared_directory(executor, CASE, PLAN) as directory:
        assert {p.name for p in directory.iterdir()} == set(REQUIRED)
        assert all(p.read_bytes() == b"{}" for p in directory.iterdir())
    assert not directory.exists()
    executor._product = lambda *_: (_ for _ in ()).throw(ProductExecutionError("artifact_missing"))
    with pytest.raises(ProductExecutionError, match="artifact_missing"):
        with prepared_directory(executor, CASE, PLAN):
            pytest.fail("Required evidence must exist")


def test_local_integration_keeps_its_existing_store():
    executor = SimpleNamespace(artifact_rpc=False, store=Path("original-store"))
    with prepared_directory(executor, CASE, PLAN) as directory:
        assert directory == Path("original-store") / PLAN
