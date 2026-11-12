"""Live Odoo: cancelled MO -> BOM/workcenter plan -> MO + component purchase."""
from __future__ import annotations

import os
import uuid
import xmlrpc.client
from datetime import UTC, datetime, timedelta

import pytest

from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config
from forgeflow.integrations.odoo20.business import Odoo20BusinessService
from forgeflow.superagent.manufacturing import prepare_manufacturing_plan

URL = os.environ.get("FORGEFLOW_ODOO20_URL", "")
DB = os.environ.get("FORGEFLOW_ODOO20_DB", "")
LOGIN = os.environ.get("FORGEFLOW_ODOO20_LOGIN", "")
PASSWORD = os.environ.get("FORGEFLOW_ODOO20_PASSWORD", "")
pytestmark = pytest.mark.skipif(not all((URL, DB, LOGIN, PASSWORD)),
                                reason="Odoo 20 credentials not configured")


def test_cancelled_mo_is_recovered_with_bom_and_workcenter() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    suffix = uuid.uuid4().hex[:8]
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {"name": f"Make vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Make component {suffix}", "default_code": f"MK-C-{suffix}",
        "type": "consu"})
    finished = adapter.create("product.product", {
        "name": f"Make finished {suffix}", "default_code": f"MK-F-{suffix}",
        "type": "consu"})
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template, "min_qty": 0,
        "price": 2.0, "delay": 0, "company_id": 1}], ctx)
    workcenter = models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "create", [{
        "name": f"Make WC {suffix}", "code": f"MW{suffix[:3]}"}], ctx)
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-MK-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2})],
        "operation_ids": [(0, 0, {"name": "Assembly", "workcenter_id": workcenter,
                                  "time_cycle_manual": 30})],
    }], ctx)
    due = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d 09:00:00")
    mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": finished, "product_qty": 3, "bom_id": bom,
        "date_start": due, "company_id": 1}], ctx)
    try:
        models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel", [[mo]], ctx)
    except xmlrpc.client.Fault:
        pass
    state = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "read",
                              [[mo]], {"fields": ["state"]})[0]["state"]
    assert state == "cancel", state
    service = Odoo20BusinessService(adapter)
    payload = prepare_manufacturing_plan(service, mo_id=int(mo),
                                         tenant_id=f"make-{suffix}")
    kinds = {action["type"] for action in payload["actions"]}
    assert "create_manufacturing_order" in kinds
    assert "create_purchase_order" in kinds
    prepared = payload["prepared"]
    service.approve(prepared, approver_uid=2, comment="manufacturing recovery")
    result = service.apply_plan(service.load_prepared(prepared.plan_id))
    assert result.succeeded
    verification = service.verify_plan(service.load_prepared(prepared.plan_id))
    assert verification.verified
    replacement = adapter.search_read(
        "mrp.production", [("product_id", "=", finished), ("id", "!=", int(mo))],
        ["id", "product_qty", "state"], limit=5)
    assert replacement and float(replacement[0]["product_qty"]) == 3
    purchases = adapter.search_read(
        "purchase.order", [("partner_id", "=", vendor)], ["id"], limit=5)
    assert purchases


def test_two_mos_share_a_component_in_one_plan() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    suffix = uuid.uuid4().hex[:6]
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {"name": f"Shared vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Shared component {suffix}", "default_code": f"SH-C-{suffix}",
        "type": "consu"})
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template, "min_qty": 0,
        "price": 2.0, "delay": 0, "company_id": 1}], ctx)
    workcenter = models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "create", [{
        "name": f"Shared WC {suffix}", "code": f"SW{suffix[:3]}"}], ctx)
    finished = []
    boms = []
    for index in (1, 2):
        product = adapter.create("product.product", {
            "name": f"Shared finished {suffix}-{index}",
            "default_code": f"SH-F-{suffix}-{index}", "type": "consu"})
        template = adapter.search_read("product.product", [("id", "=", product)],
                                       ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
        bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
            "code": f"BOM-SH-{suffix}-{index}", "product_tmpl_id": template,
            "product_qty": 1, "type": "normal",
            "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2})],
            "operation_ids": [(0, 0, {"name": "Assembly", "workcenter_id": workcenter,
                                      "time_cycle_manual": 10})],
        }], ctx)
        finished.append(product)
        boms.append(bom)
    due = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d 09:00:00")
    mos = [models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": product, "product_qty": quantity, "bom_id": bom,
        "date_start": due, "company_id": 1}], ctx)
        for product, bom, quantity in zip(finished, boms, (2, 1))]
    for mo in mos:
        try:
            models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                              [[mo]], ctx)
        except xmlrpc.client.Fault:
            pass
    service = Odoo20BusinessService(adapter)
    payload = prepare_manufacturing_plan(service, mo_ids=mos,
                                         tenant_id=f"shared-{suffix}")
    purchase_actions = [action for action in payload["actions"]
                        if action["type"] == "create_purchase_order"]
    assert len(purchase_actions) == 1
    assert float(purchase_actions[0]["quantity"]) == 6
    service.approve(payload["prepared"], approver_uid=2, comment="shared components")
    result = service.apply_plan(service.load_prepared(payload["prepared"].plan_id))
    assert result.succeeded
    assert service.verify_plan(service.load_prepared(payload["prepared"].plan_id)).verified
    replacement = adapter.search_read(
        "mrp.production", [("product_id", "in", finished), ("id", "not in", mos),
                           ("state", "!=", "cancel")], ["id"], limit=10)
    assert len(replacement) == 2
    purchase_lines = adapter.search_read(
        "purchase.order.line", [("product_id", "=", component)], ["product_qty"],
        limit=10)
    assert sum(float(row["product_qty"]) for row in purchase_lines) >= 6


