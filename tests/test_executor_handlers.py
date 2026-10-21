from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from forgeflow.contract import build_contract
from forgeflow.executor import handlers
from forgeflow.executor.protocol import ProtocolError
from forgeflow.recovery import service
from forgeflow.recovery.change_set import CHANGE_SET_REASONS, ChangeSetError
from forgeflow.recovery.journal import ExecutionStopped

PLAN = "a" * 64


@pytest.mark.parametrize("code", [*sorted(CHANGE_SET_REASONS), None,
                                  "change_set_mapping_unproven:private-canary",
                                  "change_set_mapping_unproven\nprivate-canary", "private-canary"])
def test_change_set_error_only_exposes_explicit_fixed_reason(tmp_path, monkeypatch, code):
    handler = make(tmp_path)

    def rejected(*args, **kwargs):
        error = ChangeSetError("private-canary: arbitrary supplier/offer/id 123")
        # Exercise the handler's own boundary even if an exception is tampered.
        error.code = code
        raise error

    monkeypatch.setattr(service, "prepare", rejected)
    with pytest.raises(ProtocolError) as caught:
        handler.prepare("run-1", {})
    assert caught.value.code == "plan_rejected"
    assert caught.value.reason == (code if code in CHANGE_SET_REASONS else "change_set_rejected")
    assert "private-canary" not in json.dumps(caught.value.to_dict("prepare"))


def test_change_set_prose_is_never_classified_as_a_public_reason(tmp_path, monkeypatch):
    handler = make(tmp_path)

    def rejected(*args, **kwargs):
        raise ChangeSetError("change_set_mapping_unproven")

    monkeypatch.setattr(service, "prepare", rejected)
    with pytest.raises(ProtocolError) as caught:
        handler.prepare("run-1", {})
    assert caught.value.reason == "change_set_rejected"


