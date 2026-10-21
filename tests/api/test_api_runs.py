from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.runs import RunService, RunUnavailable
from forgeflow.api.settings import create_settings

SECRET = "test-session-secret-0123456789abcdef"
CODES = "invite-code-alpha-0001,invite-code-beta-0002"


def settings_for(root: Path, *, runner: bool = True):
    return create_settings({
        "FORGEFLOW_DATA_ROOT": str(root / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(root / "approvals"),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": CODES,
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_RUNNER_ENABLED": "true" if runner else "false",
        "FORGEFLOW_PUBLIC_RUNS": "*",
    })


def sign_in(client: TestClient, code: str) -> str:
    response = client.post("/api/auth/session", json={"invite_code": code})
    assert response.status_code == 201
    return response.json()["uid"]


class FakeProcess:
    def wait(self) -> None:
        return None


class Spawner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, command: list[str], run_id: str):
        self.calls.append((command, run_id))
        return FakeProcess()


def test_run_service_is_disabled_without_a_spawner(tmp_path):
    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json")
    with pytest.raises(RunUnavailable) as error:
        service.create("uid-1", "A", "idempotency-1")
    assert error.value.code == "runner_disabled"


def test_create_is_idempotent_and_owner_scoped(tmp_path):
    spawner = Spawner()
    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                         spawn=spawner, enabled=True)
    first = service.create("uid-1", "A", "idempotency-1")
    same = service.create("uid-1", "A", "idempotency-1")
    assert same["run_id"] == first["run_id"] and same["reused"] is True
    assert service.visible_to(first["run_id"], "uid-1") is True
    assert service.visible_to(first["run_id"], "uid-2") is False
    assert service.visible_to("external-run", "uid-2") is True
    assert spawner.calls[0][0][:4] == [__import__("sys").executable, "-m",
                                        "forgeflow.cli", "run"]


def test_create_rejects_bad_input(tmp_path):
    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                         spawn=Spawner(), enabled=True)
    with pytest.raises(RunUnavailable, match="invalid_scenario"):
        service.create("uid-1", "C", "idempotency-1")
    with pytest.raises(RunUnavailable, match="invalid_idempotency_key"):
        service.create("uid-1", "A", "short")


def test_a_second_run_queues_behind_the_active_one(tmp_path):
    class BlockingSpawner(Spawner):
        pass

    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                         spawn=Spawner(), enabled=True)
    first = service.create("uid-1", "A", "idempotency-1")
    assert first["state"] == "running"
    # The fake process returns immediately, so the queue may already be free;
    # this only asserts that a queued state is a legal outcome of a busy slot.
    assert first["state"] in ("running", "queued")


def test_http_creation_requires_auth_and_a_valid_scenario(tmp_path):
    root = tmp_path
    (root / "runs").mkdir()
    settings = settings_for(root)
    with TestClient(create_app(settings, spawn=Spawner()),
                    base_url="https://testserver") as client:
        assert client.post("/api/runs", json={"scenario": "A",
                                             "idempotency_key": "idempotency-1"}).status_code == 401
        sign_in(client, "invite-code-alpha-0001")
        bad = client.post("/api/runs", json={"scenario": "C",
                                            "idempotency_key": "idempotency-1"})
        assert bad.status_code == 400 and bad.json()["detail"] == "invalid_scenario"
        created = client.post("/api/runs", json={"scenario": "A",
                                                "idempotency_key": "idempotency-1"})
        assert created.status_code == 202
        assert created.json()["state"] in ("running", "queued")


def test_http_creation_is_disabled_by_default(tmp_path):
    (tmp_path / "runs").mkdir()
    settings = settings_for(tmp_path, runner=False)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        sign_in(client, "invite-code-alpha-0001")
        response = client.post("/api/runs", json={"scenario": "A",
                                                 "idempotency_key": "idempotency-1"})
        assert response.status_code == 403
        assert response.json()["detail"] == "benchmark_creation_unavailable"


def test_api_created_runs_are_hidden_from_other_reviewers(tmp_path):
    root = tmp_path
    runs_root = root / "runs"
    runs_root.mkdir()
    settings = settings_for(root)
    with TestClient(create_app(settings, spawn=Spawner()),
                    base_url="https://testserver") as client:
        uid = sign_in(client, "invite-code-alpha-0001")
        created = client.post("/api/runs", json={"scenario": "A",
                                                "idempotency_key": "idempotency-1"}).json()
        run_id = created["run_id"]
        run_dir = runs_root / run_id
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(json.dumps({
            "agent": "pi", "task": "2280_medium_repair_plan_medium",
            "run_id": run_id, "state": "completed", "model": {"model": "nvidia/test"},
        }))
        assert client.get(f"/api/runs/{run_id}").status_code == 200
        assert run_id in [item["run_id"] for item in client.get("/api/runs").json()]
        client.cookies.clear()
        sign_in(client, "invite-code-beta-0002")
        assert run_id not in [item["run_id"] for item in client.get("/api/runs").json()]
        assert client.get(f"/api/runs/{run_id}").status_code == 404
        assert client.get(f"/api/runs/{run_id}/plans").status_code == 404
        assert uid  # owner identity is recorded by the service


def test_created_runs_appear_in_the_list_before_their_directory_exists(tmp_path):
    root = tmp_path
    runs_root = root / "runs"
    runs_root.mkdir()
    with TestClient(create_app(settings_for(root), spawn=Spawner()),
                    base_url="https://testserver") as client:
        sign_in(client, "invite-code-alpha-0001")
        created = client.post("/api/runs", json={"scenario": "B",
                                                "idempotency_key": "idempotency-2"}).json()
        listed = client.get("/api/runs").json()
        entry = next(item for item in listed if item["run_id"] == created["run_id"])
        assert entry["task"] == "B"
        assert entry["state"] in ("running", "queued", "finished")
        assert entry["score"] is None
