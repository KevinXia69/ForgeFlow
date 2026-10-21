"""Authorized diagnostics only use bound synthetic recovery records."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from recovery_synthetic import compilation_inputs

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings
from forgeflow.recovery import compiler
from forgeflow.recovery.storage import digest


def fixture(tmp_path, *, compiler_hash=None, scenario="mixed"):
    changes, snapshot, workorders = compilation_inputs(scenario)
    compiled = compiler.compile_steps(changes, snapshot, workorders)
    records = {
        "change-set": changes, "snapshot": snapshot,
        "baseline": {"payload": {"workorders": workorders}},
        "binding": {
            "compiler_source_sha256": compiler_hash or hashlib.sha256(Path(compiler.__file__).read_bytes()).hexdigest(),
            "steps_sha256": compiler.digest([step.record() for step in compiled["steps"]]),
        },
    }
    manifest = {"artifacts": {name: digest(value) for name, value in records.items()}}
    plan_id = digest(manifest)
    directory = tmp_path / "runs" / "synthetic" / "plans" / plan_id
    directory.mkdir(parents=True)
    for name, value in {**records, "manifest": manifest, "prepared": {}}.items():
        (directory / f"{name}.json").write_text(json.dumps(value))
    (directory / "not-an-input.json").write_text('{"secret":"synthetic-secret-canary"}')
    return plan_id, directory


def client_for(tmp_path, monkeypatch, *, public=True):
    monkeypatch.delenv("FORGEFLOW_DSH_ENABLED", raising=False)
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_SESSION_SECRET": "synthetic-diagnostic-session-secret-01",
        "FORGEFLOW_INVITE_CODES": "synthetic-diagnostic-invite",
        "FORGEFLOW_PUBLIC_RUNS": "synthetic" if public else "",
    })
    return TestClient(create_app(settings), base_url="https://testserver")


def sign_in(client):
    assert client.post("/api/auth/session", json={"invite_code": "synthetic-diagnostic-invite"}).status_code == 201


@pytest.mark.parametrize("scenario", ["mixed", "new_purchase"])
def test_traceability_is_authenticated_bounded_and_does_not_read_unrelated_files(tmp_path, monkeypatch, scenario):
    plan_id, directory = fixture(tmp_path, scenario=scenario)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    path = f"/api/runs/synthetic/plans/{plan_id}/traceability"
    with client_for(tmp_path, monkeypatch) as client:
        assert client.get(path).status_code == 401
        sign_in(client)
        response = client.get(path)
        assert response.status_code == 200
        value = response.json()
        assert value["available"] is True
        assert value["live_erp_checked"] is False
        active = [row for row in value["documents"] if row["action"] != "cancel"]
        assert active
        assert all(row["expected_origins"] for row in active)
        assert all(consumer["consumes_purchase_product"] for row in active for consumer in row["consumers"])
        assert "synthetic-secret-canary" not in response.text
        assert all(row["recorded_observed_origin"] is None for row in value["documents"])
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    with client_for(tmp_path, monkeypatch, public=False) as client:
        sign_in(client)
        assert client.get(path).status_code == 404


def test_traceability_refuses_a_different_compiler_or_tampered_inputs(tmp_path, monkeypatch):
    plan_id, directory = fixture(tmp_path, compiler_hash="0" * 64)
    path = f"/api/runs/synthetic/plans/{plan_id}/traceability"
    with client_for(tmp_path, monkeypatch) as client:
        sign_in(client)
        assert client.get(path).json()["reason"] == "original_compiler_unavailable"
        (directory / "snapshot.json").write_text('{"unexpected":"synthetic-secret-canary"}')
        response = client.get(path)
        assert response.json()["reason"] == "recorded_origin_binding_mismatch"
        assert "synthetic-secret-canary" not in response.text