def test_mo_bom_version_and_output_base_drive_component_quantity() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    suffix = uuid.uuid4().hex[:6]
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {"name": f"BOM vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"BOM component {suffix}", "default_code": f"BV-C-{suffix}",
        "type": "consu"})
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template, "min_qty": 0,
        "price": 1.0, "delay": 0, "company_id": 1}], ctx)
    finished = adapter.create("product.product", {
        "name": f"BOM finished {suffix}", "default_code": f"BV-F-{suffix}",
        "type": "consu"})
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    # Earliest BOM: 20 components per 10 outputs (2/unit).
    old_bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-OLD-{suffix}", "product_tmpl_id": template,
        "product_qty": 10, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 20,
                                 "uom_id": 1})],
    }], ctx)
    # The MO uses a later BOM: 30 components per 10 outputs (3/unit).
    new_bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-NEW-{suffix}", "product_tmpl_id": template,
        "product_qty": 10, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 30,
                                 "uom_id": 1})],
    }], ctx)
    assert old_bom != new_bom
    due = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d 09:00:00")
    mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": finished, "product_qty": 10, "bom_id": new_bom,
        "date_start": due, "company_id": 1}], ctx)
    try:
        models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                          [[mo]], ctx)
    except xmlrpc.client.Fault:
        pass
    service = Odoo20BusinessService(adapter)
    payload = prepare_manufacturing_plan(service, mo_id=int(mo),
                                         tenant_id=f"bom-{suffix}")
    purchases = [action for action in payload["actions"]
                 if action["type"] == "create_purchase_order"]
    assert len(purchases) == 1
    # 10 units x (30/10) = 30, not the old BOM's 20 or the raw 300.
    assert float(purchases[0]["quantity"]) == 30
    service.approve(payload["prepared"], approver_uid=2, comment="bom version")
    result = service.apply_plan(service.load_prepared(payload["prepared"].plan_id))
    assert result.succeeded
    assert service.verify_plan(service.load_prepared(payload["prepared"].plan_id)).verified


def _capacity_fixture(adapter, models, DB, uid, PASSWORD, *, start_days=2,
                      due_days=2):
    suffix = uuid.uuid4().hex[:6]
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {"name": f"Cap vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Cap component {suffix}", "default_code": f"CAP-C-{suffix}",
        "type": "consu"})
    finished = adapter.create("product.product", {
        "name": f"Cap finished {suffix}", "default_code": f"CAP-F-{suffix}",
        "type": "consu"})
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    supplierinfo = models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template, "min_qty": 100,
        "price": 2.0, "delay": 30, "company_id": 1}], ctx)
    empty_calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "create", [{
        "name": f"Empty calendar {suffix}"}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "write",
                      [[empty_calendar], {"attendance_ids": [(5, 0, 0)]}], ctx)
    workcenter = models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "create", [{
        "name": f"Cap WC {suffix}", "code": f"CW{suffix[:3]}",
        "resource_calendar_id": empty_calendar}], ctx)
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-CAP-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                 "uom_id": 1})],
        "operation_ids": [(0, 0, {"name": "Assembly", "workcenter_id": workcenter,
                                  "time_cycle_manual": 30})],
    }], ctx)
    start = (datetime.now(UTC) + timedelta(days=start_days)).strftime(
        "%Y-%m-%d 09:00:00")
    due = (datetime.now(UTC) + timedelta(days=due_days)).strftime(
        "%Y-%m-%d 18:00:00")
    mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": finished, "product_qty": 3, "bom_id": bom,
        "date_start": start, "date_finished": due, "date_deadline": due,
        "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                      [[mo]], ctx)
    return {"suffix": suffix, "vendor": vendor, "component": component,
            "finished": finished, "supplierinfo": supplierinfo,
            "workcenter": workcenter, "bom": bom, "mo": mo}


