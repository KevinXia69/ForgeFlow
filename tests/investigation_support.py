"""Small public business fixtures shared by investigation acceptance tests."""

import copy
import hashlib

from forgeflow.contract import build_contract
from forgeflow.investigation.sources import FixtureSource
from forgeflow.recovery.storage import atomic_json, digest

EVENT_TEXT = "Supplier A cancelled purchase order PO-20."
EVENT = {"text": EVENT_TEXT, "sha256": hashlib.sha256(EVENT_TEXT.encode()).hexdigest()}

def business_record(entity, identity, **fields):
    return {"entity": entity, "id": str(identity), "fields": fields}


def make_case(order_ids=(30,)):
    contract = build_contract(
        contract_id="investigation-test", task_id="supplier-case",
        task_type="supplier_cancellation", product_code="FIN", anchor_date="2026-01-01",
        margin=0.2, margin_basis="new_spend", budgets={str(i): 1000 for i in order_ids},
        sales_order_ids=list(order_ids),
        disruption={"kind": "supplier_cancellation", "vendor_ref": "V-1", "vendor_name": "Supplier A"},
        original_commitment_policy="repair_plan", sources={"policy": "public-task-policy"},
    )
    records = [
        business_record("supplier", 1, name="Supplier A", ref="V-1"),
        business_record("supplier", 2, name="Supplier B", ref="V-2"),
        business_record("product", 10, name="Finished item", default_code="FIN", product_template_id="100"),
        business_record("product", 11, name="Other item", default_code="OTHER", product_template_id="101"),
        business_record("purchase_order", 20, name="PO-20", state="purchase", supplier_id="1", origin="SO-30"),
        business_record("purchase_order", 21, name="PO-21", state="purchase", supplier_id="2", origin="SO-30"),
        business_record("purchase_order_line", 200, order_id="20", product_id="10", quantity=5,
                        unit_price=60, received_quantity=0, planned_date="2026-01-02 10:00:00"),
        business_record("purchase_order_line", 201, order_id="21", product_id="10", quantity=5,
                        unit_price=60, received_quantity=0, planned_date="2026-01-02 10:00:00"),
        business_record("supplier_offer", 1000, supplier_id="1", supplier_ref="V-1", product_id="10",
                        product_template_id="100", product_code="FIN", unit_price=60.0,
                        lead_days=1, minimum_quantity=1, offer_key="V-1|FIN|1|60.0|1"),
        business_record("supplier_offer", 1001, supplier_id="2", supplier_ref="V-2", product_id="10",
                        product_template_id="100", product_code="FIN", unit_price=60.0,
                        lead_days=1, minimum_quantity=1, offer_key="V-2|FIN|1|60.0|1"),
        business_record("supplier_offer", 1002, supplier_id="1", supplier_ref="V-1", product_id="11",
                        product_template_id="101", product_code="OTHER", unit_price=60.0,
                        lead_days=1, minimum_quantity=1, offer_key="V-1|OTHER|1|60.0|1"),
    ]
    for identity in (30, 31):
        records += [
            business_record("sales_order", identity, name=f"SO-{identity}",
                            state="sale" if identity in order_ids else "draft", customer_id=str(identity + 50),
                            commitment_date="2026-01-10 12:00:00", client_order_ref=""),
            business_record("sales_order_line", identity * 10, order_id=str(identity), product_id="10",
                            quantity=10, unit_price=1000, delivered_quantity=0),
        ]
    source = FixtureSource(records, contract)
    request = {
        "target_product_code": "FIN", "sales_order_ids": list(order_ids),
        "anchor_date": "2026-01-01", "margin": 0.2,
        "sales_order_budget_caps": {str(i): 1000 for i in order_ids},
        "disruption": {"kind": "supplier_cancellation", "vendor_ref": "V-1"},
    }
    return source, contract, request


def source_record(source, entity, identity):
    return next(row for row in source.records if row["entity"] == entity and row["id"] == str(identity))


def observe(observations, record):
    record = copy.deepcopy(record)
    observation_id = record.pop("observation_id", digest(record))
    observations[observation_id] = {"record": record, "observed_at": 1}
    return observation_id


def make_draft(source, request, *, query=None):
    """Use actual source/service queries to construct field-specific citations."""
    query = query or source.query
    observations, records = {}, {}

    def read(kind, arguments):
        result = query(kind, arguments)
        for row in result["records"]:
            identity = observe(observations, row)
            records[(row["entity"], row["id"])] = identity

    for entity, identity in (("product", 10), ("supplier", 1), ("purchase_order", 20)):
        read("record", {"entity": entity, "id": identity})
    read("policy", {})
    policy = next(identity for (entity, _), identity in records.items() if entity == "policy")
    evidence = [
        {"field": "target_product_code", "observation_id": records[("product", "10")],
         "source_field": "default_code"},
        {"field": "anchor_date", "observation_id": policy, "source_field": "anchor_date"},
        {"field": "margin", "observation_id": policy, "source_field": "margin"},
        {"field": "disruption.kind", "observation_id": policy, "source_field": "task_type"},
        {"field": "disruption.vendor_ref", "observation_id": records[("supplier", "1")],
         "source_field": "ref"},
    ]
    for index, identity in enumerate(request["sales_order_ids"]):
        read("record", {"entity": "sales_order", "id": identity})
        read("policy", {"order_id": identity})
        evidence += [
            {"field": f"sales_order_ids.{index}",
             "observation_id": records[("sales_order", str(identity))], "source_field": "id"},
            {"field": f"sales_order_budget_caps.{identity}",
             "observation_id": records[("order_policy", str(identity))], "source_field": "budget_cap"},
        ]
    draft = {"request": copy.deepcopy(request), "evidence": evidence,
             "supply_observation_id": records[("purchase_order", "20")], "reason": "initial investigation"}
    return draft, observations


def bind_offer(draft, observations, source, offer_id):
    row = source.query("record", {"entity": "supplier_offer", "id": offer_id})["records"][0]
    identity = observe(observations, row)
    draft["request"]["disruption"]["offer_key"] = row["fields"]["offer_key"]
    draft["evidence"].append({"field": "disruption.offer_key", "observation_id": identity,
                              "source_field": "offer_key"})


def prepared_artifacts(store, request, *, actual_request=None, manifest_request=None):
    """A stand-in only at recovery.prepare, preserving real on-disk binding checks."""
    actual = copy.deepcopy(actual_request if actual_request is not None else request)
    changes = {"operations": [{"kind": "purchase_order", "action": "cancel", "target": {"id": 20}},
                              {"kind": "purchase_order", "action": "create", "target": {"id": None}}]}
    manifest = {"schema_version": 1, "artifacts": {
        "change-set": digest(changes),
        "request": digest(manifest_request if manifest_request is not None else request),
    }}
    plan_id = digest(manifest)
    directory = store / plan_id
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "change-set.json", changes, replace=True)
    atomic_json(directory / "request.json", actual, replace=True)
    atomic_json(directory / "manifest.json", manifest, replace=True)
    return {"ok": True, "odoo_writes": False, "plan_id": plan_id,
            "operation_counts": {"keep": 0, "update": 0, "cancel": 1, "create": 1},
            "step_count": 2, "expected_new_spend": 100}
