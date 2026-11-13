"""A configured company scopes plan RPCs; no company means Odoo's default."""
from __future__ import annotations

from forgeflow.integrations.odoo20.adapter import Odoo20Adapter, Odoo20Config


class _Models:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def execute_kw(self, database, uid, password, model, method, args, kwargs=None):
        self.calls.append((method, tuple(args), dict(kwargs or {})))
        return {"plan_id": 1, "status": "pass", "checks": []}


def _adapter(company_id: int) -> tuple[Odoo20Adapter, _Models]:
    adapter = Odoo20Adapter(Odoo20Config("http://odoo", "db", "u", "p",
                                        company_id=company_id))
    models = _Models()
    adapter._models = lambda: models  # type: ignore[method-assign]
    adapter._authenticate = lambda: 2  # type: ignore[method-assign]
    return adapter, models


def test_a_configured_company_is_sent_with_every_plan_call() -> None:
    adapter, models = _adapter(8)
    adapter.plan_checks(9)
    method, _, kwargs = models.calls[0]
    assert method == "forgeflow_plan_checks"
    assert kwargs["context"] == {"allowed_company_ids": [8], "default_company_id": 8}


def test_no_company_keeps_the_default_context() -> None:
    adapter, models = _adapter(0)
    adapter.plan_checks(9)
    _, _, kwargs = models.calls[0]
    assert "context" not in kwargs


def test_import_search_create_and_write_use_selected_company_context() -> None:
    adapter, _ = _adapter(0)
    adapter._uid = 7
    scoped = adapter.for_company(8)
    assert adapter.config.company_id == 0
    assert scoped.config.company_id == 8
    assert scoped._uid == 7
    calls = []

    class Models:
        def execute_kw(self, database, uid, password, model, method, args, kwargs):
            calls.append((model, method, kwargs))
            return 101 if method == "create" else [] if method == "search_read" else True

    scoped._models = lambda: Models()
    scoped.search_read("product.product", [("company_id", "=", 8)], ["standard_price"])
    scoped.create("product.product", {"name": "Material", "standard_price": 12})
    scoped.write("product.product", [101], {"standard_price": 15})
    assert [method for _, method, _ in calls] == ["search_read", "create", "write"]
    for _, _, kwargs in calls:
        assert kwargs["context"] == {"allowed_company_ids": [8],
                                     "default_company_id": 8, "force_company": 8}


def test_native_fault_keeps_safe_denial_reason_without_traceback():
    import xmlrpc.client

    import pytest

    from forgeflow.integrations.odoo20.adapter import Odoo20Error

    adapter, models = _adapter(8)
    class Rejected:
        def execute_kw(self, *args, **kwargs):
            raise xmlrpc.client.Fault(1, "private traceback details\nodoo.exceptions.UserError: "
                                     "approval_permission_unavailable\n")
    adapter._models = lambda: Rejected()
    with pytest.raises(Odoo20Error, match="^approval_permission_unavailable$"):
        adapter.plan_checks(9)