def test_capacity_lead_and_moq_are_enforced() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    data = _capacity_fixture(adapter, models, DB, uid, PASSWORD)
    service = Odoo20BusinessService(adapter)
    # 30-day supplier quote cannot satisfy a 2-day recovery window.
    with pytest.raises(Exception, match="manufacturing_component_late"):
        prepare_manufacturing_plan(service, mo_id=int(data["mo"]),
                                   tenant_id=f"cap-{data['suffix']}")
    # An empty calendar means zero real capacity, even with a timely offer.
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "write",
                      [[data["supplierinfo"]], {"delay": 0}], ctx)
    with pytest.raises(Exception, match="manufacturing_(shortfall|time_infeasible)"):
        prepare_manufacturing_plan(service, mo_id=int(data["mo"]),
                                   tenant_id=f"cap-{data['suffix']}")
    # Give the workcenter a real 8h/day calendar; MOQ 100 must reach the PO.
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[data["workcenter"]], {"resource_calendar_id": calendar}], ctx)
    payload = prepare_manufacturing_plan(service, mo_id=int(data["mo"]),
                                         tenant_id=f"cap-{data['suffix']}")
    purchases = [action for action in payload["actions"]
                 if action["type"] == "create_purchase_order"]
    assert len(purchases) == 1 and float(purchases[0]["quantity"]) == 100
    assert payload["facts"]["manufacturing"]["procurement"][0]["quantity"] == 100
    service.approve(payload["prepared"], approver_uid=2, comment="capacity + MOQ")
    result = service.apply_plan(service.load_prepared(payload["prepared"].plan_id))
    assert result.succeeded


def test_an_active_mo_is_adjusted_not_duplicated() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    data = _capacity_fixture(adapter, models, DB, uid, PASSWORD)
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "write",
                      [[data["supplierinfo"]], {"delay": 0, "min_qty": 0}], ctx)
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[data["workcenter"]], {"resource_calendar_id": calendar}], ctx)
    # Recreate the same MO as confirmed instead of cancelled.
    mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": data["finished"], "product_qty": 3, "bom_id": data["bom"],
        "date_start": (datetime.now(UTC) + timedelta(days=2)).strftime(
            "%Y-%m-%d 09:00:00"), "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_confirm",
                      [[mo]], ctx)
    service = Odoo20BusinessService(adapter)
    payload = prepare_manufacturing_plan(service, mo_id=int(mo),
                                         tenant_id=f"active-{data['suffix']}")
    kinds = {action["type"] for action in payload["actions"]}
    assert "adjust_manufacturing_order" in kinds
    assert "create_manufacturing_order" not in kinds
    service.approve(payload["prepared"], approver_uid=2, comment="active recovery")
    result = service.apply_plan(service.load_prepared(payload["prepared"].plan_id))
    assert result.succeeded
    assert service.verify_plan(service.load_prepared(payload["prepared"].plan_id)).verified
    duplicates = adapter.search_read(
        "mrp.production", [("product_id", "=", data["finished"]),
                           ("id", "!=", int(mo)), ("state", "!=", "cancel")],
        ["id"], limit=5)
    assert duplicates == []


def test_material_arrival_after_start_moves_the_action_date() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    data = _capacity_fixture(adapter, models, DB, uid, PASSWORD,
                             start_days=1, due_days=5)
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "write",
                      [[data["supplierinfo"]], {"delay": 2, "min_qty": 0}], ctx)
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[data["workcenter"]], {"resource_calendar_id": calendar}], ctx)
    payload = prepare_manufacturing_plan(
        Odoo20BusinessService(adapter), mo_id=int(data["mo"]),
        tenant_id=f"arrival-{data['suffix']}")
    make = next(action for action in payload["actions"]
                if action["type"] == "create_manufacturing_order")
    assert make["params"]["date_start"].startswith(
        (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d"))


def test_a_leave_removes_capacity_even_with_a_normal_calendar() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    data = _capacity_fixture(adapter, models, DB, uid, PASSWORD)
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "write",
                      [[data["supplierinfo"]], {"delay": 0, "min_qty": 0}], ctx)
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[data["workcenter"]], {"resource_calendar_id": calendar}], ctx)
    today = datetime.now(UTC).date()
    leave = models.execute_kw(DB, uid, PASSWORD, "resource.calendar.leaves", "create", [{
        "name": "Recovery-window downtime", "calendar_id": calendar,
        "date_from": f"{today.isoformat()} 00:00:00",
        "date_to": f"{(today + timedelta(days=5)).isoformat()} 23:59:59"}], ctx)
    try:
        with pytest.raises(Exception, match="manufacturing_(shortfall|time_infeasible)"):
            prepare_manufacturing_plan(
                Odoo20BusinessService(adapter), mo_id=int(data["mo"]),
                tenant_id=f"downtime-{data['suffix']}")
    finally:
        models.execute_kw(DB, uid, PASSWORD, "resource.calendar.leaves", "unlink",
                          [[leave]], ctx)


