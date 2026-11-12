"""BOM/workcenter-aware manufacturing recovery for the formal plan chain."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..integrations.odoo20.business import Odoo20BusinessService
from ..integrations.odoo20.procurement import _currency_name
from ..integrations.odoo20.supplier_prices import applicable_supplier_prices
from ..planning.actions import ActionCompileError, compile_actions
from ..planning.multi_bom import (
    BomError,
    BomNode,
    Capacity,
    ComponentSupply,
    MultiBomPlanner,
    Product,
    SupplyStatus,
)


class ManufacturingPlanError(ValueError):
    FACT_CODES = frozenset({
        "workcenter_calendar_unknown", "workorder_schedule_unknown",
        "manufacturing_window_unknown", "bom_not_found",
        "manufacturing_bom_type_unsupported", "manufacturing_bom_output_invalid",
        "manufacturing_bom_uom_unsupported", "manufacturing_bom_fractional",
        "manufacturing_component_unmapped", "manufacturing_component_shortage",
    })
    SYSTEM_CODES = frozenset({"manufacturing_incomplete"})

    def __init__(self, code: str, message: str, *, need: str | None = None,
                 detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.need = need or (
            "fact" if code in self.FACT_CODES
            else "system" if code in self.SYSTEM_CODES else "failure")


def _product(adapter, product_id: int) -> dict:
    rows = adapter.search_read(
        "product.product", [("id", "=", int(product_id))],
        ["id", "name", "default_code", "product_tmpl_id", "uom_id", "type"],
        limit=1)
    if not rows:
        raise ManufacturingPlanError("product_not_found", "Product not found.")
    return rows[0]


def _bom_for_template(adapter, template_id: int) -> dict | None:
    rows = adapter.search_read(
        "mrp.bom", [("product_tmpl_id", "=", int(template_id)), ("active", "=", True)],
        ["id", "code", "product_qty", "type", "product_tmpl_id"], limit=1,
        order="id")
    return rows[0] if rows else None


def _bom_row(adapter, bom_id: int) -> dict | None:
    rows = adapter.search_read(
        "mrp.bom", [("id", "=", int(bom_id))],
        ["id", "code", "product_qty", "type", "product_tmpl_id"], limit=1)
    return rows[0] if rows else None


def _bom_tree(adapter, product: dict, *, bom_id: int | None = None,
              seen: frozenset[int] = frozenset()) -> BomNode:
    code = str(product.get("default_code") or product["id"])
    template = (product.get("product_tmpl_id") or [0])[0]
    if bom_id is None:
        if not template or template in seen:
            return BomNode(code)
        bom = _bom_for_template(adapter, int(template))
    else:
        bom = _bom_row(adapter, int(bom_id))
    if bom is None:
        if bom_id is not None:
            raise ManufacturingPlanError("bom_not_found", "The MO BOM no longer exists.")
        return BomNode(code)
    if str(bom.get("type") or "normal") != "normal":
        raise ManufacturingPlanError(
            "manufacturing_bom_type_unsupported",
            "Only normal BoMs can be expanded for recovery.")
    output = float(bom.get("product_qty") or 0)
    if output <= 0:
        raise ManufacturingPlanError("manufacturing_bom_output_invalid",
                                     "The BoM output quantity must be positive.")
    lines = adapter.search_read(
        "mrp.bom.line", [("bom_id", "=", int(bom["id"]))],
        ["product_id", "product_qty", "uom_id"], limit=200)
    children = []
    for line in lines:
        component = _product(adapter, int((line.get("product_id") or [0])[0]))
        component_uom = (component.get("uom_id") or [0])[0]
        line_uom = (line.get("uom_id") or [0])[0]
        if component_uom and line_uom and int(component_uom) != int(line_uom):
            raise ManufacturingPlanError(
                "manufacturing_bom_uom_unsupported",
                f"BoM line {line.get('id')} uses a different unit than its component.")
        per_parent = float(line.get("product_qty") or 0) / output
        rounded = int(round(per_parent))
        if abs(per_parent - rounded) > 1e-9:
            raise ManufacturingPlanError(
                "manufacturing_bom_fractional",
                "The BoM consumption cannot be expressed as an integer per unit.")
        child = _bom_tree(adapter, component,
                          seen=seen | ({int(template)} if template else set()))
        children.append(BomNode(
            code=str(component.get("default_code") or component["id"]),
            per_parent=max(0, rounded), children=child.children))
    return BomNode(code=code, children=tuple(children))


def _component_product(adapter, code: str) -> dict | None:
    rows = adapter.search_read(
        "product.product", [("default_code", "=", str(code))],
        ["id", "default_code", "name", "product_tmpl_id", "uom_id"], limit=1)
    if rows:
        return rows[0]
    if str(code).isdigit():
        rows = adapter.search_read(
            "product.product", [("id", "=", int(code))],
            ["id", "default_code", "name", "product_tmpl_id", "uom_id"], limit=1)
        if rows:
            return rows[0]
    return None


def _stock(adapter, product_id: int, company_id: int = 0) -> int:
    rows = adapter.search_read(
        "stock.quant", [("product_id", "=", int(product_id))],
        ["quantity", "reserved_quantity", "location_id", "company_id"], limit=500)
    location_ids = {(row.get("location_id") or [0])[0] for row in rows}
    location_ids.discard(0)
    usages = {}
    if location_ids:
        for location in adapter.search_read(
                "stock.location", [("id", "in", sorted(location_ids))],
                ["id", "usage"], limit=500):
            usages[int(location["id"])] = str(location.get("usage") or "")
    total = 0
    for row in rows:
        if usages.get(int((row.get("location_id") or [0])[0])) != "internal":
            continue
        row_company = int((row.get("company_id") or [0])[0])
        if company_id and row_company and row_company != int(company_id):
            continue
        total += max(0, int(float(row.get("quantity") or 0)
                           - float(row.get("reserved_quantity") or 0)))
    return total


def _reserved_components(adapter, mos: list[dict], company_id: int) -> dict[int, dict[str, int]]:
    """Actual move-line reservations, kept per manufacturing order."""
    by_mo: dict[int, dict[str, int]] = {}
    for mo in mos:
        mo_id = int(mo["id"])
        move_ids = [int(move_id) for move_id in (mo.get("move_raw_ids") or [])]
        if not move_ids:
            continue
        lines = adapter.search_read(
            "stock.move.line", [("move_id", "in", move_ids)],
            ["product_id", "quantity", "state"], limit=500)
        for line in lines:
            if str(line.get("state")) in {"done", "cancel"}:
                continue
            quantity = float(line.get("quantity") or 0)
            if quantity <= 0:
                continue
            component = _product(adapter, int((line.get("product_id") or [0])[0]))
            code = str(component.get("default_code") or component["id"])
            bucket = by_mo.setdefault(mo_id, {})
            bucket[code] = bucket.get(code, 0) + int(round(quantity))
    return by_mo


def _offer(adapter, product: dict, company_id: int, currency: str,
           *, max_lead_days: int | None = None,
           prefer_lead: bool = False, as_of: date | None = None) -> dict | None:
    rows = applicable_supplier_prices(
        adapter, product, company_id, as_of=as_of or datetime.now(UTC).date(), limit=50)
    best = None
    for row in rows:
        partner = (row.get("partner_id") or [0])[0]
        price = float(row.get("price") or 0)
        if partner <= 0 or price <= 0:
            continue
        row_company = (row.get("company_id") or [0])[0]
        if row_company and company_id and int(row_company) != int(company_id):
            continue
        offer_currency = _currency_name(
            adapter, {"currency_id": row.get("currency_id")}, company_id)
        if currency and offer_currency and offer_currency != currency:
            continue
        lead = int(row.get("delay") or 0)
        if max_lead_days is not None and lead > max_lead_days:
            continue
        candidate = {"partner_id": int(partner), "price": price,
                     "min_qty": int(row.get("min_qty") or 0), "lead": lead}
        if best is None:
            best = candidate
        elif prefer_lead:
            if (candidate["lead"], candidate["price"]) < (best["lead"], best["price"]):
                best = candidate
        elif price < best["price"]:
            best = candidate
    return best


def _moment(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _workcenter_data(adapter, bom_id: int, *, start: date, end: date,
                     exclude_mo_ids: set[int]) -> tuple[dict[str, float], list[Capacity]]:
    rows = adapter.search_read(
        "mrp.routing.workcenter", [("bom_id", "=", int(bom_id))],
        ["workcenter_id", "time_cycle", "time_cycle_manual"], limit=100)
    hours: dict[str, float] = {}
    centers: set[int] = set()
    for row in rows:
        wc = int((row.get("workcenter_id") or [0])[0])
        if wc <= 0:
            continue
        minutes = row.get("time_cycle_manual") or row.get("time_cycle") or 0
        hours[str(wc)] = hours.get(str(wc), 0.0) + max(0.0, float(minutes)) / 60.0
        centers.add(wc)
    capacities = []
    for wc in sorted(centers):
        data = adapter.search_read(
            "mrp.workcenter", [("id", "=", wc)],
            ["id", "name", "resource_calendar_id"], limit=1)
        calendar = (data[0].get("resource_calendar_id") or [0])[0] if data else 0
        if not calendar:
            raise ManufacturingPlanError(
                "workcenter_calendar_unknown",
                f"Workcenter {wc} has no resource calendar; capacity is unknown.")
        attendance = adapter.search_read(
            "resource.calendar.attendance", [("calendar_id", "=", int(calendar))],
            ["dayofweek", "duration_hours", "hour_from", "hour_to"], limit=200)
        leaves = adapter.search_read(
            "resource.calendar.leaves", [("calendar_id", "=", int(calendar))],
            ["date_from", "date_to", "resource_id"], limit=200)
        by_day: dict[str, float] = {}
        for row in attendance:
            duration = row.get("duration_hours")
            if not isinstance(duration, (int, float)):
                duration = max(0.0, float(row.get("hour_to") or 0)
                               - float(row.get("hour_from") or 0))
            by_day[str(row.get("dayofweek"))] = (
                by_day.get(str(row.get("dayofweek")), 0.0) + max(0.0, float(duration)))
        unavailable_days = set()
        for leave in leaves:
            leave_start = _as_date(leave.get("date_from"))
            leave_end = _as_date(leave.get("date_to"))
            if leave_start is None or leave_end is None:
                continue
            day = max(start, leave_start)
            while day <= min(end, leave_end):
                unavailable_days.add(day)
                day += timedelta(days=1)
        total = 0.0
        day = start
        while day <= end:
            if day not in unavailable_days:
                total += by_day.get(str(day.weekday()), 0.0)
            day += timedelta(days=1)
        workorders = adapter.search_read(
            "mrp.workorder", [("workcenter_id", "=", wc)],
            ["production_id", "duration_expected", "state", "date_start",
             "date_finished"], limit=500)
        used = 0.0
        window_start = datetime.combine(start, datetime.min.time())
        window_end = datetime.combine(end, datetime.max.time())
        for order in workorders:
            if str(order.get("state")) in {"done", "cancel"}:
                continue
            production = int((order.get("production_id") or [0])[0])
            if production in exclude_mo_ids:
                continue
            order_start = _moment(order.get("date_start"))
            order_end = _moment(order.get("date_finished"))
            if order_start is None or order_end is None or order_end <= order_start:
                raise ManufacturingPlanError(
                    "workorder_schedule_unknown",
                    f"Workorder on center {wc} has no usable schedule; capacity is unknown.")
            overlap_start = max(order_start, window_start)
            overlap_end = min(order_end, window_end)
            if overlap_end <= overlap_start:
                continue
            fraction = ((overlap_end - overlap_start).total_seconds()
                        / (order_end - order_start).total_seconds())
            used += max(0.0, float(order.get("duration_expected") or 0)) / 60.0 * fraction
        capacities.append(Capacity(str(wc), max(0.0, total - used)))
    return hours, capacities


def _remaining_quantity(mo: dict) -> int:
    return max(0, int(round(float(mo.get("product_qty") or 0)
                           - float(mo.get("qty_produced") or 0))))


def _component_requirements(tree: BomNode, demand: int,
                            multiplier: int = 1) -> dict[str, int]:
    result: dict[str, int] = {}
    for child in tree.children:
        amount = multiplier * child.per_parent * demand
        result[child.code] = result.get(child.code, 0) + amount
        nested = _component_requirements(child, 1, amount)
        for code, quantity in nested.items():
            result[code] = result.get(code, 0) + quantity
    return result


def _check_cumulative_capacity(adapter, entries: dict[str, dict], *,
                               exclude_mo_ids: set[int]) -> None:
    """EDF check: daily shared-center capacity, not just per-order totals."""
    from collections import defaultdict

    jobs: dict[str, list[tuple[date, date, float, str]]] = defaultdict(list)
    bom_for_resource: dict[str, int] = {}
    for code, entry in entries.items():
        for detail in entry["details"]:
            for resource, per_unit in entry["hours"].items():
                jobs[resource].append((detail["start"], detail["due"],
                                       per_unit * detail["demand"], code))
                bom_for_resource[resource] = entry["bom_id"]
    for resource, resource_jobs in jobs.items():
        first = min(job[0] for job in resource_jobs)
        last = max(job[1] for job in resource_jobs)
        day = first
        remaining = [{"start": job[0], "deadline": job[1], "hours": job[2],
                      "code": job[3]} for job in resource_jobs]
        while day <= last:
            _, daily = _workcenter_data(
                adapter, bom_for_resource[resource], start=day, end=day,
                exclude_mo_ids=exclude_mo_ids)
            capacity = next((cap.hours for cap in daily
                             if cap.resource == resource), 0.0)
            active = sorted((job for job in remaining
                             if job["hours"] > 0 and job["start"] <= day
                             and job["deadline"] >= day),
                            key=lambda job: job["deadline"])
            for job in active:
                if capacity <= 0:
                    break
                used = min(capacity, job["hours"])
                job["hours"] -= used
                capacity -= used
            day += timedelta(days=1)
            overdue = [job for job in remaining if job["hours"] > 1e-9
                       and job["deadline"] < day]
            if overdue:
                raise ManufacturingPlanError(
                    "manufacturing_time_infeasible",
                    f"Cumulative load exceeds center {resource} by "
                    f"{overdue[0]['deadline'].isoformat()}.",
                    detail={"workcenter": resource,
                            "blocked_code": overdue[0]["code"],
                            "deadline": overdue[0]["deadline"].isoformat()})


def prepare_manufacturing_plan(business: Odoo20BusinessService, *, mo_id: int | None = None,
                               mo_ids: list[int] | None = None,
                               tenant_id: str, name: str | None = None,
                               time_limit_seconds: float = 10.0) -> dict[str, Any]:
    """Plan replacement or adjustment actions with real capacity and lead times."""
    adapter = business.adapter
    ids = [int(value) for value in (mo_ids or ([mo_id] if mo_id is not None else []))]
    if not ids:
        raise ManufacturingPlanError("manufacturing_order_required",
                                     "At least one manufacturing order is required.")
    allowed_states = {"cancel", "confirmed", "progress", "planned", "to_close"}
    mos: list[dict] = []
    for identifier in ids:
        rows = adapter.search_read(
            "mrp.production", [("id", "=", identifier)],
            ["id", "name", "state", "product_id", "product_qty", "qty_produced",
             "bom_id", "date_start", "date_finished", "date_deadline",
             "company_id", "origin", "move_raw_ids"], limit=1)
        if not rows:
            raise ManufacturingPlanError("manufacturing_order_not_found",
                                         "Manufacturing order not found.")
        mo = rows[0]
        if str(mo.get("state")) not in allowed_states:
            raise ManufacturingPlanError(
                "manufacturing_order_state_unsupported",
                f"Manufacturing order state {mo.get('state')} cannot be recovered.")
        if _as_date(mo.get("date_start")) is None and _as_date(
                mo.get("date_finished")) is None:
            raise ManufacturingPlanError(
                "manufacturing_window_unknown",
                "The manufacturing order has no planned date; capacity is unknown.")
        mos.append(mo)
    modes = {"replace" if str(mo.get("state")) == "cancel" else "adjust" for mo in mos}
    if len(modes) != 1:
        raise ManufacturingPlanError(
            "manufacturing_mixed_states_unsupported",
            "Cancelled and active manufacturing orders need separate plans.")
    mode = next(iter(modes))
    today = datetime.now(UTC).date()
    def deadline(mo):
        return (_as_date(mo.get("date_deadline"))
                or _as_date(mo.get("date_finished"))
                or _as_date(mo.get("date_start")))

    dues = [deadline(mo) for mo in mos]
    products: list[Product] = []
    entries: dict[str, dict[str, Any]] = {}
    capacities: dict[str, Capacity] = {}
    for mo in mos:
        finished = _product(adapter, int((mo.get("product_id") or [0])[0]))
        code = str(finished.get("default_code") or finished["id"])
        demand = _remaining_quantity(mo)
        if demand <= 0:
            raise ManufacturingPlanError(
                "manufacturing_nothing_remaining",
                "The manufacturing order has no remaining quantity to recover.")
        bom_id = int((mo.get("bom_id") or [0])[0])
        if not bom_id:
            bom = _bom_for_template(
                adapter, int((finished.get("product_tmpl_id") or [0])[0]))
            bom_id = int(bom["id"]) if bom else 0
        if not bom_id:
            raise ManufacturingPlanError("bom_not_found",
                                         "No BOM for the finished product.")
        detail = {"mo": mo, "demand": demand,
                  "start": _as_date(mo.get("date_start")),
                  "due": (_as_date(mo.get("date_deadline"))
                          or _as_date(mo.get("date_finished"))
                          or _as_date(mo.get("date_start")))}
        if code in entries:
            if int(entries[code]["bom_id"]) != bom_id:
                raise ManufacturingPlanError(
                    "manufacturing_mixed_bom_unsupported",
                    "The same product uses different BOMs in one recovery plan.")
            entries[code]["details"].append(detail)
            entries[code]["demand"] += demand
            continue
        tree = _bom_tree(adapter, finished, bom_id=bom_id)
        entries[code] = {"demand": demand, "details": [detail],
                         "finished": finished, "bom_id": bom_id,
                         "tree": tree, "hours": {}}
    for code, entry in entries.items():
        products.append(Product(code=code, demand=entry["demand"],
                                bom=entry["tree"],
                                resource_hours=entry["hours"]))
    requirements: dict[str, int] = {}
    for entry in entries.values():
        for code, quantity in _component_requirements(entry["tree"],
                                                      entry["demand"]).items():
            requirements[code] = requirements.get(code, 0) + quantity
    company_id = int((mos[0].get("company_id") or [0])[0]) or 0
    currency = _currency_name(adapter, {"currency_id": None}, company_id)
    reserved_by_mo = (_reserved_components(adapter, mos, company_id)
                      if mode == "adjust" else {})
    component_needs: dict[str, list[tuple[dict, int]]] = {}
    for entry in entries.values():
        for detail in entry["details"]:
            for code, quantity in _component_requirements(
                    entry["tree"], detail["demand"]).items():
                component_needs.setdefault(code, []).append((detail, quantity))
    selected_offers: dict[str, dict] = {}
    availability: dict[str, dict[str, int]] = {}
    for component_code, needs in component_needs.items():
        component = _component_product(adapter, component_code)
        if component is None:
            raise ManufacturingPlanError(
                "manufacturing_component_unmapped",
                f"Component {component_code} cannot be mapped to a product.")
        free_stock = _stock(adapter, int(component["id"]), company_id)
        total_need = sum(quantity for _, quantity in needs)
        pool = free_stock
        reserved_used = 0
        shortages: list[tuple[dict, int]] = []
        for detail, quantity in sorted(needs, key=lambda item: item[0]["due"]):
            mo_id = int(detail["mo"]["id"])
            own = reserved_by_mo.get(mo_id, {}).get(component_code, 0)
            own_used = min(own, quantity)
            reserved_used += own_used
            remaining = quantity - own_used
            from_stock = min(pool, remaining)
            pool -= from_stock
            shortage = remaining - from_stock
            detail.setdefault("shortages", {})[component_code] = shortage
            if shortage > 0:
                shortages.append((detail, shortage))
        total_shortage = sum(quantity for _, quantity in shortages)
        offer = None
        if total_shortage > 0:
            # Only the orders that actually need supplier material constrain the
            # lead time. A ready-material order must not block another order's
            # longer but valid supply window.
            window = min((detail["due"] - today).days
                         for detail, _ in shortages)
            offer = _offer(adapter, component, company_id, currency,
                           max_lead_days=max(0, window), as_of=today)
            if offer is None:
                any_offer = _offer(adapter, component, company_id, currency, as_of=today)
                raise ManufacturingPlanError(
                    "manufacturing_component_late" if any_offer is not None
                    else "manufacturing_component_shortage",
                    f"No timely supplier can cover component {component_code}.",
                    detail={"component_code": component_code,
                            "shortage": total_shortage,
                            "lead_days": any_offer["lead"] if any_offer else None})
            selected_offers[component_code] = offer
        availability[component_code] = {
            "need": total_need, "stock": free_stock, "reserved": reserved_used,
            "available": free_stock + reserved_used,
            "shortage": total_shortage}

    union_start = None
    union_end = None
    for code, entry in entries.items():
        for detail in entry["details"]:
            due = detail["due"]
            start = detail["start"] or due
            arrival = 0
            for component_code in _component_requirements(
                    entry["tree"], detail["demand"]):
                if detail.get("shortages", {}).get(component_code, 0) <= 0:
                    continue
                offer = selected_offers[component_code]
                if offer["lead"] > (due - today).days:
                    raise ManufacturingPlanError(
                        "manufacturing_component_late",
                        f"Component {component_code} arrives in {offer['lead']} days, "
                        f"after the {due.isoformat()} deadline.",
                        detail={"component_code": component_code,
                                "lead_days": offer["lead"], "due": due.isoformat()})
                arrival = max(arrival, int(offer["lead"]))
            planned_start = max(start, today + timedelta(days=arrival))
            if planned_start > due:
                raise ManufacturingPlanError(
                    "manufacturing_time_infeasible",
                    f"Material for {code} arrives after its deadline {due.isoformat()}.")
            detail["start"] = planned_start
            union_start = planned_start if union_start is None else min(union_start, planned_start)
            union_end = due if union_end is None else max(union_end, due)
    for product in products:
        entry = entries[product.code]
        hours, workcenter_capacities = _workcenter_data(
            adapter, entry["bom_id"], start=union_start, end=union_end,
            exclude_mo_ids=set(ids))
        entry["hours"] = hours
        for capacity in workcenter_capacities:
            existing = capacities.get(capacity.resource)
            if existing is None or capacity.hours > existing.hours:
                capacities[capacity.resource] = capacity
            # Each MO must fit its own window; a later deadline cannot lend
            # capacity to an earlier one.
            for detail in entry["details"]:
                _, detail_caps = _workcenter_data(
                    adapter, entry["bom_id"], start=detail["start"],
                    end=detail["due"], exclude_mo_ids=set(ids))
                for own in detail_caps:
                    if own.resource != capacity.resource:
                        continue
                    required = hours.get(own.resource, 0.0) * detail["demand"]
                    if own.hours + 1e-9 < required:
                        raise ManufacturingPlanError(
                            "manufacturing_time_infeasible",
                            f"Only {own.hours:.2f}h remain on {own.resource} for "
                            f"{detail['mo'].get('name')} before "
                            f"{detail['due'].isoformat()}; {required:.2f}h required.",
                            detail={"workcenter": own.resource,
                                    "available_hours": own.hours,
                                    "required_hours": required,
                                    "mo_id": detail["mo"]["id"]})
    _check_cumulative_capacity(adapter, entries, exclude_mo_ids=set(ids))
    products = [Product(code=product.code, demand=product.demand,
                        bom=product.bom,
                        resource_hours=entries[product.code]["hours"])
                for product in products]
    supplies: dict[str, ComponentSupply] = {}
    for component_code, facts in availability.items():
        offer = selected_offers.get(component_code)
        available = facts["available"]
        if facts["shortage"]:
            available += facts["shortage"]
        supplies[component_code] = ComponentSupply(
            component_code, available=available,
            status=SupplyStatus.LIMITED if offer or not facts["shortage"]
            else SupplyStatus.UNKNOWN, on_hand=facts["stock"])
    offers = selected_offers
    try:
        plan = MultiBomPlanner(products, list(capacities.values()), supplies,
                               time_limit_seconds=time_limit_seconds).solve()
    except BomError as error:
        if error.code == "plan_unknown":
            raise ManufacturingPlanError(
                "manufacturing_incomplete",
                "The solver timed out before proving feasibility; retry later.") from None
        raise ManufacturingPlanError(
            "manufacturing_unsolved", str(error), need="failure") from None
    missing = [product.code for product in products
               if int(plan.produced.get(product.code, 0)) < product.demand]
    if not plan.feasible or missing:
        raise ManufacturingPlanError(
            "manufacturing_shortfall",
            f"The workcenter/component plan cannot produce {missing}.",
            detail={"produced": plan.produced, "shortfall": plan.shortfall,
                    "component_need": plan.component_need,
                    "resource_use": plan.resource_use})
    operations = []
    for product in products:
        entry = entries[product.code]
        produced_total = int(plan.produced[product.code])
        if mode == "replace":
            remaining = produced_total
            for index, detail in enumerate(entry["details"]):
                if index == len(entry["details"]) - 1:
                    quantity = remaining
                else:
                    quantity = int(round(
                        produced_total * detail["demand"] / entry["demand"]))
                    quantity = min(quantity, remaining)
                remaining -= quantity
                if quantity <= 0:
                    continue
                operations.append({
                    "operation_id": f"make-{product.code}-{detail['mo']['id']}",
                    "kind": "manufacturing_order", "action": "create",
                    "target": {}, "before": {},
                    "after": {"quantity": quantity,
                              "product_id": int(entry["finished"]["id"]),
                              "product_code": product.code,
                              "bom_id": entry["bom_id"],
                              "workcenter_id": (int(next(iter(entry["hours"])))
                                                if entry["hours"] else None),
                              "date_start": detail["start"].isoformat() + " 09:00:00",
                              "date_deadline": detail["due"].isoformat() + " 09:00:00",
                              "origin_refs": [{"model": "mrp.production",
                                               "id": int(detail["mo"]["id"])}]},
                })
        else:
            for detail in entry["details"]:
                mo = detail["mo"]
                operations.append({
                    "operation_id": f"adjust-{mo['id']}",
                    "kind": "manufacturing_order", "action": "update",
                    "target": {"id": int(mo["id"]),
                               "name": str(mo.get("name") or mo["id"])},
                    "before": {"quantity": float(mo.get("product_qty") or 0)},
                    "after": {"quantity": int(round(float(
                        mo.get("product_qty") or 0))),
                        "date_start": detail["start"].isoformat() + " 09:00:00",
                        "date_deadline": detail["due"].isoformat() + " 09:00:00"},
                })
    procurement = []
    for component_code, need in plan.component_need.items():
        component = _component_product(adapter, component_code)
        if component is None:
            raise ManufacturingPlanError(
                "manufacturing_component_unmapped",
                f"Component {component_code} cannot be mapped to a product.")
        shortage = availability[component_code]["shortage"]
        if shortage <= 0:
            continue
        offer = offers.get(component_code)
        if offer is None:
            raise ManufacturingPlanError(
                "manufacturing_component_unmapped",
                f"No confirmed timely supply for component {component_code}.",
                detail={"component_code": component_code, "shortage": shortage})
        quantity = max(shortage, int(offer["min_qty"]))
        operations.append({
            "operation_id": f"buy-{component_code}", "kind": "purchase_order",
            "action": "create", "target": {}, "before": {},
            "after": {"quantity": quantity, "product_id": int(component["id"]),
                      "product_code": component_code,
                      "partner_id": int(offer["partner_id"]),
                      "unit_price": float(offer["price"]),
                      "date_planned": (today + timedelta(
                          days=int(offer["lead"]))).isoformat() + " 09:00:00",
                      "origin_refs": [{"model": "mrp.production",
                                       "id": int(mos[0]["id"])}]},
        })
        procurement.append({"component_code": component_code,
                            "quantity": quantity, "shortage": shortage,
                            "unit_price": float(offer["price"]),
                            "cost": round(quantity * float(offer["price"]), 4),
                            "lead_days": int(offer["lead"]),
                            "min_order_quantity": int(offer["min_qty"])})
    changes = {"semantics": "target_state_to_change_set", "operations": operations,
               "projected_state": {"checks": {"demand_covered": True,
                                              "supply_known": True}}}
    try:
        compiled = compile_actions(
            changes, plan={"trace": {"solve_calls": [{"status": plan.status.value}]},
                           "assumptions": []},
            currency=currency, unit="Units")
    except ActionCompileError as error:
        raise ManufacturingPlanError(
            "manufacturing_compile_failed", f"actions: {error.code}") from None
    exception_id = adapter.create("forgeflow.exception_event", {
        "name": "Recover manufacturing orders " + ", ".join(
            str(mo.get("name") or mo["id"]) for mo in mos),
        "event_type": "production_disruption",
        "product_id": int(entries[products[0].code]["finished"]["id"]),
        "occurred_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "description": "Recovery for " + ", ".join(
            str(mo.get("name") or mo["id"]) for mo in mos),
        "company_id": company_id or False,
    })
    facts = {"mos": mos, "bom_ids": sorted({entry["bom_id"]
                                            for entry in entries.values()}),
             "products": [entry["finished"] for entry in entries.values()],
             "manufacturing": {"mode": mode, "produced": plan.produced,
                               "component_need": plan.component_need,
                               "shortfall": plan.shortfall,
                               "resource_use": plan.resource_use,
                               "workcenters": sorted(capacities),
                               "capacity_hours": {key: capacity.hours
                                                  for key, capacity in capacities.items()},
                               "procurement": procurement,
                               "window_days": max(1, (max(dues) - today).days + 1)},
             "affected_targets": []}
    prepared = business.prepare_recovery(
        exception_id, compiled["actions"], facts,
        tenant_ref=f"{tenant_id}:company:{company_id}",
        name=name or "Recovery for " + ", ".join(
            str(mo.get("name") or mo["id"]) for mo in mos),
        expires_in_hours=72)
    return {"prepared": prepared, "actions": compiled["actions"], "plan": plan,
            "mos": mos, "facts": facts}
