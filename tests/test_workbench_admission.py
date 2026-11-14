from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from forgeflow.local_workbench.admission import (
    AdmissionError,
    AdmissionGate,
    _lock,
    paused,
    quiesce,
    resume,
)


@pytest.fixture
def config(tmp_path):
    return {"installation_id": "synthetic-installation",
            "env": {"FORGEFLOW_APPROVALS_ROOT": str(tmp_path)}}


def test_quiesce_waits_for_inflight_admission_then_blocks_future_ticks(config, monkeypatch):
    from forgeflow.superagent import heartbeat

    started, finish, stopped, paused_beat = [threading.Event() for _ in range(4)]
    calls = []

    def tick():
        calls.append("tick")
        started.set()
        assert finish.wait(3)

    monkeypatch.setattr(heartbeat, "beat", lambda *_a, **_k: paused_beat.set())
    service = SimpleNamespace(tick=tick, tick_seconds=0.05, store=None,
                              ctx=SimpleNamespace(tenant_id="synthetic"))
    gate = AdmissionGate(config, "source", service)
    thread = threading.Thread(target=gate.run, args=(stopped,))
    result = []
    quiescing = threading.Thread(target=lambda: result.append(quiesce(config, timeout=2)))
    try:
        thread.start()
        assert started.wait(2)
        quiescing.start()
        time.sleep(0.1)
        assert not result, "quiesce returned while admission was still in flight"
        finish.set()
        quiescing.join(2)
        assert result[0]["in_flight_admission_complete"] is True
        count = len(calls)
        assert paused_beat.wait(2)
        time.sleep(0.15)
        assert len(calls) == count
        assert paused(config)
        resume(config)
        deadline = time.monotonic() + 2
        while len(calls) == count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) > count
    finally:
        finish.set()
        stopped.set()
        thread.join(3)
        if quiescing.ident is not None:
            quiescing.join(3)
    assert not thread.is_alive() and not quiescing.is_alive()


def test_failed_quiesce_never_claims_pause(config):
    with _lock(config):
        with pytest.raises(AdmissionError, match="local_admission_busy"):
            quiesce(config, timeout=0.01)
    assert paused(config) is False


def test_pause_survives_new_service_instance_and_requires_explicit_resume(config):
    quiesce(config)
    assert paused(dict(config)) is True
    assert quiesce(dict(config))["admission_paused"] is True
    assert resume(dict(config))["admission_paused"] is False
    assert paused(config) is False


def test_foreign_pause_marker_cannot_be_cleared(config, tmp_path):
    marker = tmp_path / "local-admission-paused.json"
    marker.write_text(json.dumps({"schema_version": 1, "installation_id": "another"}))
    with pytest.raises(AdmissionError, match="local_admission_marker_invalid"):
        resume(config)
    assert marker.exists()