def _active_fixture(adapter, models, DB, uid, PASSWORD, *, stock_qty=0):
    suffix = uuid.uuid4().hex[:6]
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {"name": f"Res vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Res component {suffix}", "default_code": f"RES-C-{suffix}",
        "type": "consu", "is_storable": True})
    finished = adapter.create("product.product", {
        "name": f"Res finished {suffix}", "default_code": f"RES-F-{suffix}",
        "type": "consu"})
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template, "min_qty": 0,
        "price": 2.0, "delay": 0, "company_id": 1}], ctx)
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-RES-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                 "uom_id": 1})]}], ctx)
    if stock_qty:
        location = models.execute_kw(DB, uid, PASSWORD, "stock.location", "search",
                                     [[["usage", "=", "internal"]]], {"limit": 1})[0]
        models.execute_kw(DB, uid, PASSWORD, "stock.quant", "create", [{
            "product_id": component, "location_id": location,
            "quantity": stock_qty, "company_id": 1}], ctx)
    due = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d 18:00:00")
    mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": finished, "product_qty": 2, "bom_id": bom,
        "date_start": due, "date_deadline": due, "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_confirm",
                      [[mo]], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_assign",
                      [[mo]], ctx)
    return {"suffix": suffix, "vendor": vendor, "component": component,
            "finished": finished, "bom": bom, "mo": mo}


def test_real_reservation_prevents_duplicate_component_purchase() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    data = _active_fixture(adapter, models, DB, uid, PASSWORD, stock_qty=4)
    payload = prepare_manufacturing_plan(
        Odoo20BusinessService(adapter), mo_id=int(data["mo"]),
        tenant_id=f"res-{data['suffix']}")
    assert not [action for action in payload["actions"]
                if action["type"] == "create_purchase_order"]
    assert payload["facts"]["manufacturing"]["procurement"] == []
    assert payload["facts"]["manufacturing"]["mode"] == "adjust"


def test_unreserved_demand_does_not_count_as_reservation() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    data = _active_fixture(adapter, models, DB, uid, PASSWORD, stock_qty=0)
    payload = prepare_manufacturing_plan(
        Odoo20BusinessService(adapter), mo_id=int(data["mo"]),
        tenant_id=f"nores-{data['suffix']}")
    purchases = [action for action in payload["actions"]
                 if action["type"] == "create_purchase_order"]
    assert len(purchases) == 1 and float(purchases[0]["quantity"]) == 4


def test_same_product_multiple_deadlines_are_order_independent() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    data = _capacity_fixture(adapter, models, DB, uid, PASSWORD,
                             start_days=1, due_days=2)
    due = (datetime.now(UTC) + timedelta(days=4)).strftime("%Y-%m-%d 18:00:00")
    second = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": data["finished"], "product_qty": 2, "bom_id": data["bom"],
        "date_start": due, "date_deadline": due, "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                      [[second]], ctx)
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "write",
                      [[data["supplierinfo"]], {"delay": 0, "min_qty": 0}], ctx)
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[data["workcenter"]], {"resource_calendar_id": calendar}], ctx)
    service = Odoo20BusinessService(adapter)
    first_order = prepare_manufacturing_plan(
        service, mo_ids=[int(data["mo"]), int(second)],
        tenant_id=f"order-a-{data['suffix']}")
    reversed_order = prepare_manufacturing_plan(
        service, mo_ids=[int(second), int(data["mo"])],
        tenant_id=f"order-b-{data['suffix']}")
    def dates(payload):
        return sorted((action.get("quantity"), action["params"].get("date_start"),
                       action["params"].get("date_deadline"))
                      for action in payload["actions"]
                      if action["type"] == "create_manufacturing_order")
    assert dates(first_order) == dates(reversed_order)


