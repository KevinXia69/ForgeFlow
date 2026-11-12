"""Independent currency identities and a no-ERP managed executor lifecycle."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_product_handoff import binding
from test_recovery_scope_closure import real_bundle as real_bundle

from forgeflow import runtime_lifecycle as lifecycle
from forgeflow.investigation import fixtures
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import policy_from_payload
from forgeflow.permissions import (
    CONTINUATION_READ_ONLY_MODELS,
    CONTINUATION_WRITE_GATED_MODELS,
    READ_ONLY_MODELS,
    provision_script,
)
from forgeflow.product.environment import (
    EnvironmentConfig,
    LocalReadConnection,
    ManagedOdooEnvironment,
    ReadOnlyConnection,
    currency_evidence,
)
from forgeflow.product.executor import ProductExecutionError
from forgeflow.product.handoff import HandoffRegistry


@pytest.fixture
def currency_records(monkeypatch):
    case = fixtures.supplier_cancellation_control()
    monkeypatch.setitem(fixtures.SCHEMAS, "res.company", frozenset({"id", "currency_id"}))
    records = case.records
    records["res.company"] = [{"id": 1, "currency_id": 1}]
    return records, task_template(case.contract)


def test_currency_proof_covers_company_orders_and_component_offers(currency_records):
    records, template = currency_records
    connection = fixtures.FixtureConnection(records)
    before = connection.fingerprint()
    result = currency_evidence(connection, template)
    expected = {"res.company:1", "sale.order:1", "purchase.order:1"}
    expected |= {f"product.supplierinfo:{row['id']}" for row in records["product.supplierinfo"]}
    assert set(result["record_currencies"]) == expected
    assert set(result["record_currencies"].values()) == {"USD"}
    assert result["company_id"] == 1 and type(result["company_id"]) is int
    assert connection.fingerprint() == before and connection.write_attempts == []


@pytest.mark.parametrize("mutation", ["missing_company_currency", "missing_order_currency", "missing_offer_currency",
    "mixed_po_currency", "mixed_offer_currency", "mixed_company", "unrecognized_currency", "unavailable_schema"])
def test_currency_unknown_and_mixed_values_rejected(currency_records, monkeypatch, mutation):
    records, template = currency_records
    if mutation == "missing_company_currency":
        records["res.company"][0]["currency_id"] = False
    elif mutation == "missing_order_currency":
        records["sale.order"][0]["currency_id"] = False
    elif mutation == "missing_offer_currency":
        records["product.supplierinfo"][0]["currency_id"] = False
    elif mutation in {"mixed_po_currency", "mixed_offer_currency"}:
        records["res.currency"].append({"id": 2, "name": "EUR"})
        model = "purchase.order" if mutation == "mixed_po_currency" else "product.supplierinfo"
        records[model][0]["currency_id"] = 2
    elif mutation == "mixed_company":
        records["purchase.order"][0]["company_id"] = 2
    elif mutation == "unrecognized_currency":
        records["res.currency"][0]["name"] = "unknown"
    connection = fixtures.FixtureConnection(records)
    if mutation == "unavailable_schema":
        monkeypatch.setitem(fixtures.SCHEMAS, "res.company", frozenset({"id"}))
    with pytest.raises(ProductExecutionError, match="currency|company"):
        currency_evidence(connection, template)
    assert connection.write_attempts == []


def test_reader_rejects_write_before_underlying_connection(currency_records):
    records, _ = currency_records
    connection = fixtures.FixtureConnection(records)
    reader = ReadOnlyConnection(connection)
    for method in ("write", "create", "unlink", "execute"):
        with pytest.raises(ProductExecutionError, match="read_only_method_required"):
            getattr(reader.get_model("purchase.order"), method)
    assert connection.write_attempts == []


def test_continuation_permissions_are_explicit_and_write_gated():
    assert "res.company" not in READ_ONLY_MODELS
    assert CONTINUATION_READ_ONLY_MODELS == (*READ_ONLY_MODELS, "res.company")
    assert CONTINUATION_WRITE_GATED_MODELS == CONTINUATION_READ_ONLY_MODELS
    script = provision_script(models=CONTINUATION_READ_ONLY_MODELS,
                              write_gated=CONTINUATION_WRITE_GATED_MODELS)
    assert script.count("'res.company'") == 2


def test_local_reader_never_uses_proxy_or_write_method(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    config.read_key.write_text("invented-read-key\n")
    config.read_key.chmod(0o400)
    calls = []
    monkeypatch.setenv("ALL_PROXY", "socks5://invalid.local:9999")

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, content=b"[]", json=lambda: [])

    monkeypatch.setattr("forgeflow.product.environment.httpx.post", post)
    reader = LocalReadConnection(config)
    assert reader.get_model("sale.order").search(domain=[], limit=1) == []
    assert calls[0][1]["trust_env"] is False
    assert calls[0][1]["headers"]["X-Odoo-Database"] == "bench"
    with pytest.raises(ProductExecutionError, match="read_only_method_required"):
        reader.get_model("sale.order").write(ids=[1], vals={})
    assert len(calls) == 1


def test_environment_source_accepts_partial_policy_without_inventing_budget(currency_records, tmp_path, monkeypatch):
    records, template = currency_records
    fields = template["fields"]
    partial = policy_from_payload({"kind": "forgeflow.investigation_policy", "schema_version": 1,
        "policy_id": "unknown-budget", "task_id": fields["task_id"],
        **{key: fields[key] for key in ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")},
        "orders": {"1": {"authorized": True, "budget_cap": None}}, "sources": {"policy": "owner"}})
    environment = ManagedOdooEnvironment(configuration(tmp_path))
    monkeypatch.setattr(environment, "connection_factory", lambda: fixtures.FixtureConnection(records))
    source = environment.source_factory(partial)
    assert source.query("policy", {"order_id": "1"})["records"][0]["fields"] == {"authorized": True, "budget_cap": None}


def configuration(tmp_path, bundle=None):
    return EnvironmentConfig(lifecycle_file=tmp_path / "lifecycle.json", container="invented-sandbox",
        bundle=bundle or tmp_path / "bundle", read_key=tmp_path / "read-key",
        write_key=tmp_path / "write-key", repository=Path(__file__).resolve().parents[1],
        docker_host="unix:///tmp/invented-no-docker.sock", port=18199)


def test_generation_binds_current_managed_container_and_instance(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    record = {"schema_version": 1, "uid": os.getuid(), "ready": True, "instance": "a" * 32,
              "config": {"container": config.container, "odoo_port": config.port},
              "container_id": "b" * 64, "image_id": "sha256:" + "c" * 64, "project": "d" * 64}
    lifecycle.write_record(config.lifecycle_file, record)
    observed = [record["container_id"], record["image_id"], True,
                {"io.forgeflow.instance": record["instance"], "io.forgeflow.project": record["project"]}]
    calls = []

    def inspect(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="|".join(json.dumps(value) for value in observed))

    monkeypatch.setattr("forgeflow.product.environment.subprocess.run", inspect)
    environment = ManagedOdooEnvironment(config)
    first = environment.generation()
    assert environment.generation() == first and len(first) == 64
    assert all(command[:2] == ["docker", "inspect"] for command in calls)
    observed[0] = "f" * 64
    with pytest.raises(ProductExecutionError, match="generation_changed"):
        environment.generation()
    observed[0] = record["container_id"]
    observed[2] = False
    with pytest.raises(ProductExecutionError, match="generation_changed"):
        environment.generation()


def test_bound_currency_is_rechecked_after_confirmation(currency_records, tmp_path, monkeypatch):
    records, template = currency_records
    value, _ = binding()
    value["currency"] = currency_evidence(fixtures.FixtureConnection(records), template)
    environment = ManagedOdooEnvironment(configuration(tmp_path))
    monkeypatch.setattr(environment, "generation", lambda: value["generation"])
    monkeypatch.setattr(environment, "connection_factory", lambda: fixtures.FixtureConnection(records))
    environment.guard_binding(value)
    # Even a consistently changed single currency cannot reuse an old USD cap.
    records["res.currency"][0]["name"] = "EUR"
    with pytest.raises(ProductExecutionError, match="currency_changed"):
        environment.guard_binding(value)


def test_real_host_executor_reuses_owned_process_without_touching_erp(tmp_path, monkeypatch, real_bundle):
    config = configuration(tmp_path, real_bundle)
    config.write_key.write_text("invented-never-used-key\n")
    config.write_key.chmod(0o400)
    environment = ManagedOdooEnvironment(config)
    monkeypatch.setattr(environment, "generation", lambda: "generation-1")
    value, _ = binding()
    with tempfile.TemporaryDirectory(prefix="ff-env-", dir="/tmp") as short:
        registry = HandoffRegistry(tmp_path / "registry", socket_root=short,
            generation=environment.generation, executor_factory=environment.executor_factory)
        registry.register(value)
        paths = registry.paths(value)
        record_path = paths.directory / "executor-process.json"
        other_value, _ = binding("2" * 32)
        registry.register(other_value)
        other_paths = registry.paths(other_value)
        other_record = other_paths.directory / "executor-process.json"
        try:
            first = environment.executor_factory(value, paths)
            record, state = lifecycle.managed_process(record_path)
            assert state == "running" and paths.socket.is_socket()
            second = environment.executor_factory(value, paths)
            assert lifecycle.read_record(record_path)["identity"] == record["identity"]
            assert first.socket_path == second.socket_path and first.request == value["request"]
            assert list(paths.store.iterdir()) == [] and list(paths.approvals.iterdir()) == []
            launch = (paths.directory / "executor-launch.json").read_text()
            assert "invented-never-used-key" not in launch
            other = environment.executor_factory(other_value, other_paths)
            assert other.socket_path != first.socket_path
            assert lifecycle.managed_process(other_record)[1] == "running"
            assert lifecycle.managed_process(record_path)[1] == "running"
        finally:
            lifecycle.stop_process(other_record)
            lifecycle.stop_process(record_path)
        assert lifecycle.managed_process(record_path)[1] == "not_recorded"
        assert lifecycle.process_identity(record["identity"]["pid"]) is None
        assert not paths.socket.exists()
