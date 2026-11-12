"""A component cancellation uses the same real single-level recovery pipeline."""
import hashlib

from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.contract import contract_from_payload
from forgeflow.investigation.fixtures import FixtureConnection, supplier_cancellation_control
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import OdooSource
from forgeflow.recovery.storage import digest, read_json


def test_component_supply_is_linked_to_finished_demand_and_really_prepared(tmp_path, real_bundle):
    case = supplier_cancellation_control()
    records = case.records
    records["purchase.order"][0]["partner_id"] = 2
    records["purchase.order.line"][0].update(product_id=2, product_qty=20, price_unit=2)
    records["stock.move"][0].update(product_id=2, product_uom_qty=20)
    policy = case.policy.payload()
    policy["disruption"].update(vendor_ref="V-RAW", vendor_name="Fixture component vendor")
    contract = contract_from_payload(policy)
    connection = FixtureConnection(records)
    before = connection.fingerprint()
    text = "Fixture component vendor cancelled purchase order PO-CANCEL. Prepare recovery for the affected demand."
    service = InvestigationService(source=OdooSource(connection, contract), contract=contract,
        store=tmp_path / "store", bundle=real_bundle, connection_factory=lambda: connection,
        event={"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()})
    run = "component-control"

    def query(kind, **arguments):
        return service.dispatch(run, "query", {"kind": kind, "arguments": arguments})["records"]

    product = query("record", entity="product", id=1)[0]
    supplier = query("record", entity="supplier", id=2)[0]
    purchase = query("record", entity="purchase_order", id=1)[0]
    order = query("record", entity="sales_order", id=1)[0]
    budget = query("policy", order_id=1)[0]
    policy_record = query("policy")[0]
    offer = query("record", entity="supplier_offer", id=1)[0]
    # The actual BoM is available to the agent through the same query surface.
    relationships = query("relations", entity="product", id=2)
    line = next(row for row in relationships if row["entity"] == "bom_line")
    assert any(row["entity"] == "product" and row["id"] == "1"
               for row in query("relations", entity="bom", id=line["fields"]["bom_id"]))
    request = {"target_product_code": "FIN", "sales_order_ids": [1],
               "anchor_date": contract.anchor_date, "margin": contract.margin,
               "sales_order_budget_caps": {"1": contract.budgets["1"]},
               "disruption": {"kind": "supplier_cancellation", "vendor_ref": "V-RAW",
                              "offer_key": offer["fields"]["offer_key"]}}
    mappings = [("target_product_code", product, "default_code"),
                ("disruption.vendor_ref", supplier, "ref"),
                ("disruption.offer_key", offer, "offer_key"),
                ("sales_order_ids.0", order, "id"),
                ("sales_order_budget_caps.1", budget, "budget_cap"),
                ("anchor_date", policy_record, "anchor_date"),
                ("margin", policy_record, "margin"),
                ("disruption.kind", policy_record, "task_type")]
    draft = {"request": request, "supply_observation_id": purchase["observation_id"],
             "evidence": [{"field": field, "observation_id": row["observation_id"],
                           "source_field": name} for field, row, name in mappings]}
    checked = service.dispatch(run, "validate", {"draft": draft})
    assert checked["valid"], checked
    prepared = service.dispatch(run, "prepare", {"draft_id": checked["draft_id"]})
    assert prepared["status"] == "prepared", prepared
    review = read_json(service.root / "review.json")
    assert review["request_sha256"] == digest(request)
    assert {"bom:1", "bom_line:1"} <= set(review["verification_facts"])
    assert connection.fingerprint() == before
    assert connection.write_attempts == []