def test_partial_production_backorder_with_real_reservation() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    suffix = uuid.uuid4().hex[:6]
    component = adapter.create("product.product", {
        "name": f"Partial component {suffix}", "default_code": f"PT-C-{suffix}",
        "type": "consu", "is_storable": True})
    finished = adapter.create("product.product", {
        "name": f"Partial finished {suffix}", "default_code": f"PT-F-{suffix}",
        "type": "consu", "is_storable": True})
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-PT-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                 "uom_id": 1})]}], ctx)
    due = (datetime.now(UTC) + timedelta(days=4)).strftime("%Y-%m-%d 18:00:00")
    mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": finished, "product_qty": 10, "bom_id": bom,
        "date_start": due, "date_deadline": due, "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_confirm",
                      [[mo]], ctx)
    source = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "read",
                               [[mo]], {"fields": ["location_src_id"]})[0][
                                   "location_src_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "stock.quant", "create", [{
        "product_id": component, "location_id": source, "quantity": 20,
        "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_assign",
                      [[mo]], ctx)
    moves = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "read",
                              [[mo]], {"fields": ["move_raw_ids",
                                                  "move_finished_ids"]})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "write",
                      [[mo], {"qty_producing": 8}], ctx)
    raw_lines = models.execute_kw(
        DB, uid, PASSWORD, "stock.move", "read", [moves["move_raw_ids"]],
        {"fields": ["move_line_ids"]})[0]["move_line_ids"]
    fin_lines = models.execute_kw(
        DB, uid, PASSWORD, "stock.move", "read", [moves["move_finished_ids"]],
        {"fields": ["move_line_ids"]})[0]["move_line_ids"]
    models.execute_kw(DB, uid, PASSWORD, "stock.move.line", "write",
                      [raw_lines, {"quantity": 16}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "stock.move.line", "write",
                      [fin_lines, {"quantity": 8}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "button_mark_done",
                      [[mo]], ctx)
    try:
        bid = models.execute_kw(DB, uid, PASSWORD, "mrp.production.backorder",
                                "create", [{
                                    "mrp_production_ids": [(6, 0, [mo])],
                                    "mrp_production_backorder_line_ids": [
                                        (0, 0, {"mrp_production_id": mo,
                                                "to_backorder": True})]}], ctx)
        models.execute_kw(DB, uid, PASSWORD, "mrp.production.backorder",
                          "action_backorder", [[bid]], ctx)
    except xmlrpc.client.Fault:
        pass  # the action can fault after creating the backorder
    produced = adapter.search_read(
        "mrp.production", [("product_id", "=", finished)],
        ["id", "state", "product_qty", "qty_produced", "backorder_sequence"],
        limit=10)
    original = next(row for row in produced if int(row["id"]) == int(mo))
    backorder = next(row for row in produced
                     if int(row["id"]) != int(mo) and row["state"] != "cancel")
    assert float(original["qty_produced"]) == 8
    assert float(backorder["product_qty"]) == 2
    payload = prepare_manufacturing_plan(
        Odoo20BusinessService(adapter), mo_id=int(backorder["id"]),
        tenant_id=f"partial-{suffix}")
    assert payload["facts"]["manufacturing"]["mode"] == "adjust"
    assert not [action for action in payload["actions"]
                if action["type"] == "create_purchase_order"]
    service = Odoo20BusinessService(adapter)
    service.approve(payload["prepared"], approver_uid=2, comment="partial backorder")
    result = service.apply_plan(service.load_prepared(payload["prepared"].plan_id))
    assert result.succeeded
    assert service.verify_plan(service.load_prepared(payload["prepared"].plan_id)).verified
    assert adapter.search_read(
        "mrp.production", [("id", "=", int(original["id"]))],
        ["qty_produced"], limit=1)[0]["qty_produced"] == 8


def test_early_deadline_cannot_borrow_later_capacity() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    data = _capacity_fixture(adapter, models, DB, uid, PASSWORD,
                             start_days=2, due_days=4)
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "write",
                      [[data["supplierinfo"]], {"delay": 0, "min_qty": 0}], ctx)
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[data["workcenter"]], {"resource_calendar_id": calendar}], ctx)
    early = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d 18:00:00")
    huge = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": data["finished"], "product_qty": 40, "bom_id": data["bom"],
        "date_start": early, "date_deadline": early, "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                      [[huge]], ctx)
    with pytest.raises(Exception, match="manufacturing_time_infeasible"):
        prepare_manufacturing_plan(
            Odoo20BusinessService(adapter), mo_ids=[int(huge), int(data["mo"])],
            tenant_id=f"early-bad-{data['suffix']}")
    small = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": data["finished"], "product_qty": 2, "bom_id": data["bom"],
        "date_start": early, "date_deadline": early, "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                      [[small]], ctx)
    payload = prepare_manufacturing_plan(
        Odoo20BusinessService(adapter), mo_ids=[int(small), int(data["mo"])],
        tenant_id=f"early-ok-{data['suffix']}")
    assert len([action for action in payload["actions"]
                if action["type"] == "create_manufacturing_order"]) == 2


