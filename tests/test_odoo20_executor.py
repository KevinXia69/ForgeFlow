from __future__ import annotations

import pytest

from forgeflow.integrations.odoo20.adapter import Odoo20Error
from forgeflow.integrations.odoo20.executor import Odoo20BusinessExecutor


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def apply_approved_plan(self, *args):
        self.calls.append(args)
        return {"operation_id": 7, "state": "succeeded", "readback": {"state": "cancel"},
                "receipt_id": 9}

    def reconcile_plan(self, *args):
        return {"operation_id": 7, "state": "succeeded", "readback": {"state": "cancel"},
                "receipt_id": 9}


def test_executor_only_allows_approved_operation_types() -> None:
    fake = FakeAdapter()
    executor = Odoo20BusinessExecutor(fake)
    result = executor.apply(1, "key", "cancel_purchase_order", "purchase.order", 2)
    assert result.operation_id == 7 and result.state == "succeeded"
    with pytest.raises(Odoo20Error, match="operation_not_allowed"):
        executor.apply(1, "key2", "write_any_model", "res.partner", 2)
    assert len(fake.calls) == 1
