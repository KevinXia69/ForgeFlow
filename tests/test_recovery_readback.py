"""Synthetic dynamic-binding and drift tests; no ERP/model/network calls."""

from __future__ import annotations

import copy
import json

import pytest

from forgeflow.recovery.journal import ExecutionStopped, Step, execute, plan_digest
from forgeflow.recovery.readback import ReadbackAdapter, ReadbackError


class Model:
    def __init__(self, database, model):
        self.database, self.model = database, model

    def read(self, *, ids, fields):
        return [
            {k: copy.deepcopy(v) for k, v in self.database[self.model][i].items() if k in fields}
            for i in ids
        ]

    def write(self, *, ids, vals):
        for i in ids:
            self.database[self.model][i].update(vals)
        return True


class Connection:
    def __init__(self):
        self.db = {
            "mrp.production": {
                1: {
                    "id": 1,
                    "name": "FF/MO/synthetic",
                    "state": "confirmed",
                    "workorder_ids": [9],
                    "company_id": 1,
                    "product_id": 2,
                    "product_uom_id": 1,
                    "bom_id": 1,
                    "product_qty": 2,
                    "qty_produced": 0,
                    "origin": "SO001",
                    "date_start": "2026-10-22 00:00:00",
                    "date_deadline": "2026-10-28 00:00:00",
                    "date_finished": False,
                    "move_raw_ids": [1],
                    "move_finished_ids": [2],
                }
            },
            "mrp.workorder": {
                9: {
                    "id": 9,
                    "name": "Synthetic WO",
                    "state": "pending",
                    "production_id": 1,
                    "product_id": 2,
                    "operation_id": 3,
                    "workcenter_id": 1,
                    "date_start": False,
                    "date_finished": False,
                    "duration_expected": 1.0,
                }
            },
        }

    def get_model(self, name):
        return Model(self.db, name)


def steps():
    identity = {
        "token": "workorder:new:mrp.production:1",
        "model": "mrp.workorder",
        "id": None,
        "name": None,
        "production": {
            "token": "new:mrp.production:1",
            "model": "mrp.production",
            "id": 1,
            "name": "FF/MO/synthetic",
        },
    }
    bind = Step(
        "bind:wo:1",
        "bind_workorder",
        identity["token"],
        {
            "identity": identity,
            "arguments": {"product_id": 2, "operation_id": 3},
            "before": None,
            "after": {"binding_key": "bind:wo:1"},
        },
    )
    schedule = Step(
        "schedule:wo:1",
        "schedule_workorder",
        identity["token"],
        {
            "identity": identity,
            "arguments": {
                "workcenter_id": 2,
                "start": "2026-10-22 00:00:00",
                "finish": "2026-10-28 00:00:00",
                "duration_minutes": 110.0,
            },
            "before": {"binding_key": bind.key},
            "after": {
                "workcenter_id": 2,
                "date_start": "2026-10-22 00:00:00",
                "date_finished": "2026-10-28 00:00:00",
                "duration_expected": 110.0,
            },
        },
        (bind.key,),
    )
    return bind, schedule


def adapter(tmp_path, connection, compiled=None):
    def guard(adapter, compiled, completed):
        # Synthetic fixture guard only; production needs ScopeGuard, including
        # all immutable facts and unaffected records, not just this toy graph.
        assert set(connection.db["mrp.workorder"]) == {9}
        assert connection.db["mrp.workorder"][9]["production_id"] == 1

    return ReadbackAdapter(
        connection, guard, tmp_path, plan_sha256=plan_digest(compiled or steps())
    )


def test_real_wo_id_and_before_are_durable_then_scheduled(tmp_path):
    connection = Connection()
    service = adapter(tmp_path, connection)
    bind, schedule = steps()
    result = execute((bind, schedule), service, tmp_path / "journal.jsonl")
    assert result["verified_steps"] == 2
    original = service._binding(bind.key, bind.payload["identity"])
    assert original["id"] == 9 and original["duration_expected"] == 1.0
    assert original["date_start"] is False
    assert connection.db["mrp.workorder"][9]["duration_expected"] == 110.0
    assert execute((bind, schedule), service, tmp_path / "journal.jsonl") == result