def _partial_backorder_fixture(adapter, models, DB, uid, PASSWORD):
    suffix = uuid.uuid4().hex[:6]
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    vendor = adapter.create("res.partner", {"name": f"P2B vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"P2B component {suffix}", "default_code": f"P2B-C-{suffix}",
        "type": "consu", "is_storable": True})
    finished = adapter.create("product.product", {
        "name": f"P2B finished {suffix}", "default_code": f"P2B-F-{suffix}",
        "type": "consu", "is_storable": True})
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template,
        "min_qty": 1, "price": 2.0, "delay": 0, "company_id": 1}], ctx)
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-P2B-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                 "uom_id": 1})]}], ctx)
    due = (datetime.now(UTC) + timedelta(days=4)).strftime("%Y-%m-%d 18:00:00")
    mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
        "product_id": finished, "product_qty": 10, "bom_id": bom,
        "date_start": due, "date_deadline": due, "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_confirm",
                      [[mo]], ctx)
    source = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "read",
                               [[mo]], {"fields": ["location_src_id"]})[0][
                                   "location_src_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "stock.quant", "create", [{
        "product_id": component, "location_id": source, "quantity": 20,
        "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_assign",
                      [[mo]], ctx)
    moves = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "read",
                              [[mo]], {"fields": ["move_raw_ids",
                                                  "move_finished_ids"]})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "write",
                      [[mo], {"qty_producing": 8}], ctx)
    raw_lines = models.execute_kw(
        DB, uid, PASSWORD, "stock.move", "read", [moves["move_raw_ids"]],
        {"fields": ["move_line_ids"]})[0]["move_line_ids"]
    fin_lines = models.execute_kw(
        DB, uid, PASSWORD, "stock.move", "read", [moves["move_finished_ids"]],
        {"fields": ["move_line_ids"]})[0]["move_line_ids"]
    models.execute_kw(DB, uid, PASSWORD, "stock.move.line", "write",
                      [raw_lines, {"quantity": 16}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "stock.move.line", "write",
                      [fin_lines, {"quantity": 8}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "button_mark_done",
                      [[mo]], ctx)
    try:
        bid = models.execute_kw(DB, uid, PASSWORD, "mrp.production.backorder",
                                "create", [{
                                    "mrp_production_ids": [(6, 0, [mo])],
                                    "mrp_production_backorder_line_ids": [
                                        (0, 0, {"mrp_production_id": mo,
                                                "to_backorder": True})]}], ctx)
        models.execute_kw(DB, uid, PASSWORD, "mrp.production.backorder",
                          "action_backorder", [[bid]], ctx)
    except xmlrpc.client.Fault:
        pass
    backorder = next(row for row in adapter.search_read(
        "mrp.production", [("product_id", "=", finished)],
        ["id", "state", "product_qty", "qty_produced"], limit=10)
        if int(row["id"]) != int(mo) and row["state"] != "cancel")
    return {"suffix": suffix, "vendor": vendor, "component": component,
            "finished": finished, "source": source, "mo": mo,
            "backorder": backorder}


def test_backorder_with_two_reserved_components_buys_only_two() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    data = _partial_backorder_fixture(adapter, models, DB, uid, PASSWORD)
    backorder = int(data["backorder"]["id"])
    moves = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "read",
                              [[backorder]], {"fields": ["move_raw_ids"]})[0]
    raw_lines = models.execute_kw(
        DB, uid, PASSWORD, "stock.move", "read", [moves["move_raw_ids"]],
        {"fields": ["move_line_ids"]})[0]["move_line_ids"]
    # Keep 2 of the 4 reserved components; reduce on-hand so public free stock is 0.
    models.execute_kw(DB, uid, PASSWORD, "stock.move.line", "write",
                      [raw_lines, {"quantity": 2}], ctx)
    quants = models.execute_kw(DB, uid, PASSWORD, "stock.quant", "search",
                               [[["product_id", "=", data["component"]],
                                 ["location_id", "=", data["source"]]]], {"limit": 5})
    models.execute_kw(DB, uid, PASSWORD, "stock.quant", "write",
                      [quants, {"quantity": 2}], ctx)
    payload = prepare_manufacturing_plan(
        Odoo20BusinessService(adapter), mo_id=backorder,
        tenant_id=f"p2b-{data['suffix']}")
    purchases = [action for action in payload["actions"]
                 if action["type"] == "create_purchase_order"]
    assert len(purchases) == 1 and float(purchases[0]["quantity"]) == 2
    assert payload["facts"]["manufacturing"]["mode"] == "adjust"
    service = Odoo20BusinessService(adapter)
    service.approve(payload["prepared"], approver_uid=2, comment="reserved 2")
    result = service.apply_plan(service.load_prepared(payload["prepared"].plan_id))
    assert result.succeeded
    assert service.verify_plan(service.load_prepared(payload["prepared"].plan_id)).verified
    lines = adapter.search_read(
        "purchase.order.line", [("product_id", "=", data["component"])],
        ["product_qty", "order_id"], limit=10)
    assert sum(float(line["product_qty"]) for line in lines) == 2
    assert float(adapter.search_read(
        "mrp.production", [("id", "=", int(data["mo"]))], ["qty_produced"],
        limit=1)[0]["qty_produced"]) == 8


def test_same_day_cumulative_capacity_is_rejected() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    suffix = uuid.uuid4().hex[:6]
    vendor = adapter.create("res.partner", {"name": f"Cum vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Cum component {suffix}", "default_code": f"CUM-C-{suffix}",
        "type": "consu", "is_storable": True})
    finished = adapter.create("product.product", {
        "name": f"Cum finished {suffix}", "default_code": f"CUM-F-{suffix}",
        "type": "consu", "is_storable": True})
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template,
        "min_qty": 0, "price": 1.0, "delay": 0, "company_id": 1}], ctx)
    workcenter = models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "create", [{
        "name": f"Cum WC {suffix}", "code": f"CM{suffix[:3]}"}], ctx)
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-CUM-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                 "uom_id": 1})],
        "operation_ids": [(0, 0, {"name": "Hour each", "workcenter_id": workcenter,
                                  "time_cycle_manual": 60})]}], ctx)
    def make_mo(quantity, days):
        when = (datetime.now(UTC) + timedelta(days=days)).strftime(
            "%Y-%m-%d 18:00:00")
        mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
            "product_id": finished, "product_qty": quantity, "bom_id": bom,
            "date_start": when, "date_deadline": when, "company_id": 1}], ctx)
        models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                          [[mo]], ctx)
        return mo
    a = make_mo(6, 1)
    b = make_mo(6, 1)
    c = make_mo(2, 3)
    with pytest.raises(Exception, match="manufacturing_time_infeasible"):
        prepare_manufacturing_plan(Odoo20BusinessService(adapter),
                                   mo_ids=[a, b, c],
                                   tenant_id=f"cum-bad-{suffix}")
    b2 = make_mo(6, 2)
    payload = prepare_manufacturing_plan(Odoo20BusinessService(adapter),
                                         mo_ids=[a, b2, c],
                                         tenant_id=f"cum-ok-{suffix}")
    assert len([action for action in payload["actions"]
                if action["type"] == "create_manufacturing_order"]) == 3


