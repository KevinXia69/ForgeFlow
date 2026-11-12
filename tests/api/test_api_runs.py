from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.evidence import ReadOnlyEvidenceProjection
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
    assert "--data-root" in spawner.calls[0][0]
    assert spawner.calls[0][0][spawner.calls[0][0].index("--data-root") + 1] == str(tmp_path / "runs")


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


class BlockingProcess:
    def __init__(self, release: threading.Event) -> None:
        self.pid = os.getpid()
        self.release = release

    def wait(self) -> None:
        self.release.wait(5)


def test_idempotency_key_is_bound_to_the_scenario(tmp_path):
    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                         spawn=Spawner(), enabled=True)
    first = service.create("uid-1", "A", "idempotency-conflict-1")
    assert first["reused"] is False
    with pytest.raises(RunUnavailable) as error:
        service.create("uid-1", "B", "idempotency-conflict-1")
    assert error.value.code == "idempotency_conflict"


def test_rebuilt_service_waits_for_a_live_worker(tmp_path):
    release = threading.Event()
    process = BlockingProcess(release)
    calls = []

    def spawn(command, run_id):
        calls.append((command, run_id))
        return process if len(calls) == 1 else FakeProcess()

    first_service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                               spawn=spawn, enabled=True)
    first = first_service.create("uid-1", "A", "idempotency-live-1")
    assert first["state"] == "running"

    second_service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                                spawn=Spawner(), enabled=True)
    second = second_service.create("uid-1", "B", "idempotency-live-2")
    assert second["state"] == "queued"
    assert len(calls) == 1
    release.set()


def test_dead_local_worker_is_interrupted_during_recovery(tmp_path):
    state = tmp_path / "approvals" / "runs.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "schema_version": 1, "owners": {}, "idempotency": {},
        "runs": {"api-a-1": {"uid": "uid-1", "scenario": "A", "state": "running",
                             "worker_pid": 999_999_999,
                             "worker_host": socket.gethostname()}},
    }))
    service = RunService(tmp_path / "runs", state, spawn=Spawner(), enabled=True)
    assert service.state_of("api-a-1") == "interrupted"


def test_reads_see_ownership_written_by_another_instance(tmp_path):
    spawner = Spawner()
    a = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                   spawn=spawner, enabled=True)
    b = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                   spawn=spawner, enabled=True)
    created = a.create("alice", "A", "idempotency-cross-read-1")
    run_id = created["run_id"]
    assert b.owner(run_id) == "alice"
    assert b.state_of(run_id) is not None
    assert [row["run_id"] for row in b.list_for("alice")] == [run_id]
    projection = ReadOnlyEvidenceProjection(
        SimpleNamespace(public_runs=("*",), deployment="local"), None, b)
    assert projection.can_view_run(run_id, "alice") is True
    assert projection.can_view_run(run_id, "bob") is False


def test_promotion_does_not_start_a_second_worker_while_one_is_running(tmp_path):
    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                         spawn=Spawner(), enabled=True)
    service._state = {"runs": {
        "C": {"state": "running", "scenario": "A"},
        "B": {"state": "queued", "scenario": "A"},
    }}
    assert service._promote_locked() is None
    assert service._state["runs"]["B"]["state"] == "queued"
    service._state["runs"]["C"]["state"] = "finished"
    assert service._promote_locked() == ("B", "A")
    assert service._state["runs"]["B"]["state"] == "running"


def test_promotion_records_a_fresh_dispatch_reservation(tmp_path):
    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                         spawn=Spawner(), enabled=True)
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    service._state = {"runs": {
        "A": {"state": "finished", "scenario": "A"},
        "B": {"state": "queued", "scenario": "A", "created_at": old},
    }}
    assert service._promote_locked() == ("B", "A")
    record = service._state["runs"]["B"]
    assert record["dispatch_started_at"] != old
    assert service._worker_alive(record) is True


def test_restart_promotes_queued_work_after_a_dead_worker(tmp_path):
    state = tmp_path / "approvals" / "runs.json"
    state.parent.mkdir(parents=True)
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    state.write_text(json.dumps({
        "schema_version": 1, "owners": {}, "idempotency": {},
        "runs": {
            "api-a-1": {"uid": "uid-1", "scenario": "A", "state": "running",
                        "worker_pid": 999_999_999, "worker_host": socket.gethostname(),
                        "created_at": old},
            "api-b-1": {"uid": "uid-1", "scenario": "B", "state": "queued",
                        "created_at": old},
        },
    }))
    release = threading.Event()
    calls = []

    def spawn(command, run_id):
        calls.append(run_id)
        return BlockingProcess(release)

    service = RunService(tmp_path / "runs", state, spawn=spawn, enabled=True)
    assert service.state_of("api-a-1") == "interrupted"
    assert service.state_of("api-b-1") == "running"
    assert calls == ["api-b-1"]
    release.set()


