"""Independent public-tool expectations for a one-level component disruption."""

import copy
from dataclasses import replace

from test_investigation_dev_dataset import service_for
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.contract import contract_from_payload
from forgeflow.investigation.fixtures import supplier_cancellation_control
from forgeflow.investigation.rules import run_rules
from forgeflow.recovery.storage import read_json


def component_case():
    case = supplier_cancellation_control()
    case.records["purchase.order"][0]["partner_id"] = 2
    case.records["purchase.order.line"][0].update(product_id=2, product_qty=20, price_unit=2)
    case.records["stock.move"][0].update(product_id=2, product_uom_qty=20)
    policy = case.policy.payload()
    policy["disruption"].update(vendor_ref="V-RAW", vendor_name="Fixture component vendor")
    return replace(case, event="Fixture component vendor cancelled purchase order PO-CANCEL.",
                   policy=contract_from_payload(policy))


def test_unique_finished_parent_uses_observed_bom_and_real_solver(real_bundle, tmp_path):
    case = component_case()
    connection, service = service_for(case, tmp_path, real_bundle)
    result = run_rules(case.event, lambda action, payload: service.dispatch(case.case_id, action, payload))
    assert result["result"]["status"] == "prepared", result
    request = read_json(service.root / "review.json")["request"]
    assert request["target_product_code"] == "FIN"
    assert request["sales_order_ids"] == [1]
    assert request["disruption"] == {"kind": "supplier_cancellation", "vendor_ref": "V-RAW", "offer_key": "V-RAW|RAW|1|2.0|1"}
    assert any(row["action"] == "query" and row["payload"]["arguments"].get("entity") == "bom" for row in result["trace"])
    assert connection.write_attempts == []


def test_two_active_finished_parents_require_a_specific_question(real_bundle, tmp_path):
    case = component_case()
    records = case.records
    for model, index, updates in [
        ("product.product", 0, {"id": 3, "product_tmpl_id": 3, "name": "FIN-OTHER", "default_code": "FIN-OTHER"}),
        ("mrp.bom", 0, {"id": 2, "product_tmpl_id": 3}),
        ("mrp.bom.line", 0, {"id": 2, "bom_id": 2}),
        ("sale.order", 0, {"id": 2, "name": "SO-2"}),
        ("sale.order.line", 0, {"id": 2, "order_id": 2, "product_id": 3}),
    ]:
        row = copy.deepcopy(records[model][index])
        row.update(updates)
        records[model].append(row)
    connection, service = service_for(case, tmp_path, real_bundle)
    result = run_rules(case.event, lambda action, payload: service.dispatch(case.case_id, action, payload))
    assert result["result"]["status"] == "needs_clarification", result
    assert result["result"]["terminal"]["reason"] == "component_target_ambiguous"
    question = result["result"]["terminal"]["question"]
    assert "FIN" in question and "FIN-OTHER" in question
    assert result["result"]["usage"]["prepares"] == 0
    assert connection.write_attempts == []