def test_ready_order_does_not_block_another_orders_longer_supply() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    suffix = uuid.uuid4().hex[:6]
    vendor = adapter.create("res.partner", {"name": f"Win vendor {suffix}",
                                            "supplier_rank": 1})
    workcenter = models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "create", [{
        "name": f"Win WC {suffix}", "code": f"WN{suffix[:3]}"}], ctx)
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[workcenter], {"resource_calendar_id": calendar}], ctx)
    products = {}
    components = {}
    for name in ("A", "B"):
        component = adapter.create("product.product", {
            "name": f"Win {name} component {suffix}",
            "default_code": f"WN-{name}-C-{suffix}", "type": "consu",
            "is_storable": True})
        finished = adapter.create("product.product", {
            "name": f"Win {name} finished {suffix}",
            "default_code": f"WN-{name}-F-{suffix}", "type": "consu"})
        template = adapter.search_read("product.product", [("id", "=", finished)],
                                       ["product_tmpl_id"], limit=1)[0][
                                           "product_tmpl_id"][0]
        component_template = adapter.search_read(
            "product.product", [("id", "=", component)],
            ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
        bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
            "code": f"BOM-WN-{name}-{suffix}", "product_tmpl_id": template,
            "product_qty": 1, "type": "normal",
            "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                     "uom_id": 1})],
            "operation_ids": [(0, 0, {"name": "Assembly", "workcenter_id": workcenter,
                                      "time_cycle_manual": 30})]}], ctx)
        products[name] = (finished, bom)
        components[name] = (component, component_template)
    # Only B has a supplier quote; it is three days out.
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": components["B"][1],
        "min_qty": 0, "price": 2.0, "delay": 3, "company_id": 1}], ctx)
    # A's component is already on hand.
    location = models.execute_kw(DB, uid, PASSWORD, "stock.location", "search",
                                 [[["usage", "=", "internal"]]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "stock.quant", "create", [{
        "product_id": components["A"][0], "location_id": location,
        "quantity": 10, "company_id": 1}], ctx)
    def make_mo(name, days):
        when = (datetime.now(UTC) + timedelta(days=days)).strftime(
            "%Y-%m-%d 18:00:00")
        mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
            "product_id": products[name][0], "product_qty": 2,
            "bom_id": products[name][1], "date_start": when,
            "date_deadline": when, "company_id": 1}], ctx)
        models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                          [[mo]], ctx)
        return mo
    ready = make_mo("A", 1)
    waiting = make_mo("B", 10)
    service = Odoo20BusinessService(adapter)
    first = prepare_manufacturing_plan(service, mo_ids=[ready, waiting],
                                       tenant_id=f"win-a-{suffix}")
    second = prepare_manufacturing_plan(service, mo_ids=[waiting, ready],
                                        tenant_id=f"win-b-{suffix}")
    for payload in (first, second):
        purchases = [action for action in payload["actions"]
                     if action["type"] == "create_purchase_order"]
        assert len(purchases) == 1
        assert "WN-B-C-" in purchases[0]["summary"]
        assert float(purchases[0]["quantity"]) == 4
        assert len([action for action in payload["actions"]
                    if action["type"] == "create_manufacturing_order"]) == 2