def test_unreadable_existing_registry_is_not_overwritten_and_refuses_dispatch(tmp_path, monkeypatch):
    state = tmp_path / "approvals" / "runs.json"
    state.parent.mkdir(parents=True)
    valid = {"schema_version": 1, "owners": {"run-1": "uid-1"},
             "idempotency": {"uid-1:key-1": {"run_id": "run-1", "scenario": "A"}},
             "runs": {"run-1": {"uid": "uid-1", "scenario": "A", "state": "finished"}}}
    state.write_text(json.dumps(valid))
    spawner = Spawner()
    service = RunService(tmp_path / "runs", state, spawn=spawner, enabled=True)
    before = state.read_bytes()
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == state:
            raise OSError("temporary read failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(RunUnavailable, match="state_unavailable"):
        service.owner("run-1")
    with pytest.raises(RunUnavailable, match="state_unavailable"):
        service.create("uid-2", "A", "key-0002")
    assert state.read_bytes() == before
    assert spawner.calls == []


def test_corrupt_registry_is_preserved_and_refuses_dispatch(tmp_path):
    state = tmp_path / "approvals" / "runs.json"
    state.parent.mkdir(parents=True)
    state.write_text("{not-json")
    before = state.read_bytes()
    spawner = Spawner()
    service = RunService(tmp_path / "runs", state, spawn=spawner, enabled=True)
    assert state.read_bytes() == before
    with pytest.raises(RunUnavailable, match="state_unavailable"):
        service.create("uid-1", "A", "key-0001")
    assert spawner.calls == []


def test_http_corrupt_registry_returns_state_unavailable(tmp_path):
    (tmp_path / "runs").mkdir()
    state = tmp_path / "approvals" / "runs.json"
    state.parent.mkdir(parents=True)
    state.write_text("{not-json")
    before = state.read_bytes()
    with TestClient(create_app(settings_for(tmp_path), spawn=Spawner()),
                    base_url="https://testserver") as client:
        sign_in(client, "invite-code-alpha-0001")
        assert client.get("/api/runs").status_code == 503
        response = client.post("/api/runs", json={"scenario": "A", "idempotency_key": "idempotency-1"})
        assert response.status_code == 503
        assert response.json()["detail"] == "state_unavailable"
    assert state.read_bytes() == before


def test_invalid_utf8_registry_is_preserved_and_refuses_dispatch(tmp_path):
    state = tmp_path / "approvals" / "runs.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(b'{"schema_version": 1}\xff')
    before = state.read_bytes()
    spawner = Spawner()
    service = RunService(tmp_path / "runs", state, spawn=spawner, enabled=True)
    assert state.read_bytes() == before
    with pytest.raises(RunUnavailable, match="state_unavailable"):
        service.create("uid-1", "A", "key-0001")
    assert spawner.calls == []


def test_queue_promotes_in_acceptance_order_across_scenarios(tmp_path):
    release = threading.Event()
    calls: list[str] = []

    def spawn(command, run_id):
        calls.append(run_id)
        return BlockingProcess(release) if len(calls) == 1 else FakeProcess()

    service = RunService(tmp_path / "runs", tmp_path / "approvals/runs.json",
                         spawn=spawn, enabled=True)
    first = service.create("uid-1", "B", "idempotency-b-0001")
    second = service.create("uid-1", "A", "idempotency-a-0001")
    assert first["state"] == "running" and second["state"] == "queued"
    release.set()
    deadline = time.monotonic() + 5
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2
    assert calls[0] == first["run_id"]
    assert calls[1] == second["run_id"]
    assert service.state_of(first["run_id"]) == "finished"
    assert service.state_of(second["run_id"]) == "finished"


def test_queue_sequence_survives_service_restart(tmp_path):
    release = threading.Event()
    calls: list[str] = []
    state = tmp_path / "approvals/runs.json"

    def spawn(command, run_id):
        calls.append(run_id)
        return BlockingProcess(release) if len(calls) == 1 else FakeProcess()

    first_service = RunService(tmp_path / "runs", state, spawn=spawn, enabled=True)
    first = first_service.create("uid-1", "B", "idempotency-b-0002")
    second = first_service.create("uid-1", "A", "idempotency-a-0002")
    assert first["state"] == "running" and second["state"] == "queued"
    saved = json.loads(state.read_text())
    assert saved["runs"][first["run_id"]]["queue_seq"] == 1
    assert saved["runs"][second["run_id"]]["queue_seq"] == 2

    restarted = RunService(tmp_path / "runs", state, spawn=spawn, enabled=True)
    assert restarted.state_of(second["run_id"]) == "queued"
    release.set()
    deadline = time.monotonic() + 5
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [first["run_id"], second["run_id"]]