def test_even_matching_schedule_requires_durable_binding(tmp_path):
    connection = Connection()
    service = adapter(tmp_path, connection)
    _, schedule = steps()
    connection.db["mrp.workorder"][9].update(schedule.payload["after"])
    assert service.observe(schedule) == "conflict"


def test_wrong_product_or_operation_cannot_be_bound(tmp_path):
    connection = Connection()
    bind, _ = steps()
    service = adapter(tmp_path, connection, (bind,))
    connection.db["mrp.workorder"][9]["operation_id"] = 99
    with pytest.raises(ExecutionStopped, match="precondition_conflict"):
        execute((bind,), service, tmp_path / "journal.jsonl")
    assert not service._binding_path(bind.key).exists()


def test_drift_after_binding_is_not_adopted_as_new_before(tmp_path):
    connection = Connection()
    service = adapter(tmp_path, connection)
    bind, schedule = steps()
    service.apply(bind)
    original = service.binding_digest(bind)
    connection.db["mrp.workorder"][9]["duration_expected"] = 777.0
    assert service.observe(schedule) == "conflict"
    assert service.binding_digest(bind) == original
    with pytest.raises(FileExistsError):
        service.apply(bind)
    assert service.binding_digest(bind) == original


def test_duplicate_or_changed_wo_does_not_get_selected_by_position(tmp_path):
    connection = Connection()
    bind, _ = steps()
    service = adapter(tmp_path, connection, (bind,))
    connection.db["mrp.production"][1]["workorder_ids"].append(10)
    connection.db["mrp.workorder"][10] = {**connection.db["mrp.workorder"][9], "id": 10}
    with pytest.raises(ReadbackError, match="expected_one_unexecuted_workorder"):
        service.observe(bind)
    with pytest.raises(ExecutionStopped, match="preflight_failed"):
        execute((bind,), service, tmp_path / "journal.jsonl")


@pytest.mark.parametrize("field", ["date_start", "duration_expected", "operation_id", "id"])
def test_missing_requested_fields_cannot_be_bound_and_written(tmp_path, field):
    connection = Connection()
    service = adapter(tmp_path, connection)
    bind, schedule = steps()
    connection.db["mrp.workorder"][9].pop(field)
    with pytest.raises(ExecutionStopped, match="readback_failed"):
        execute((bind, schedule), service, tmp_path / "journal.jsonl")
    assert connection.db["mrp.workorder"][9]["workcenter_id"] == 1
    assert not service._binding_path(bind.key).exists()


@pytest.mark.parametrize("damage", ["delete", "replace"])
def test_completed_binding_receipt_cannot_be_lost_or_replaced_on_resume(tmp_path, damage):
    connection = Connection()
    service = adapter(tmp_path, connection)
    bind, schedule = steps()
    execute((bind, schedule), service, tmp_path / "journal.jsonl")
    original = copy.deepcopy(connection.db)
    artifact = service._binding_path(bind.key)
    if damage == "delete":
        artifact.unlink()
    else:
        data = json.loads(artifact.read_text())
        data["record"]["duration_expected"] += 1
        artifact.write_text(json.dumps(data))
    with pytest.raises(ExecutionStopped):
        execute((bind, schedule), service, tmp_path / "journal.jsonl")
    assert connection.db == original


def test_different_plan_cannot_adopt_binding_from_previous_plan(tmp_path):
    connection = Connection()
    original = steps()
    first = adapter(tmp_path, connection, original)
    first.apply(original[0])
    changed = copy.deepcopy(original)
    changed[1].payload["after"]["duration_expected"] = 220.0
    changed[1].payload["arguments"]["duration_minutes"] = 220.0
    second = adapter(tmp_path, connection, changed)
    assert first.binding_directory != second.binding_directory
    assert second._binding(changed[0].key, changed[0].payload["identity"]) is None
    assert second.observe(changed[1]) == "conflict"


def test_binding_content_cannot_claim_another_plan_digest(tmp_path):
    connection = Connection()
    service = adapter(tmp_path, connection)
    bind, _ = steps()
    service.apply(bind)
    artifact = service._binding_path(bind.key)
    data = json.loads(artifact.read_text())
    data["plan_sha256"] = "0" * 64
    artifact.write_text(json.dumps(data))
    with pytest.raises(ReadbackError, match="binding_identity_mismatch"):
        service.binding_digest(bind)
