"""Independent order-level dates and quantities; no ERP or provider calls."""
import copy

import pytest

from forgeflow.product.readiness import build_order_readiness, project_order_readiness

KEYS = {"plan_id": "a" * 64, "snapshot_sha256": "b" * 64}


def facts():
    request = {"anchor_date": "2026-01-01", "target_product_code": "FIN"}
    customer = {"ref": "c1", "task_order_ref": "SO-1", "demand": 10, "deadline": 8}
    allocation = {**customer, "accepted": True, "stock_allocated": 2, "assembly_units": 5,
                  "purchase_units": 3, "fulfilled_demand": 10}
    operations = [
        {"kind": "manufacturing_order", "target": {"token": "mo:1"},
         "after": {"product_code": "FIN", "quantity": 5, "due_day": 6}},
        {"kind": "purchase_order", "target": {"token": "po:1"},
         "after": {"product_code": "FIN", "quantity": 3, "arrival_day": 7, "supply_role": "finished"}},
        {"kind": "purchase_order", "target": {"token": "po:2"}, "after": None},
    ]
    flows = [{"sales_order": {"name": "SO-1"}, "product_code": "FIN", "source_token": token,
              "quantity": quantity, "available_day": day}
             for token, quantity, day in (("stock:FIN", 2, 0), ("mo:1", 5, 6), ("po:1", 3, 7))]
    return request, {"customers": [customer]}, {"allocations": [allocation]}, {
        "operations": operations, "projected_state": {"sales_order_flows": flows}}


def test_recomputes_latest_supply_date_and_buffer_with_retained_stock_and_new_sources():
    inputs = facts()
    before = copy.deepcopy(inputs)
    result = build_order_readiness(*inputs, **KEYS)
    assert result["orders"] == [{"order_ref": "SO-1", "demand_units": 10, "covered_units": 10,
        "stock_units": 2, "manufactured_units": 5, "purchased_units": 3,
        "customer_due_day": 8, "customer_due_date": "2026-01-09",
        "planned_ready_day": 7, "planned_ready_date": "2026-01-08", "buffer_days": 1, "status": "on_time"}]
    assert inputs == before
    assert result["basis"]["time_basis"] == "planned_supply_availability_days"
    result["orders"][0]["buffer_days"] = 0
    with pytest.raises(ValueError, match="invalid_order_readiness"):
        project_order_readiness(result, **KEYS)


@pytest.mark.parametrize("day,status,buffer", [(8, "on_time", 0), (10, "late", -2)])
def test_zero_and_negative_planning_buffer_are_not_claims_of_actual_delivery(day, status, buffer):
    inputs = facts()
    inputs[3]["operations"][1]["after"]["arrival_day"] = day
    inputs[3]["projected_state"]["sales_order_flows"][2]["available_day"] = day
    row = build_order_readiness(*inputs, **KEYS)["orders"][0]
    assert (row["status"], row["buffer_days"]) == (status, buffer)


@pytest.mark.parametrize("fault", ["forged_day", "other_order", "other_product", "overallocated",
                                  "unmatched_plan", "changed_deadline", "duplicate_customer"])
def test_cross_checks_allocations_and_supply_sources(fault):
    inputs = facts()
    flow = inputs[3]["projected_state"]["sales_order_flows"][1]
    if fault == "forged_day":
        flow["available_day"] = 1
    elif fault == "other_order":
        flow["sales_order"]["name"] = "OTHER"
    elif fault == "other_product":
        flow["product_code"] = "RAW"
    elif fault == "overallocated":
        flow["quantity"] = 6
    elif fault == "unmatched_plan":
        inputs[2]["allocations"][0]["assembly_units"] = 4
    elif fault == "changed_deadline":
        inputs[2]["allocations"][0]["deadline"] = 9
    else:
        inputs[1]["customers"].append(copy.deepcopy(inputs[1]["customers"][0]))
    with pytest.raises(ValueError, match="invalid_order_readiness"):
        build_order_readiness(*inputs, **KEYS)


def test_missing_history_stays_missing_and_uncovered_orders_do_not_get_a_ready_date():
    assert project_order_readiness(None, **KEYS) is None
    inputs = facts()
    inputs[3]["projected_state"]["sales_order_flows"] = []
    inputs[2]["allocations"][0].update(stock_allocated=0, assembly_units=0, purchase_units=0, fulfilled_demand=0)
    row = build_order_readiness(*inputs, **KEYS)["orders"][0]
    assert row["status"] == "incomplete" and row["covered_units"] == 0
    assert row["planned_ready_date"] is None and row["buffer_days"] is None


@pytest.mark.parametrize("fault", ["binding", "extra_field", "date", "quantity", "false_pass", "bool_count"])
def test_public_projection_rejects_rebinding_arithmetic_errors_and_extra_fields(fault):
    value = build_order_readiness(*facts(), **KEYS)
    row = value["orders"][0]
    if fault == "binding":
        value["basis"]["snapshot_sha256"] = "c" * 64
    elif fault == "extra_field":
        row["private_path"] = "CANARY"
    elif fault == "date":
        row["planned_ready_date"] = "2026-01-01"
    elif fault == "quantity":
        row["covered_units"] = 11
    elif fault == "false_pass":
        row["status"] = "verified"
    else:
        row["stock_units"] = True
    with pytest.raises(ValueError, match="invalid_order_readiness"):
        project_order_readiness(value, **KEYS)


@pytest.mark.parametrize("question", ["What is the per-order ready date and date buffer?", "每单齐货日和交期余量是多少？"])
def test_answers_cite_the_recorded_plan_including_orders_after_index_two(question):
    from forgeflow.api.case_qa import answer_case

    value = build_order_readiness(*facts(), **KEYS)
    for index in range(2, 13):
        value["orders"].append({**value["orders"][0], "order_ref": f"SO-{index}"})
    case = {"state": "awaiting_approval", "plan": {**KEYS, "order_readiness": value}}
    result = answer_case(question, case)
    assert "SO-12" in result["answer"] and "2026-01-08" in result["answer"]
    citations = {row["field"]: row["value"] for row in result["citations"]}
    assert citations["plan.order_readiness.orders.11.planned_ready_date"] == "2026-01-08"
    assert citations["plan.order_readiness.orders.11.buffer_days"] == 1
    assert "不是实际送达结果" in result["answer"] or "not actual delivery results" in result["answer"]
    case["plan"].pop("order_readiness")
    missing = answer_case(question, case)
    assert "未知" in missing["answer"] or "unknown" in missing["answer"]
    assert "2026-01-08" not in missing["answer"]
    case["plan"]["order_readiness"] = value
    case["decision"] = {"revision_pending": True}
    assert "2026-01-08" not in answer_case(question, case)["answer"]