def contract():
    return build_contract(
        contract_id="handler-test-v1",
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


def approval_payload(**overrides):
    values = {
        "nonce": "0" * 32,
        "run_id": "run-1",
        "plan_id": PLAN,
        "policy_sha256": "b" * 64,
        "snapshot_sha256": "c" * 64,
        "approver": "judge",
        "issued_at_utc": datetime.now(UTC).isoformat(),
        "expires_at_utc": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    values.update(overrides)
    return values


def make(tmp_path, **kwargs):
    store = tmp_path / "state" / "recovery"
    store.mkdir(parents=True)
    return handlers.RecoveryHandlers(
        connection_factory=lambda: "connection",
        store=store,
        bundle=tmp_path / "bundle",
        **kwargs,
    )


def test_prepare_passes_the_recorded_contract(tmp_path, monkeypatch):
    handler = make(tmp_path)
    recorded = contract()
    (tmp_path / "state" / "policy.json").write_text(json.dumps(recorded.payload()))
    seen = {}

    def fake_prepare(connection, request, *, store, bundle, contract=None):
        seen.update(connection=connection, request=request, contract=contract)
        return {"ok": True, "plan_id": PLAN, "odoo_writes": False}

    monkeypatch.setattr(service, "prepare", fake_prepare)
    result = handler.prepare("run-1", {"target_product_code": "FIN"})
    assert result["run_id"] == "run-1"
    assert result["plan_id"] == PLAN
    assert seen["connection"] == "connection"
    assert seen["contract"].digest() == recorded.digest()


def test_prepare_maps_a_contract_conflict(tmp_path, monkeypatch):
    handler = make(tmp_path)

    def fake_prepare(*args, **kwargs):
        raise service.RecoveryError("task_contract_mismatch")

    monkeypatch.setattr(service, "prepare", fake_prepare)
    with pytest.raises(ProtocolError) as error:
        handler.prepare("run-1", {})
    assert error.value.code == "plan_rejected"
    assert error.value.status == 422


def test_apply_builds_a_grant_from_the_approval(tmp_path, monkeypatch):
    handler = make(tmp_path)
    seen = {}

    def fake_apply(connection, plan_id, **kwargs):
        seen.update(plan_id=plan_id, kwargs=kwargs)
        return {"ok": True, "execution_validated": True}

    monkeypatch.setattr(service, "apply", fake_apply)
    result = handler.apply("run-1", PLAN, approval_payload())
    assert result["run_id"] == "run-1"
    grant = seen["kwargs"]["grant"]
    assert grant.run_id == "run-1"
    assert grant.approval.plan_id == PLAN
    assert seen["kwargs"]["require_grant"] is True


def test_apply_without_an_approval_passes_no_grant(tmp_path, monkeypatch):
    handler = make(tmp_path)
    seen = {}

    def fake_apply(connection, plan_id, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(service, "apply", fake_apply)
    handler.apply("run-1", PLAN, None)
    assert seen["grant"] is None


@pytest.mark.parametrize("approval", [
    {},
    {"nonce": "0" * 32},
    approval_payload(extra="x"),
    approval_payload(nonce="short"),
])
def test_malformed_approval_is_rejected_before_the_service(tmp_path, monkeypatch, approval):
    handler = make(tmp_path)
    called = []
    monkeypatch.setattr(service, "apply", lambda *a, **k: called.append(True))
    with pytest.raises(ProtocolError) as error:
        handler.apply("run-1", PLAN, approval)
    assert error.value.code == "approval_rejected"
    assert called == []


def test_expired_approval_maps_to_approval_rejected(tmp_path, monkeypatch):
    handler = make(tmp_path)

    def fake_apply(*args, **kwargs):
        raise ExecutionStopped("approval_expired")

    monkeypatch.setattr(service, "apply", fake_apply)
    with pytest.raises(ProtocolError) as error:
        handler.apply("run-1", PLAN, approval_payload())
    assert error.value.code == "approval_rejected"
    assert error.value.status == 403


def test_status_reports_the_recorded_state(tmp_path):
    handler = make(tmp_path)
    plan_dir = handler.store / PLAN
    plan_dir.mkdir()
    (plan_dir / "prepared.json").write_text(json.dumps({"plan_id": PLAN}))
    assert handler.status("run-1", PLAN)["state"] == "prepared"

    (handler.store / "active-plan.json").write_text(json.dumps({"plan_id": PLAN}))
    assert handler.status("run-1", PLAN)["state"] == "running"

    (plan_dir / "success.json").write_text(json.dumps({
        "execution_validated": True, "verified_steps": 3,
    }))
    success = handler.status("run-1", PLAN)
    assert success["state"] == "succeeded"
    assert success["verified_steps"] == 3


def test_status_of_an_unknown_plan_is_not_found(tmp_path):
    handler = make(tmp_path)
    with pytest.raises(ProtocolError) as error:
        handler.status("run-1", PLAN)
    assert error.value.code == "plan_not_found"


def test_a_symlinked_policy_file_is_refused(tmp_path):
    handler = make(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(contract().payload()))
    (tmp_path / "state" / "policy.json").symlink_to(target)
    with pytest.raises(ProtocolError) as error:
        handler.prepare("run-1", {})
    assert error.value.code == "internal_error"


def test_a_service_failure_is_recorded_for_diagnostics(tmp_path, monkeypatch):
    handler = make(tmp_path)

    def failing(*args, **kwargs):
        raise service.RecoveryError("odoo_unavailable")

    monkeypatch.setattr(service, "prepare", failing)
    with pytest.raises(ProtocolError):
        handler.prepare("run-1", {})
    line = json.loads((tmp_path / "state" / "executor-errors.jsonl").read_text())
    assert line == {"op": "prepare", "reason": "odoo_unavailable"}


def test_an_unexpected_exception_is_recorded_with_a_traceback(tmp_path, monkeypatch):
    handler = make(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(service, "prepare", boom)
    with pytest.raises(RuntimeError):
        handler.prepare("run-1", {})
    entry = json.loads((tmp_path / "state" / "executor-errors.jsonl").read_text())
    assert entry["reason"].startswith("RuntimeError")
    assert "connection refused" in entry["traceback"]


def test_the_product_record_is_projected_onto_the_wire_fields():
    from forgeflow.executor.handlers import approval_wire_record

    record = {**approval_payload(), "schema_version": 1, "status": "valid"}
    projected = approval_wire_record(record)
    assert set(projected) == set(handlers.APPROVAL_FIELDS)
    assert "schema_version" not in projected and "status" not in projected
    with pytest.raises(ProtocolError) as error:
        approval_wire_record({"nonce": "0" * 32})
    assert error.value.code == "approval_rejected"


def test_prepare_returns_actionable_live_input_reason(tmp_path, monkeypatch):
    from forgeflow.planning.live import LiveInputError

    handler = make(tmp_path)

    def fake_prepare(*args, **kwargs):
        raise LiveInputError(
            "Unselected active demand shares closure products",
            code="unselected_active_demand",
            details={"missing_sales_order_ids": [4, 5]},
        )

    monkeypatch.setattr(service, "prepare", fake_prepare)
    with pytest.raises(ProtocolError) as raised:
        handler.prepare("run-1", {"target_product_code": "FIN"})
    assert raised.value.code == "plan_rejected"
    assert raised.value.reason == "unselected_active_demand:4-5"
