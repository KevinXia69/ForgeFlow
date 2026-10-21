"""Failure injection for remote writes: timeout is not proof of non-commit."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from forgeflow.recovery import journal as journal_module
from forgeflow.recovery.journal import ExecutionStopped, Journal, Step, execute


def purchase(key="create:po:1", **kwargs):
    return Step(key, "create_purchase", "new:purchase.order:1",
                {"name": "FF/PO/synthetic-1", "quantity": 5}, **kwargs)


class FakeERP:
    def __init__(self, *, failure=None):
        self.records = {}
        self.writes = []
        self.failure = failure
        self.preflight_calls = 0

    def preflight(self, steps, completed):
        self.preflight_calls += 1
        if self.failure == "stale_scope":
            raise RuntimeError("private transport details must not escape")

    def observe(self, step):
        if self.failure == "readback" and self.writes:
            raise RuntimeError("private response")
        existing = self.records.get(step.key)
        if existing is None:
            return "before"
        return "after" if existing == step.payload else "conflict"

    def apply(self, step):
        self.writes.append(step.key)
        if self.failure == "before_commit":
            raise TimeoutError("secret service response")
        self.records[step.key] = dict(step.payload)
        if self.failure == "after_commit":
            raise TimeoutError("secret service response")
        if self.failure == "process_crash":
            raise KeyboardInterrupt


def test_remote_commit_followed_by_timeout_is_read_back_without_duplicate(tmp_path):
    erp = FakeERP(failure="after_commit")
    path = tmp_path / "journal.jsonl"
    assert execute((purchase(),), erp, path)["verified_steps"] == 1
    assert execute((purchase(),), erp, path)["verified_steps"] == 1
    assert erp.writes == ["create:po:1"]
    assert [json.loads(line)["event"] for line in path.read_text().splitlines()] == [
        "plan", "dispatch", "verified",
    ]


def test_process_crash_after_commit_resumes_by_identity_without_recreation(tmp_path):
    erp = FakeERP(failure="process_crash")
    path = tmp_path / "journal.jsonl"
    with pytest.raises(KeyboardInterrupt):
        execute((purchase(),), erp, path)
    erp.failure = None
    assert execute((purchase(),), erp, path)["verified_steps"] == 1
    assert erp.writes == ["create:po:1"]
    assert json.loads(path.read_text().splitlines()[-1])["event"] == "reconciled"


def test_no_observed_record_after_timeout_is_not_permission_to_retry(tmp_path):
    erp = FakeERP(failure="before_commit")
    path = tmp_path / "journal.jsonl"
    with pytest.raises(ExecutionStopped, match="^mutation_not_verified$"):
        execute((purchase(),), erp, path)
    erp.failure = None
    with pytest.raises(ExecutionStopped, match="^unresolved_previous_dispatch$"):
        execute((purchase(),), erp, path)
    assert erp.writes == ["create:po:1"]
    assert "secret" not in path.read_text()


def test_unavailable_readback_can_reconcile_later_without_second_write(tmp_path):
    erp = FakeERP(failure="readback")
    path = tmp_path / "journal.jsonl"
    with pytest.raises(ExecutionStopped, match="^mutation_outcome_unknown$"):
        execute((purchase(),), erp, path)
    erp.failure = None
    assert execute((purchase(),), erp, path)["verified_steps"] == 1
    assert erp.writes == ["create:po:1"]


def test_changed_completed_record_halts_before_later_mutation(tmp_path):
    erp = FakeERP()
    path = tmp_path / "journal.jsonl"
    execute((purchase(),), erp, path)
    erp.records["create:po:1"]["quantity"] = 9
    with pytest.raises(ExecutionStopped, match="^completed_step_drift$"):
        execute((purchase(),), erp, path)
    assert len(erp.writes) == 1


def test_preflight_blocks_entire_plan_before_first_write(tmp_path):
    erp = FakeERP(failure="stale_scope")
    with pytest.raises(ExecutionStopped, match="^preflight_failed$"):
        execute((purchase(),), erp, tmp_path / "journal.jsonl")
    assert erp.writes == []
    assert (tmp_path / "journal.jsonl").read_bytes() == b""


def test_dependency_failure_never_executes_downstream_operation(tmp_path):
    erp = FakeERP(failure="before_commit")
    steps = (purchase(), Step("confirm:po:1", "confirm_purchase",
                              "new:purchase.order:1", {"state": "purchase"},
                              ("create:po:1",)))
    with pytest.raises(ExecutionStopped, match="^mutation_not_verified$"):
        execute(steps, erp, tmp_path / "journal.jsonl")
    assert erp.writes == ["create:po:1"]


@pytest.mark.parametrize("kind", ["receive_stock", "button_mark_done", "adjust_inventory",
                                 "execute_rpc", "delete_purchase"])
def test_physical_and_arbitrary_operations_rejected_before_adapter(tmp_path, kind):
    erp = FakeERP()
    bad = Step("bad", kind, "new:purchase.order:1", {})
    with pytest.raises(ExecutionStopped, match="^unsupported_operation$"):
        execute((bad,), erp, tmp_path / "journal.jsonl")
    assert erp.preflight_calls == 0
    assert erp.writes == []
    assert not (tmp_path / "journal.jsonl").exists()


def test_existing_wrong_document_does_not_get_overwritten(tmp_path):
    erp = FakeERP()
    erp.records["create:po:1"] = {"quantity": 77}
    with pytest.raises(ExecutionStopped, match="^precondition_conflict$"):
        execute((purchase(),), erp, tmp_path / "journal.jsonl")
    assert erp.writes == []


def test_journal_cannot_be_reused_for_a_different_plan(tmp_path):
    erp = FakeERP()
    path = tmp_path / "journal.jsonl"
    execute((purchase(),), erp, path)
    with pytest.raises(ExecutionStopped, match="^journal_plan_mismatch$"):
        execute((purchase(key="another"),), erp, path)
    assert len(erp.writes) == 1


@pytest.mark.parametrize("corruption", ["truncate", "reorder", "modify"])
def test_interrupted_or_corrupt_journal_is_not_silently_reset(tmp_path, corruption):
    erp = FakeERP()
    path = tmp_path / "journal.jsonl"
    execute((purchase(),), erp, path)
    original = path.read_bytes()
    if corruption == "truncate":
        path.write_bytes(original[:-3])
    elif corruption == "reorder":
        lines = original.splitlines(keepends=True)
        path.write_bytes(b"".join([lines[0], lines[2], lines[1]]))
    else:
        path.write_bytes(original.replace(b'"dispatch"', b'"modified"'))
    with pytest.raises(ExecutionStopped):
        execute((purchase(),), erp, path)
    assert len(erp.writes) == 1


def test_exclusive_lock_prevents_two_dispatchers(tmp_path):
    path = tmp_path / "journal.jsonl"
    with Journal(path):
        with pytest.raises(ExecutionStopped, match="^execution_already_running$"):
            execute((purchase(),), FakeERP(), path)


def test_durable_journal_does_not_contain_full_business_payload(tmp_path):
    step = Step("create:po:1", "create_purchase", "new:purchase.order:1",
                {"name": "FF/PO/synthetic-1", "internal_note": "private business text"})
    path = tmp_path / "journal.jsonl"
    execute((step,), FakeERP(), path)
    assert "private business text" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


class BindingERP(FakeERP):
    def __init__(self):
        super().__init__()
        self.binding = None

    def apply(self, step):
        super().apply(step)
        if step.kind == "bind_workorder":
            self.binding = b"synthetic bound WO id and original schedule"

    def binding_digest(self, step):
        if self.binding is None:
            raise FileNotFoundError
        return hashlib.sha256(self.binding).hexdigest()


@pytest.mark.parametrize("change", ["missing", "different"])
def test_completed_binding_cannot_be_silently_recreated_from_new_state(tmp_path, change):
    erp = BindingERP()
    path = tmp_path / "journal.jsonl"
    bind = Step("bind:wo:1", "bind_workorder", "new:mrp.workorder:1", {"product_id": 2})
    execute((bind,), erp, path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["data"]["binding_sha256"] == hashlib.sha256(erp.binding).hexdigest()
    erp.binding = None if change == "missing" else b"changed observed state"
    with pytest.raises(ExecutionStopped, match="binding_receipt_(unavailable|changed)"):
        execute((bind,), erp, path)
    assert erp.writes == ["bind:wo:1"]


def test_binding_digest_rechecked_after_later_operations(tmp_path):
    class CorruptAfterBind(BindingERP):
        def apply(self, step):
            super().apply(step)
            if step.kind != "bind_workorder":
                self.binding = b"changed by later operation"
    erp = CorruptAfterBind()
    bind = Step("bind:wo:1", "bind_workorder", "new:mrp.workorder:1", {"product_id": 2})
    with pytest.raises(ExecutionStopped, match="^binding_receipt_changed$"):
        execute((bind, purchase()), erp, tmp_path / "journal.jsonl")


class PurchaseLifecycle:
    """One real document progresses through create and confirmation states."""

    def __init__(self, *, interrupt_confirm=False, corrupt_quantity=False):
        self.record = None
        self.writes = []
        self.interrupt_confirm = interrupt_confirm
        self.corrupt_quantity = corrupt_quantity

    def preflight(self, steps, completed):
        # The final accumulated projection contains the quantity from create,
        # even though confirmation's own narrow postcondition is just state.
        if len(completed) == len(steps) and self.record != {
            "quantity": 5, "state": "purchase",
        }:
            raise ValueError("cumulative projection drift")

    def observe(self, step):
        if step.kind == "create_purchase":
            if self.record is None:
                return "before"
            return "after" if self.record == {"quantity": 5, "state": "draft"} else "conflict"
        if self.record is not None and self.record["state"] == "purchase":
            return "after"
        return "before" if self.record == {"quantity": 5, "state": "draft"} else "conflict"

    def apply(self, step):
        self.writes.append(step.key)
        if step.kind == "create_purchase":
            self.record = {"quantity": 5, "state": "draft"}
        else:
            self.record["state"] = "purchase"
            if self.corrupt_quantity:
                self.record["quantity"] = 99
            if self.interrupt_confirm:
                raise KeyboardInterrupt


def lifecycle_steps():
    return (purchase(), Step("confirm:po:1", "confirm_purchase", "new:purchase.order:1",
                              {"state": "purchase"}, ("create:po:1",)))


@pytest.mark.parametrize("interrupt_confirm", [False, True])
def test_same_document_later_state_supersedes_earlier_after_on_replay(tmp_path, interrupt_confirm):
    erp = PurchaseLifecycle(interrupt_confirm=interrupt_confirm)
    steps, path = lifecycle_steps(), tmp_path / "journal.jsonl"
    if interrupt_confirm:
        with pytest.raises(KeyboardInterrupt):
            execute(steps, erp, path)
    else:
        assert execute(steps, erp, path)["verified_steps"] == 2
    erp.interrupt_confirm = False
    assert execute(steps, erp, path)["verified_steps"] == 2
    assert erp.writes == ["create:po:1", "confirm:po:1"]
    assert erp.record == {"quantity": 5, "state": "purchase"}


def test_final_accumulated_preflight_rejects_overwritten_earlier_field(tmp_path):
    erp = PurchaseLifecycle(corrupt_quantity=True)
    with pytest.raises(ExecutionStopped, match="^postflight_failed$"):
        execute(lifecycle_steps(), erp, tmp_path / "journal.jsonl")
    assert erp.writes == ["create:po:1", "confirm:po:1"]
    assert erp.record == {"quantity": 99, "state": "purchase"}


@pytest.mark.parametrize("event", ["dispatch", "already_applied"])
def test_valid_hash_cannot_hide_nonprefix_or_multiple_pending_history(tmp_path, event):
    erp = FakeERP(failure="before_commit")
    steps = (purchase(), purchase(key="create:po:2"))
    path = tmp_path / "journal.jsonl"
    with pytest.raises(ExecutionStopped, match="^mutation_not_verified$"):
        execute(steps, erp, path)
    second_hash = hashlib.sha256(json.dumps(
        steps[1].record(), sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    with Journal(path) as journal:
        journal.append(event, {"key": steps[1].key, "step_sha256": second_hash})
    before = erp.preflight_calls
    with pytest.raises(ExecutionStopped, match="^invalid_journal_order$"):
        execute(steps, erp, path)
    assert erp.preflight_calls == before
    assert erp.writes == ["create:po:1"]


def test_nonstring_history_key_is_a_fixed_error_before_adapter(tmp_path):
    erp = FakeERP(failure="before_commit")
    path = tmp_path / "journal.jsonl"
    with pytest.raises(ExecutionStopped):
        execute((purchase(),), erp, path)
    with Journal(path) as journal:
        journal.append("verified", {"key": [], "step_sha256": "invalid"})
    before = erp.preflight_calls
    with pytest.raises(ExecutionStopped, match="^invalid_journal_event$"):
        execute((purchase(),), erp, path)
    assert erp.preflight_calls == before


@pytest.mark.parametrize("phase", ["preflight", "observe", "apply"])
def test_adapter_cannot_change_hashed_payload_and_continue(tmp_path, phase):
    class MutatingAdapter(FakeERP):
        def preflight(self, steps, completed):
            super().preflight(steps, completed)
            if phase == "preflight":
                steps[0].payload["quantity"] = 999

        def observe(self, step):
            if phase == "observe":
                step.payload["quantity"] = 999
            return super().observe(step)

        def apply(self, step):
            if phase == "apply":
                step.payload["quantity"] = 999
            super().apply(step)

    erp, step = MutatingAdapter(), purchase()
    path = tmp_path / "journal.jsonl"
    with pytest.raises(ExecutionStopped, match="^adapter_mutated_plan$"):
        execute((step,), erp, path)
    assert step.payload["quantity"] == 5
    if phase == "apply":
        assert erp.writes == [step.key]
        # A side effect already happened; do not mark it verified or retry it.
        assert json.loads(path.read_text().splitlines()[-1])["event"] == "dispatch"
        safe_adapter = FakeERP()
        safe_adapter.records = erp.records
        with pytest.raises(ExecutionStopped, match="^precondition_conflict$"):
            execute((step,), safe_adapter, path)
        assert safe_adapter.writes == []
    else:
        assert erp.writes == []


@pytest.mark.parametrize(("step", "code"), [
    (Step("x", [], "doc", {}), "unsupported_operation"),
    (Step("x", "create_purchase", "doc", {}, ([],)), "invalid_dependency_order"),
    (Step("x", "create_purchase", "doc", {1: "coerced-key"}), "non_json_payload"),
])
def test_malformed_plan_types_never_reach_adapter(tmp_path, step, code):
    erp = FakeERP()
    with pytest.raises(ExecutionStopped, match=f"^{code}$"):
        execute((step,), erp, tmp_path / "journal.jsonl")
    assert erp.preflight_calls == 0
    assert erp.writes == []


def test_directory_sync_failure_prevents_remote_dispatch(tmp_path, monkeypatch):
    real_fsync = os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory could not be persisted")
        return real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fail_directory_sync)
    erp = FakeERP()
    with pytest.raises(OSError, match="directory could not be persisted"):
        execute((purchase(),), erp, tmp_path / "journal.jsonl")
    assert erp.writes == []


def test_zero_byte_journal_write_stops_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_module.os, "write", lambda *_: 0)
    erp = FakeERP()
    with pytest.raises(ExecutionStopped, match="^journal_write_failed$"):
        execute((purchase(),), erp, tmp_path / "journal.jsonl")
    assert erp.writes == []


def test_short_reads_do_not_hide_pending_dispatch_and_allow_resend(tmp_path, monkeypatch):
    erp = FakeERP(failure="before_commit")
    path = tmp_path / "journal.jsonl"
    with pytest.raises(ExecutionStopped, match="^mutation_not_verified$"):
        execute((purchase(),), erp, path)
    # The first read ends at a complete plan row. Ignoring the unread dispatch
    # row would incorrectly allow this previously ambiguous write to be sent.
    first_row_bytes = len(path.read_bytes().splitlines(keepends=True)[0])
    real_read = os.read
    monkeypatch.setattr(journal_module.os, "read",
                        lambda fd, size: real_read(fd, min(size, first_row_bytes)))
    erp.failure = None
    with pytest.raises(ExecutionStopped, match="^unresolved_previous_dispatch$"):
        execute((purchase(),), erp, path)
    assert erp.writes == ["create:po:1"]
