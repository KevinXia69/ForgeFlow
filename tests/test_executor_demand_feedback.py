"""G4 demand feedback reaches the autonomous client through the real socket."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from forgeflow.executor.daemon import ExecutorServer, Handlers
from forgeflow.executor.handlers import RecoveryHandlers
from forgeflow.planning.live import LiveInputError
from forgeflow.recovery import client, service


@pytest.mark.parametrize("missing", [[4, 5], list(range(1, 31)), [9007199254740900 + i for i in range(10)]])
def test_missing_active_demand_is_bounded_and_survives_wire(tmp_path, monkeypatch, missing):
    # Relative socket names stay below the macOS AF_UNIX path limit.
    monkeypatch.chdir(tmp_path)
    calls = []

    def reject_plan(*args, **kwargs):
        calls.append("prepare")
        raise LiveInputError(
            "private synthetic error details must not appear on the wire",
            code="unselected_active_demand",
            details={"missing_sales_order_ids": missing, "secret": "synthetic-canary"},
        )

    monkeypatch.setattr(service, "prepare", reject_plan)
    recovery = RecoveryHandlers(
        connection_factory=lambda: object(), store=tmp_path / "recovery",
        bundle=tmp_path / "bundle", require_grant=False,
    )
    server = ExecutorServer(
        Path("feedback.sock"), Handlers(recovery.prepare, recovery.apply, recovery.status),
        agent_uid=os.getuid(), peer_check=lambda *_: None,
    )
    server.bind()
    stop = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        with pytest.raises(client.ExecutorError) as raised:
            client.prepare(str(server.socket_path), "synthetic-g4", {}, timeout=3)
        error = raised.value
        assert error.code == "plan_rejected"
        assert error.status == 422
        expected = []
        for item in missing[:20]:
            if len("unselected_active_demand:" + "-".join(map(str, [*expected, item]))) > 120:
                break
            expected.append(item)
        assert error.reason == "unselected_active_demand:" + "-".join(map(str, expected))
        assert client.prepare_feedback(error)["missing_sales_order_ids"] == expected
        assert "synthetic-canary" not in str(error)
        assert "private synthetic" not in str(error)
        assert calls == ["prepare"]
        assert not (tmp_path / "recovery").exists()
    finally:
        stop.set()
        thread.join(timeout=3)
        server.close()
    assert not thread.is_alive()
