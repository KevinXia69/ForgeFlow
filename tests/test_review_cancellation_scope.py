"""A costly, unaffected purchase must not be cancelled by a single-notice review."""

from pathlib import Path

import pytest
from test_recovery_scope_closure import real_bundle as real_bundle

from forgeflow.contract import contract_from_payload
from forgeflow.investigation.fixtures import FixtureConnection
from forgeflow.product.executor import ProductExecutionError
from forgeflow.product.review_facts import check_confirmed_supply
from forgeflow.recovery.change_set import ChangeSetError
from forgeflow.recovery.service import prepare_product
from forgeflow.recovery.storage import digest, read_json

ROOT = Path(__file__).resolve().parents[1] / "evidence/recovery-review-v1/first"
SUBJECT = {"purchase_order_id": 1, "purchase_line_id": 1, "supplier_id": 3, "product_id": 1}
REASON = "additional_purchase_cancellation_unsupported"


def inputs(*, add_purchase):
    case = read_json(ROOT / "input/P1.json")
    records = case["records"]
    if add_purchase:
        # A second vendor has already promised one finished unit. Keeping it would
        # exceed the permitted spend; the unrestricted planner removes it.
        quote = next(q for q in records["product.supplierinfo"] if q["partner_id"] == 4)
        quote["price"] = 800
        additions = {
            "purchase.order": {
                "partner_id": 4,
                "name": "PO-OTHER-91",
                "order_line": [91],
                "picking_ids": [91],
            },
            "purchase.order.line": {
                "order_id": 91,
                "move_ids": [91],
                "price_unit": 800,
                "product_qty": 1,
            },
            "stock.picking": {"move_ids": [91]},
            "stock.move": {"purchase_line_id": 91, "picking_id": 91, "product_uom_qty": 1},
        }
        for model, fields in additions.items():
            origin = next(r for r in records[model] if r["id"] == 1)
            records[model].append(origin | fields | {"id": 91})
    return (
        FixtureConnection(records),
        read_json(ROOT / "bundles/P1/packet/product.request.json"),
        contract_from_payload(case["policy"]),
    )


def prepare(inputs, bundle, store):
    connection, request, policy = inputs
    result = prepare_product(
        connection,
        request,
        run_id="scope-test",
        owner_sha256=digest("owner"),
        expected_revision=0,
        operation_id="prepare",
        store=store,
        bundle=bundle,
        contract=policy,
    )
    return store / result["selected_plan_id"]


@pytest.mark.parametrize("add_purchase", [False, True])
def test_authorization_of_original_unscoped_plan_rechecks_all_cancellations(
    tmp_path, real_bundle, add_purchase
):
    data = inputs(add_purchase=add_purchase)
    directory = prepare(data, real_bundle, tmp_path / "store")
    snapshot, blueprint, changes = [
        read_json(directory / (name + ".json")) for name in ("snapshot", "blueprint", "change-set")
    ]
    actual = {
        op["target"]["id"]
        for op in changes["operations"]
        if op["action"] == "cancel" and op["kind"] == "purchase_order"
    }
    assert actual == ({1, 91} if add_purchase else {1})
    if add_purchase:
        with pytest.raises(ProductExecutionError, match=REASON):
            check_confirmed_supply(snapshot, blueprint, changes, SUBJECT)
    else:
        check_confirmed_supply(snapshot, blueprint, changes, SUBJECT)
    assert data[0].write_attempts == []


def test_scoped_request_refuses_extra_cancellation_before_preparation(tmp_path, real_bundle):
    data = inputs(add_purchase=True)
    data[1]["disruption"].update(purchase_order_id=1, purchase_line_id=1)
    store = tmp_path / "store"
    with pytest.raises(ChangeSetError) as error:
        prepare(data, real_bundle, store)
    assert error.value.code == REASON
    assert not list(store.glob("*/prepared.json"))
    assert data[0].write_attempts == []
