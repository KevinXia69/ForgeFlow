"""One synthetic control through the actual reader, solver, change set, and prepare."""

import copy
import tarfile
from pathlib import Path

import pytest

from forgeflow.investigation.fixtures import (
    FixtureAccessError,
    FixtureConnection,
    supplier_cancellation_control,
)
from forgeflow.investigation.sources import OdooSource
from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.planning.live import collect_snapshot
from forgeflow.recovery.change_set import validate_projection
from forgeflow.recovery.service import prepare
from forgeflow.recovery.storage import digest, read_json


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("investigation-fixture-bundle")
    root = Path(__file__).resolve().parents[1]
    archive_path = directory / "bundle.tar.gz"
    build_planner_bundle(root, archive_path, recovery=True)
    with tarfile.open(archive_path) as archive:
        archive.extractall(directory, filter="data")
    return directory / "planner"


def investigated_request(source):
    supplier = source.query("entities", {"entity": "supplier", "query": "Fixture old vendor"})["records"]
    assert len(supplier) == 1
    supplier = supplier[0]
    purchase = source.query("entities", {"entity": "purchase_order", "query": "PO-CANCEL"})["records"]
    assert len(purchase) == 1
    purchase = purchase[0]
    assert purchase["fields"]["supplier_id"] == supplier["id"]
    related = source.query("relations", {"entity": "purchase_order", "id": purchase["id"]})
    assert related["complete"] is True
    line = next(row for row in related["records"] if row["entity"] == "purchase_order_line")
    product = source.query("record", {"entity": "product", "id": line["fields"]["product_id"]})["records"][0]
    related = source.query("relations", {"entity": "product", "id": product["id"]})
    assert related["complete"] is True
    ids = sorted({int(row["fields"]["order_id"]) for row in related["records"]
                  if row["entity"] == "sales_order_line"
                  and row["fields"]["quantity"] > row["fields"]["delivered_quantity"]})
    budgets = {}
    for identity in ids:
        policy = source.query("policy", {"order_id": identity})["records"][0]["fields"]
        assert policy["authorized"] is True
        budgets[str(identity)] = policy["budget_cap"]
    policy = source.query("policy", {})["records"][0]["fields"]
    return {
        "target_product_code": product["fields"]["default_code"], "sales_order_ids": ids,
        "anchor_date": policy["anchor_date"], "margin": policy["margin"],
        "sales_order_budget_caps": budgets,
        "disruption": {"kind": policy["task_type"], "vendor_ref": supplier["fields"]["ref"]},
    }


def test_control_uses_real_prepare_and_solver_without_business_writes(real_bundle, tmp_path):
    case = supplier_cancellation_control()
    connection = FixtureConnection(case.records)
    source = OdooSource(connection, case.contract)
    before = connection.fingerprint()
    request = investigated_request(source)
    result = prepare(connection, request, store=tmp_path / "store", bundle=real_bundle,
                     contract=case.contract)
    assert result["ok"] is True
    assert result["odoo_writes"] is False
    assert connection.fingerprint() == before
    assert connection.write_attempts == []
    assert {call["method"] for call in connection.calls} == {"fields_get", "search", "read"}
    directory = tmp_path / "store" / result["plan_id"]
    manifest = read_json(directory / "manifest.json")
    assert digest(manifest) == result["plan_id"]
    assert "forgeflow/investigation/fixtures.py" not in manifest["sources"]["files_sha256"]
    assert manifest["artifacts"]["request"] == digest(request)
    assert read_json(directory / "request.json") == request
    snapshot = read_json(directory / "snapshot.json")
    blueprint = read_json(directory / "blueprint.json")
    plan = read_json(directory / "plan.json")
    changes = read_json(directory / "change-set.json")
    assert len(blueprint["manufactured_products"]) == 1
    assert blueprint["repair_context"]["broken_supplier_vendor_ref"] == "V-OLD"
    assert plan["solver"]["version"] == "9.15.6755"
    assert plan["trace"]["solve_calls"]
    assert all(call["status"] in {"OPTIMAL", "FEASIBLE"} for call in plan["trace"]["solve_calls"])
    assert not any(row["vendor_ref"] == "V-OLD" for row in plan["plan"]["purchase_orders"])
    assert result["operation_counts"]["cancel"] >= 1
    assert result["operation_counts"]["create"] >= 1
    validate_projection(changes, request, snapshot, blueprint, plan)
    assert (directory / "baseline.json").is_file()
    assert not (directory / "success.json").exists()


