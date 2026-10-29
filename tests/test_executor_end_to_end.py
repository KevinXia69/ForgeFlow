"""End-to-end wiring: client -> socket -> daemon -> handlers -> recovery service.

The service itself is replaced by fakes, so this test proves the transport and
the handler contract agree, without Odoo, a container, or a model call.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forgeflow.contract import build_contract
from forgeflow.executor import daemon, handlers
from forgeflow.recovery import client, service

PLAN = "a" * 64


def contract():
    return build_contract(
        contract_id="e2e-v1",
        task_id="synthetic",
        task_type="workcenter_outage",
        product_code="FIN",
        anchor_date="2026-01-01",
        margin=0.2,
        margin_basis="portfolio_new_spend_margin_at_selling_price",
        budgets={"1": 2000.0},
        sales_order_ids=(1,),
        disruption={"kind": "workcenter_outage", "workcenter_code": "WC-A",
                    "workcenter_name": "Assembly A"},
        original_commitment_policy="keep",
        sources={"budgets": "synthetic"},
    )


def approval(plan_id=PLAN):
    now = datetime.now(UTC)
    return {
        "nonce": "0" * 32,
        "run_id": "run-1",
        "plan_id": plan_id,
        "policy_sha256": contract().digest(),
        "snapshot_sha256": "c" * 64,
        "approver": "judge",
        "issued_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=10)).isoformat(),
    }


@pytest.fixture
def executor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = tmp_path / "state" / "recovery"
    store.mkdir(parents=True)
    (tmp_path / "state" / "policy.json").write_text(json.dumps(contract().payload()))
    seen = {}

    def fake_prepare(connection, request, *, store, bundle, contract=None, retention_profile=None):
        seen["contract"] = contract
        assert retention_profile is None
        return {"ok": True, "plan_id": PLAN, "odoo_writes": False, "step_count": 3}

    def fake_apply(connection, plan_id, *, store, bundle, grant=None,
                   require_grant=False, approval_store=None):
        seen["grant"] = grant
        seen["require_grant"] = require_grant
        return {"ok": True, "execution_validated": True, "verified_steps": 3}

    monkeypatch.setattr(service, "prepare", fake_prepare)
    monkeypatch.setattr(service, "apply", fake_apply)
    monkeypatch.setattr(daemon, "require_peer", lambda sock, uid, **kwargs: None)
    runtime = handlers.RecoveryHandlers(
        connection_factory=lambda: object(), store=store, bundle=tmp_path / "bundle",
    )
    server = daemon.ExecutorServer(
        Path("s.sock"),
        daemon.Handlers(prepare=runtime.prepare, apply=runtime.apply, status=runtime.status),
        agent_uid=os.geteuid(),
    )
    server.bind()
    stop = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        yield Path("s.sock"), store, seen
    finally:
        stop.set()
        thread.join(5)
        server.close()


def test_client_prepare_apply_and_status_round_trip(executor):
    socket_path, store, seen = executor
    prepared = client.prepare(socket_path, "run-1", {"target_product_code": "FIN"})
    assert prepared["ok"] is True
    assert prepared["plan_id"] == PLAN
    assert seen["contract"] is not None

    plan_dir = store / PLAN
    plan_dir.mkdir()
    (plan_dir / "prepared.json").write_text(json.dumps({"plan_id": PLAN}))
    assert client.status(socket_path, "run-1", PLAN)["state"] == "prepared"

    applied = client.apply(socket_path, "run-1", PLAN, approval())
    assert applied["execution_validated"] is True
    assert seen["require_grant"] is True
    assert seen["grant"].run_id == "run-1"

    (plan_dir / "success.json").write_text(json.dumps({
        "execution_validated": True, "verified_steps": 3,
    }))
    assert client.status(socket_path, "run-1", PLAN)["state"] == "succeeded"


def test_service_errors_arrive_as_wire_codes(executor, monkeypatch):
    socket_path, _, _ = executor

    def failing(*args, **kwargs):
        raise service.RecoveryError("task_contract_mismatch")

    monkeypatch.setattr(service, "prepare", failing)
    with pytest.raises(client.ExecutorError) as error:
        client.prepare(socket_path, "run-1", {})
    assert error.value.code == "plan_rejected"
    assert error.value.status == 422


def test_an_unknown_plan_is_reported_as_not_found(executor):
    socket_path, _, _ = executor
    with pytest.raises(client.ExecutorError) as error:
        client.status(socket_path, "run-1", PLAN)
    assert error.value.code == "plan_not_found"


def test_an_unapproved_apply_is_refused(executor, monkeypatch):
    socket_path, _, _ = executor

    def refusing(*args, **kwargs):
        raise service.RecoveryError("approval_required")

    monkeypatch.setattr(service, "apply", refusing)
    with pytest.raises(client.ExecutorError) as error:
        client.apply(socket_path, "run-1", PLAN, None)
    assert error.value.code == "approval_rejected"
