from __future__ import annotations

import xmlrpc.client
from types import SimpleNamespace

import pytest

from forgeflow.integrations.odoo20.adapter import (
    Odoo20Adapter,
    Odoo20Config,
    Odoo20Unavailable,
)
from forgeflow.integrations.odoo20.contracts import REQUIRED_MODELS


def adapter_with_metadata(metadata=None, *, version="20.0"):
    calls = []

    def execute(database, uid, password, model, method, args, kwargs):
        assert (database, uid, password) == ("synthetic", 7, "synthetic-password")
        # Model metadata remains available to the ordinary ERP operator. The
        # administrative registry must never be used or granted to this user.
        assert model in REQUIRED_MODELS
        assert method == "fields_get"
        assert args == [] and kwargs == {"attributes": ["type"]}
        calls.append(model)
        if metadata is not None:
            return metadata(model)
        return {name: {"type": "synthetic"} for name in REQUIRED_MODELS[model]}

    adapter = Odoo20Adapter(Odoo20Config(
        "http://odoo.invalid", "synthetic", "operator", "synthetic-password"))
    adapter._common = lambda: SimpleNamespace(
        version=lambda: {"server_version": version}, authenticate=lambda *_: 7)
    adapter._models = lambda: SimpleNamespace(execute_kw=execute)
    return adapter, calls


def test_contract_uses_only_allowlisted_business_model_metadata():
    adapter, calls = adapter_with_metadata()
    result = adapter.check_contract()
    assert result.ok
    assert calls == list(REQUIRED_MODELS)


def test_contract_still_rejects_missing_required_fields_and_models():
    def metadata(model):
        if model == "forgeflow.execution_receipt":
            return {}
        return {name: {} for name in REQUIRED_MODELS[model]
                if (model, name) != ("purchase.order.line", "product_qty")}

    adapter, _ = adapter_with_metadata(metadata)
    result = adapter.check_contract()
    assert not result.ok
    assert result.missing_models == ("forgeflow.execution_receipt",)
    assert result.missing_fields == ("purchase.order.line.product_qty",)


def test_contract_rejects_unsupported_version_before_model_calls():
    adapter, calls = adapter_with_metadata(version="19.0")
    result = adapter.check_contract()
    assert not result.ok and result.missing_models == ("version",)
    assert calls == []


def test_contract_rpc_failure_is_unavailable_without_echoing_private_fault():
    def denied(_model):
        raise xmlrpc.client.Fault(1, "private-erp-values")

    adapter, _ = adapter_with_metadata(denied)
    with pytest.raises(Odoo20Unavailable, match="^odoo_contract_unavailable$"):
        adapter.check_contract()


@pytest.mark.parametrize("value", [None, [], {"name": "private-malformed-metadata"}])
def test_contract_rejects_malformed_model_metadata(value):
    adapter, _ = adapter_with_metadata(lambda _model: value)
    with pytest.raises(Odoo20Unavailable, match="^odoo_contract_unavailable$"):
        adapter.check_contract()