def test_orders_cannot_consume_capacity_before_their_start_day() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    suffix = uuid.uuid4().hex[:6]
    vendor = adapter.create("res.partner", {"name": f"Start vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Start component {suffix}", "default_code": f"ST-C-{suffix}",
        "type": "consu", "is_storable": True})
    finished = adapter.create("product.product", {
        "name": f"Start finished {suffix}", "default_code": f"ST-F-{suffix}",
        "type": "consu"})
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template,
        "min_qty": 0, "price": 1.0, "delay": 0, "company_id": 1}], ctx)
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    workcenter = models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "create", [{
        "name": f"Start WC {suffix}", "code": f"ST{suffix[:3]}"}], ctx)
    calendar = models.execute_kw(DB, uid, PASSWORD, "resource.calendar", "search",
                                 [[]], {"limit": 1})[0]
    models.execute_kw(DB, uid, PASSWORD, "mrp.workcenter", "write",
                      [[workcenter], {"resource_calendar_id": calendar}], ctx)
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-ST-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                 "uom_id": 1})],
        "operation_ids": [(0, 0, {"name": "Hour each", "workcenter_id": workcenter,
                                  "time_cycle_manual": 60})]}], ctx)
    def make_mo(quantity, days):
        when = (datetime.now(UTC) + timedelta(days=days)).strftime(
            "%Y-%m-%d 18:00:00")
        mo = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
            "product_id": finished, "product_qty": quantity, "bom_id": bom,
            "date_start": when, "date_deadline": when, "company_id": 1}], ctx)
        models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_cancel",
                          [[mo]], ctx)
        return mo
    a = make_mo(2, 1)
    b = make_mo(6, 3)
    c = make_mo(6, 3)
    for ids in ([a, b, c], [c, b, a]):
        with pytest.raises(Exception, match="manufacturing_time_infeasible"):
            prepare_manufacturing_plan(
                Odoo20BusinessService(adapter), mo_ids=ids,
                tenant_id=f"start-{suffix}")


def test_other_orders_reservation_is_not_reallocated() -> None:
    adapter = Odoo20Adapter(Odoo20Config(URL, DB, LOGIN, PASSWORD))
    common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
    uid = common.authenticate(DB, LOGIN, PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
    ctx = {"context": {"allowed_company_ids": [1], "default_company_id": 1}}
    suffix = uuid.uuid4().hex[:6]
    vendor = adapter.create("res.partner", {"name": f"Own vendor {suffix}",
                                            "supplier_rank": 1})
    component = adapter.create("product.product", {
        "name": f"Own component {suffix}", "default_code": f"OWN-C-{suffix}",
        "type": "consu", "is_storable": True})
    finished = adapter.create("product.product", {
        "name": f"Own finished {suffix}", "default_code": f"OWN-F-{suffix}",
        "type": "consu", "is_storable": True})
    component_template = adapter.search_read(
        "product.product", [("id", "=", component)],
        ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "product.supplierinfo", "create", [{
        "partner_id": vendor, "product_tmpl_id": component_template,
        "min_qty": 0, "price": 1.0, "delay": 0, "company_id": 1}], ctx)
    template = adapter.search_read("product.product", [("id", "=", finished)],
                                   ["product_tmpl_id"], limit=1)[0]["product_tmpl_id"][0]
    bom = models.execute_kw(DB, uid, PASSWORD, "mrp.bom", "create", [{
        "code": f"BOM-OWN-{suffix}", "product_tmpl_id": template,
        "product_qty": 1, "type": "normal",
        "bom_line_ids": [(0, 0, {"product_id": component, "product_qty": 2,
                                 "uom_id": 1})]}], ctx)
    due = (datetime.now(UTC) + timedelta(days=4)).strftime("%Y-%m-%d 18:00:00")
    def make_mo():
        return models.execute_kw(DB, uid, PASSWORD, "mrp.production", "create", [{
            "product_id": finished, "product_qty": 2, "bom_id": bom,
            "date_start": due, "date_deadline": due, "company_id": 1}], ctx)
    owner = make_mo()
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_confirm",
                      [[owner]], ctx)
    source = models.execute_kw(DB, uid, PASSWORD, "mrp.production", "read",
                               [[owner]], {"fields": ["location_src_id"]})[0][
                                   "location_src_id"][0]
    models.execute_kw(DB, uid, PASSWORD, "stock.quant", "create", [{
        "product_id": component, "location_id": source, "quantity": 4,
        "company_id": 1}], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_assign",
                      [[owner]], ctx)
    other = make_mo()
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_confirm",
                      [[other]], ctx)
    models.execute_kw(DB, uid, PASSWORD, "mrp.production", "action_assign",
                      [[other]], ctx)
    service = Odoo20BusinessService(adapter)
    def purchase_quantity(payload):
        purchases = [action for action in payload["actions"]
                     if action["type"] == "create_purchase_order"]
        assert len(purchases) == 1
        return float(purchases[0]["quantity"])
    alone = prepare_manufacturing_plan(service, mo_id=int(other),
                                       tenant_id=f"own-a-{suffix}")
    assert purchase_quantity(alone) == 4
    both = prepare_manufacturing_plan(
        service, mo_ids=[int(other), int(owner)], tenant_id=f"own-b-{suffix}")
    assert purchase_quantity(both) == 4
