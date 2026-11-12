"""Current packet checks exercise the exporter, fresh preparation and standalone reader."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "experiments/recovery-review-v2"
INPUT = ROOT / "evidence/recovery-review-v1/first/input/P1.json"


def load(path):
    return json.loads(path.read_bytes())


def pin(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review(out):
    register = out / "register.json"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            V2 / "reader.py",
            out / "packet",
            register,
            pin(register),
            load(register)["as_of"],
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=out,
    )
    return json.loads(result.stdout)


def export(case, dest):
    src = dest.parent / (dest.name + ".json")
    src.write_text(json.dumps(case))
    subprocess.run([sys.executable, V2 / "export.py", src, dest], check=True, capture_output=True)
    return review(dest)


def change(case, model, rid, field, new):
    row = next(r for r in case["records"][model] if r["id"] == rid)
    case["changes_after_preparation"].append(
        {
            "model": model,
            "id": rid,
            "field": field,
            "previous": row[field],
            "value": new,
            "effective_at": case["as_of"],
        }
    )


@pytest.mark.parametrize(
    "model,rid,field,value,expected",
    [
        ("mrp.workcenter", 1, "note", "10 minutes", "workcenter_capacity"),
        ("mrp.workcenter", 1, "note", "99 minutes", "workcenter_capacity"),
        ("mrp.workcenter", 1, "note", "no current capacity", "workcenter_capacity_note"),
        ("mrp.bom", 1, "type", "phantom", "bom_type"),
        ("mrp.bom", 1, "active", False, "applicable_bom"),
        ("mrp.bom", 1, "product_tmpl_id", 2, "applicable_bom"),
        ("mrp.bom", 1, "product_qty", 2, "bom_product_qty"),
        ("mrp.bom", 1, "product_uom_id", 7, "bom_product_uom_id"),
        ("mrp.bom", 1, "produce_delay", 2, "bom_produce_delay"),
        ("mrp.bom.line", 1, "bom_id", 999, "bom_component_lines"),
        ("mrp.bom.line", 1, "product_uom_id", 6, "bom_component_lines"),
        ("mrp.bom.line", 1, "product_qty", 3, "bom_component_lines"),
        ("mrp.bom.line", 1, "product_id", 1, "bom_component_lines"),
        (
            "res.partner",
            2,
            "comment",
            "Maximum order quantity for RAW: 10 units",
            "supplier_quantity_limit",
        ),
        (
            "res.partner",
            2,
            "comment",
            "Maximum order quantity for RAW: ten cases",
            "supplier_quantity_limit",
        ),
        (
            "res.partner",
            2,
            "comment",
            "Maximum order quantity for RAW: 10 units per hour",
            "supplier_quantity_limit",
        ),
        ("res.partner", 2, "comment", None, "supplier_quantity_limit"),
        ("product.product", 2, "uom_id", 6, "purchase_product_or_unit"),
    ],
)
def test_current_business_revision(tmp_path, model, rid, field, value, expected):
    case = load(INPUT)
    if model == "mrp.workcenter" and value.endswith(" minutes"):
        note = case["records"][model][0]["note"]
        value = note.replace("100 total", value.split()[0] + " total")
    change(case, model, rid, field, value)
    reply = export(case, tmp_path / "case")
    assert (reply["decision"], reply["field"], reply["next_action"]) == (
        "needs_evidence",
        expected,
        "refresh_source_and_prepare",
    )
    if model == "mrp.workcenter" and value.startswith("Benchmark"):
        assert reply["planned_minutes"] == 14
    if model == "res.partner":
        assert reply["planned_units"] == 14


@pytest.mark.parametrize(
    "group,name,decision",
    [
        ("first", "P1", "ready_for_approval"),
        ("second", "P2", "ready_for_approval"),
        ("first", "C1", "needs_clarification"),
        ("second", "R1", "needs_evidence"),
    ],
)
def test_existing_case_outcomes(tmp_path, group, name, decision):
    case = load(INPUT.parents[2] / group / "input" / (name + ".json"))
    r = export(case, tmp_path / name)
    assert r["decision"] == decision
    if decision == "ready_for_approval":
        assert r["workcenter_loads"][0]["planned_minutes"] == {"P1": 14, "P2": 12}[name]
        assert r["supplier_limits"][0]["planned_units"] == {"P1": 14, "P2": 18}[name]


@pytest.mark.parametrize(
    "note, expected",
    [
        ("", {"status": "unbounded"}),
        ("Maximum order quantity for RAW: 14 units", {"status": "bounded", "units": 14}),
    ],
)
def test_explicit_source_without_cap_or_exact_boundary(tmp_path, note, expected):
    case = load(INPUT)
    case["records"]["res.partner"][1]["comment"] = note
    wc = case["records"]["mrp.workcenter"][0]
    wc["note"] = wc["note"].replace("100 total", "14 total")
    reply = export(case, tmp_path / "case")
    assert reply["decision"] == "ready_for_approval"
    assert reply["workcenter_loads"][0]["capacity_minutes"] == 14
    assert reply["supplier_limits"][0]["current_limit"] == expected


def module():
    spec = importlib.util.spec_from_file_location("offline_v2", V2 / "reader.py")
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


def test_multiple_orders_share_one_capacity_and_vendor_sku_limit(tmp_path):
    case = load(INPUT)
    wc = case["records"]["mrp.workcenter"][0]
    wc["note"] = wc["note"].replace("100 total", "20 total")
    case["records"]["res.partner"][1]["comment"] = "Maximum order quantity for RAW: 20 units"
    dest = tmp_path / "case"
    assert export(case, dest)["decision"] == "ready_for_approval"
    reader = module()
    current = {p.stem: load(p) for p in (dest / "packet/base/packet").glob("*.json")}
    state, snap, src = (
        current["product.change-set"]["projected_state"],
        current["product.snapshot"]["models"],
        load(dest / "packet/current-inputs.json"),
    )
    for check, records, metric in (
        (reader.center_loads, state["manufacturing_orders"], "planned_minutes"),
        (reader.purchase_limits, state["purchase_orders"], "planned_units"),
    ):
        with pytest.raises(reader.base.Gather) as error:
            check(records + deepcopy(records), current, snap, src)
        assert error.value.details[metric] == 28
    src["workorders"].append(
        {"id": 99, "workcenter_id": 1, "production_id": 9, "product_id": 1, "state": "pending"}
    )
    with pytest.raises(reader.base.Gather, match="workcenter_active_workorders"):
        reader.center_loads(state["manufacturing_orders"], current, snap, src)
    src["supplier_limits"] = []
    with pytest.raises(reader.base.Gather, match="supplier_limit_evidence"):
        reader.purchase_limits(state["purchase_orders"], current, snap, src)


def test_current_note_safety_and_pinned_inputs(tmp_path):
    case = load(INPUT)
    secret = "irrelevant personal note"
    cap = "Maximum order quantity for RAW: 100 units"
    change(case, "res.partner", 2, "comment", cap + ". " + secret)
    out = tmp_path / "case"
    assert export(case, out)["decision"] == "ready_for_approval"
    assert all(secret not in p.read_text() for p in out.rglob("*.json"))
    (out / "packet/current-inputs.json").write_text("{}")
    assert review(out) == {"decision": "unverifiable", "error": "current_inputs_hash_mismatch"}
    case["records"]["res.partner"][1]["comment"] = secret
    with pytest.raises(subprocess.CalledProcessError) as error:
        export(case, tmp_path / "private")
    assert b"unrestricted_preparation_partner_note" in error.value.stderr
    assert not (tmp_path / "private").exists()


def test_v1_pin_does_not_assert_v2_checks():
    reader = module()
    old = INPUT.parents[1] / "bundles/P1"
    register = old / "register.json"
    assert reader.inspect_packet(
        old / "packet", register, pin(register), load(register)["as_of"]
    ) == {"decision": "unverifiable", "error": "requires_v2_register_at_review_instant"}


def test_phantom_bom_invalidates_incident_even_with_only_purchased_replacement(tmp_path):
    case = load(INPUT.parents[2] / "second/input/P2.json")
    for quote in case["records"]["product.supplierinfo"]:
        if quote["product_tmpl_id"] == 1:
            quote["price"] = 3
    baseline = tmp_path / "baseline"
    assert export(case, baseline)["decision"] == "ready_for_approval"
    state = load(baseline / "packet/base/packet/product.change-set.json")["projected_state"]
    assert state["manufacturing_orders"] == []
    change(case, "mrp.bom", 1, "type", "phantom")
    verdict = export(case, tmp_path / "phantom")
    assert (verdict["decision"], verdict["field"]) == ("needs_evidence", "bom_type")


def case_with_active_purchase():
    case = load(INPUT)
    for model, updates in (
        ("purchase.order", {"state": "purchase", "partner_id": 2}),
        ("stock.move", {"state": "assigned"}),
        ("stock.picking", {"state": "assigned"}),
    ):
        next(r for r in case["records"][model] if r["id"] == 14).update(updates)
    return case


@pytest.mark.parametrize(
    "model,field,value,expected",
    [
        (None, None, None, None),
        ("purchase.order", "state", "cancel", "purchase_commitment_state"),
        ("purchase.order.line", "qty_received", 4, "purchase_commitment_qty_received"),
    ],
)
def test_plan_depends_on_existing_component_purchase(tmp_path, model, field, value, expected):
    case = case_with_active_purchase()
    if model:
        change(case, model, 14, field, value)
    dest = tmp_path / "case"
    result = export(case, dest)
    ops = load(dest / "packet/base/packet/product.change-set.json")["operations"]
    assert any(
        op["kind"] == "purchase_order" and op["action"] == "update" and op["target"]["id"] == 14
        for op in ops
    )
    if expected:
        assert (result["decision"], result["field"], result["source"]) == (
            "needs_evidence",
            expected,
            f"{model}:14",
        )
    else:
        assert result["decision"] == "ready_for_approval"


@pytest.mark.parametrize("allows_selected_center", [False, True])
def test_selected_workcenter_must_still_be_routable(tmp_path, allows_selected_center):
    case = load(INPUT)
    change(case, "mrp.routing.workcenter", 1, "workcenter_id", 2)
    if allows_selected_center:
        change(case, "mrp.workcenter", 2, "alternative_workcenter_ids", [1])
    dest = tmp_path / "case"
    result = export(case, dest)
    state = load(dest / "packet/base/packet/product.change-set.json")["projected_state"]
    assert [r["workcenter_id"] for r in state["manufacturing_orders"]] == [1]
    if allows_selected_center:
        assert result["decision"] == "ready_for_approval"
    else:
        assert (
            result["decision"],
            result["field"],
            result["planned_workcenter"],
            result["current_allowed_workcenters"],
        ) == ("needs_evidence", "workcenter_route", 1, [2])


def test_historical_packet_needs_current_dependency_evidence(tmp_path):
    # The untouched release snapshot has a valid pin but cannot answer new checks.
    import zipfile

    with zipfile.ZipFile(ROOT / "evidence/recovery-review-v2/review-packets.zip") as old:
        for name in old.namelist():
            if name.startswith("cases/P1/"):
                old.extract(name, tmp_path)
    result = review(tmp_path / "cases/P1")
    assert (result["decision"], result["field"]) == ("needs_evidence", "purchase_line_evidence")


def test_removing_selected_alternative_workcenter_requires_new_plan(tmp_path):
    case = load(INPUT)
    primary = case["records"]["mrp.workcenter"][0]
    primary["note"] = primary["note"].replace("100 total", "0 total")
    change(case, "mrp.workcenter", 1, "alternative_workcenter_ids", [])
    result = export(case, tmp_path / "case")
    assert (
        result["decision"],
        result["field"],
        result["planned_workcenter"],
        result["current_allowed_workcenters"],
    ) == ("needs_evidence", "workcenter_route", 2, [1])
