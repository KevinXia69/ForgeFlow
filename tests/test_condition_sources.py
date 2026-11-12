from forgeflow.superagent import ConditionSource


def test_condition_source_emits_once_per_transition() -> None:
    source = ConditionSource("late_supplier", lambda facts: facts["days_late"] > 2,
                             "Investigate late supplier")
    assert source.scan({"days_late": 1, "object_ref": "PO-1"}) == []
    events = source.scan({"days_late": 3, "object_ref": "PO-1"})
    assert len(events) == 1 and events[0].object_ref == "PO-1"
    assert source.scan({"days_late": 4, "object_ref": "PO-1"}) == []