def test_investigation_and_snapshot_read_the_same_mutable_host_tables():
    case = supplier_cancellation_control()
    connection = FixtureConnection(case.records)
    source = OdooSource(connection, case.contract)
    request = investigated_request(source)
    connection.records["sale.order.line"][0]["product_uom_qty"] = 11
    observed = source.query("record", {"entity": "sales_order_line", "id": 1})["records"][0]
    snapshot = collect_snapshot(connection, request)
    assert observed["fields"]["quantity"] == 11
    assert snapshot["models"]["sale.order.line"][0]["product_uom_qty"] == 11
    assert case.records["sale.order.line"][0]["product_uom_qty"] == 10


def test_fixture_queries_apply_prefix_domains_pagination_and_literal_matching():
    case = supplier_cancellation_control()
    connection = FixtureConnection(case.records)
    model = connection.get_model("res.partner")
    domain = ["|", ("ref", "=", "V-OLD"), ("ref", "=", "V-NEW"), ("supplier_rank", ">", 0)]
    assert model.search(domain=domain, limit=1, offset=0) == [3]
    assert model.search(domain=domain, limit=1, offset=1) == [4]
    assert model.search(domain=domain, limit=1, offset=2) == []
    connection.records["product.product"][0]["default_code"] = "FIN_100%"
    assert connection.get_model("product.product").search(
        domain=[("default_code", "ilike", r"FIN\_100\%")],
    ) == [1]
    assert model.search(domain=["!", ("supplier_rank", "=", 0)]) == [2, 3, 4]


@pytest.mark.parametrize("method", ["write", "create", "unlink", "button_confirm", "execute"])
def test_no_business_write_or_arbitrary_model_method_exists(method):
    connection = FixtureConnection(supplier_cancellation_control().records)
    before = connection.fingerprint()
    with pytest.raises(FixtureAccessError, match="fixture_read_only"):
        getattr(connection.get_model("purchase.order"), method)()
    assert connection.fingerprint() == before
    assert connection.write_attempts == [{"model": "purchase.order", "method": method}]


def test_unknown_models_fields_and_unsupported_query_language_are_rejected():
    connection = FixtureConnection(supplier_cancellation_control().records)
    with pytest.raises(FixtureAccessError, match="unsupported_fixture_model"):
        connection.get_model("ir.config_parameter")
    model = connection.get_model("sale.order")
    for domain in [[("secret", "=", "value")], [("name", "child_of", 1)], ["|", ("id", "=", 1)]]:
        with pytest.raises(FixtureAccessError):
            model.search(domain=domain)
    with pytest.raises(FixtureAccessError):
        model.read(ids=[1], fields=["secret"])
    with pytest.raises(FixtureAccessError):
        model.search(domain=[], order="name desc")


def test_fixture_records_cannot_include_reference_answers_or_duplicate_identities():
    records = supplier_cancellation_control().records
    polluted = copy.deepcopy(records)
    polluted["sale.order"][0]["reference_request"] = {"sales_order_ids": [1]}
    with pytest.raises(FixtureAccessError, match="invalid_fixture_records"):
        FixtureConnection(polluted)
    duplicate = copy.deepcopy(records)
    duplicate["sale.order"].append(copy.deepcopy(duplicate["sale.order"][0]))
    with pytest.raises(FixtureAccessError, match="duplicate_fixture_id"):
        FixtureConnection(duplicate)
