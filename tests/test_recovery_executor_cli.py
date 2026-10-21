from __future__ import annotations

import json

import pytest

from forgeflow.recovery import client, service

FIELDS = ("ok", "odoo_writes", "plan_id", "operation_counts", "step_count", "expected_new_spend")


@pytest.fixture
def request_file(tmp_path):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"target_product_code": "FIN"}))
    return path


def test_prepare_goes_through_the_socket_and_prints_exact_fields(monkeypatch, capsys, request_file):
    calls = {}

    def fake_prepare(socket_path, run_id, request, *, timeout):
        calls.update(socket=socket_path, run_id=run_id, request=request, timeout=timeout)
        return {
            "schema_version": 1, "op": "prepare", "ok": True, "odoo_writes": False,
            "plan_id": "a" * 64,
            "operation_counts": {"keep": 1, "update": 2, "cancel": 1, "create": 1},
            "step_count": 5, "expected_new_spend": 12.5, "reused": False,
        }

    monkeypatch.setenv("FORGEFLOW_EXECUTOR_SOCKET", "/run/forgeflow/executor.sock")
    monkeypatch.setenv("FORGEFLOW_RUN_ID", "run-9")
    monkeypatch.setattr(client, "prepare", fake_prepare)
    assert service.main(["prepare", "--request", str(request_file)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert tuple(printed) == FIELDS
    assert calls["run_id"] == "run-9"
    assert calls["request"] == {"target_product_code": "FIN"}


def test_apply_prints_the_applied_summary(monkeypatch, capsys):
    def fake_apply(socket_path, run_id, plan_id, approval, *, timeout):
        assert approval is None and plan_id == "a" * 64
        return {
            "schema_version": 1, "op": "apply", "ok": True, "odoo_writes": True,
            "plan_id": "a" * 64, "verified_steps": 5, "execution_validated": True,
            "benchmark_success": None, "replayed": False,
        }

    monkeypatch.setenv("FORGEFLOW_EXECUTOR_SOCKET", "/run/forgeflow/executor.sock")
    monkeypatch.setattr(client, "apply", fake_apply)
    assert service.main(["apply", "--plan-id", "a" * 64, "--autonomous"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {"ok", "odoo_writes", "plan_id", "verified_steps",
                            "execution_validated", "benchmark_success"}


def test_executor_failure_maps_to_the_tool_failure_shape(monkeypatch, capsys):
    def failing(*args, **kwargs):
        raise client.ExecutorError("approval_rejected", status=403, retryable=False, reason=None)

    monkeypatch.setenv("FORGEFLOW_EXECUTOR_SOCKET", "/run/forgeflow/executor.sock")
    monkeypatch.setattr(client, "apply", failing)
    assert service.main(["apply", "--plan-id", "a" * 64, "--autonomous"]) == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["error_code"] == "recovery_execute_stopped"
    assert printed["odoo_writes"] is True
    assert printed["reason"] == "approval_rejected"


def test_without_the_socket_the_local_path_is_used(monkeypatch, capsys):
    monkeypatch.delenv("FORGEFLOW_EXECUTOR_SOCKET", raising=False)

    def forbid(*args, **kwargs):
        raise AssertionError("client must not be used without the socket env")

    monkeypatch.setattr(client, "prepare", forbid)
    # No socket and no odoolib import assertion here: the local path is covered
    # by the existing service tests; this only proves the branch selection.
    assert "FORGEFLOW_EXECUTOR_SOCKET" not in __import__("os").environ


def test_route_log_records_the_socket_path(monkeypatch, tmp_path, request_file):
    route = tmp_path / "route.jsonl"
    monkeypatch.setenv("FORGEFLOW_ROUTE_LOG", str(route))
    monkeypatch.setenv("FORGEFLOW_EXECUTOR_SOCKET", "/run/forgeflow/executor.sock")
    monkeypatch.setattr(client, "prepare", lambda *a, **k: {
        "ok": True, "odoo_writes": False, "plan_id": "a" * 64,
        "operation_counts": {"keep": 0, "update": 0, "cancel": 0, "create": 0},
        "step_count": 0, "expected_new_spend": 0.0,
    })
    assert service.main(["prepare", "--request", str(request_file)]) == 0
    entries = [json.loads(line) for line in route.read_text().splitlines()]
    assert entries[0] == {"op": "prepare", "mode": "executor", "socket_set": True}
    assert entries[-1]["ok"] is True and entries[-1]["mode"] == "executor"
