"""Pure target-state to live commitment change-set calculation.

Inputs are explicit live records, a validated request and a freshly computed plan.
No filesystem, RPC, model, reference solution, seed or verifier access occurs.
This bounded proposal requires fresh execution preflight and external acceptance.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta
from decimal import Decimal

from forgeflow.planning.objectives import objective_matches
from forgeflow.recovery.margin import (
    MARGIN_BASIS,
    decimal_amount,
    decimal_text,
    margin_ratio,
    margin_satisfied,
)
from forgeflow.recovery.product_constraints import check_product_margin, constrained_blueprint

CHANGE_SET_REASONS = frozenset({
    "change_set_binding_mismatch", "change_set_supply_quantity_mismatch",
    "change_set_existing_offer_ambiguous", "change_set_mapping_unproven",
    "change_set_origin_capacity_mismatch",
})


class ChangeSetError(ValueError):
    """Keep diagnostic prose private; expose only an explicitly assigned fixed code."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code if isinstance(code, str) and code in CHANGE_SET_REASONS else None


def need(condition, message, *, code=None):
    if not condition:
        raise ChangeSetError(message, code=code)


def integer(value, label, minimum=0):
    need(
        type(value) in (int, float) and value >= minimum and value == int(value),
        f"{label}: nonnegative integer required",
    )
    return int(value)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def rid(value):
    return value[0] if isinstance(value, list) else value


def offer_key(row):
    return (
        f"{row['ref']}|{row['product_code']}|{row['min_qty']}|"
        f"{round(row['price_dollars'], 2)}|{row['lead_time']}"
    )


def _origin_capacity_feasible(pos, mos, component_need):
    """Check declared PO coverage jointly, without double-counting MO demand.

    A minimum-order remainder can exceed its listed consumers' total need.
    Otherwise the whole purchase quantity must fit those consumers. Every
    declared edge carries at least one unit; these are integral business units.
    This independent residual-flow check consumes only the proposed records.
    """
    def positive_integer(value):
        return (type(value) in (int, float) and math.isfinite(value)
                and value > 0 and value == int(value))

    if any(not positive_integer(value) for value in component_need.values()):
        return False
    if any(not positive_integer(row["quantity"]) for row in pos + mos):
        return False
    for sku, per in component_need.items():
        purchases = [po for po in pos if po["supply_role"] == "component" and po["product_code"] == sku]
        demand = {mo["target"]["token"]: mo["quantity"] * per for mo in mos}
        required, edges = {}, {}
        for index, po in enumerate(purchases):
            linked = {ref["token"] for ref in po["origin_refs"]}
            if not linked or len(linked) != len(po["origin_refs"]) or not linked <= demand.keys():
                return False
            required[index] = min(po["quantity"], sum(demand[token] for token in linked))
            edges[index] = linked
        remaining = dict(demand)
        for index, linked in edges.items():
            required[index] -= len(linked)
            for token in linked:
                remaining[token] -= 1
        if any(value < 0 for value in (*required.values(), *remaining.values())):
            return False
        residual = defaultdict(dict)

        def add(left, right, capacity):
            residual[left][right] = capacity
            residual[right][left] = 0

        for index, quantity in required.items():
            add("source", ("po", index), quantity)
            for token in edges[index]:
                add(("po", index), ("mo", token), quantity)
        for token, quantity in remaining.items():
            add(("mo", token), "sink", quantity)
        owed = sum(required.values())
        while owed:
            previous = {"source": None}
            queue = deque(["source"])
            while queue and "sink" not in previous:
                node = queue.popleft()
                for target_node, capacity in residual[node].items():
                    if capacity > 0 and target_node not in previous:
                        previous[target_node] = node
                        queue.append(target_node)
            if "sink" not in previous:
                return False
            quantity, node = owed, "sink"
            while previous[node] is not None:
                quantity = min(quantity, residual[previous[node]][node])
                node = previous[node]
            node = "sink"
            while previous[node] is not None:
                source = previous[node]
                residual[source][node] -= quantity
                residual[node][source] += quantity
                node = source
            owed -= quantity
    return True


def day(value, anchor):
    return (datetime.fromisoformat(value).date() - anchor).days


def target(model, row=None, serial=None):
    return {
        "token": f"existing:{model}:{row['id']}" if row else f"new:{model}:{serial:04d}",
        "model": model,
        "id": row["id"] if row else None,
        "name": row["name"] if row else None,
    }


def build_change_set(request, snapshot, blueprint, fresh_plan, *, product_constraints=None, product_objective=None):
    """Return a proposal, rejecting unsupported/ambiguous inputs; never execute it."""
    # Re-derive all public facts/commitments from records. This catches stale or
    # unrelated blueprints without importing any pinned upstream source.
    rebuilt = constrained_blueprint(snapshot, request, product_constraints)
    need(rebuilt == blueprint, "Blueprint differs from supplied live snapshot/request",
         code="change_set_binding_mismatch")
    need(
        fresh_plan.get("blueprint_sha256") == canonical_hash(blueprint),
        "Fresh plan is not bound to this exact blueprint",
        code="change_set_binding_mismatch",
    )
    need(fresh_plan.get("odoo_writes") is False, "Only read-only fresh solver output accepted")
    p = fresh_plan["plan"]
    need(objective_matches(fresh_plan, product_objective), "Plan objective binding differs",
         code="change_set_binding_mismatch")
    manufactured = blueprint["manufactured_products"]
    code = request["target_product_code"]
    need(
        len(manufactured) == 1 and manufactured[0]["product"]["code"] == code,
        "Prototype supports only one manufactured target and one-level BoM",
    )
    manufacture = manufactured[0]
    components = {
        c["product"]["code"]: integer(c["per_unit"], "BoM per-unit", 1)
        for c in manufacture["components"]
    }
    need(code not in components, "Cyclic BoM")
    choices = {c["workcenter_code"]: c for c in manufacture["workcenter_choices"]}
    caps = {c["code"]: Decimal(str(c["capacity_minutes"])) for c in blueprint["workcenters"]}
    models = snapshot["models"]
    products = {x["default_code"]: x for x in models["product.product"]}
    centers = {x["code"]: x for x in models["mrp.workcenter"]}
    sos = {x["name"]: x for x in models["sale.order"] if x["id"] in request["sales_order_ids"]}
    company_id = rid(next(iter(sos.values()))["company_id"])
    currency_id = rid(next(iter(sos.values()))["currency_id"])
    customers = {x["ref"]: x for x in blueprint["customers"]}
    so_refs = {ref: target("sale.order", sos[c["task_order_ref"]]) for ref, c in customers.items()}
    partners = {x["ref"]: x for x in models["res.partner"] if x["ref"]}
    anchor = date.fromisoformat(request["anchor_date"])

    def dates(n):
        return (anchor + timedelta(days=n)).isoformat() + " 00:00:00"

    repair = blueprint["repair_context"]
    broken_mos = set(repair["broken_manufacturing_order_refs"])
    broken_pos = set(repair["broken_purchase_order_refs"])
    old_mos = {
        x["name"]: x for x in models["mrp.production"] if x["state"] not in ("done", "cancel")
    }
    old_pos = {x["name"]: x for x in models["purchase.order"] if x["state"] != "cancel"}
    live_mos = repair["seeded_manufacturing_orders"]
    live_pos = repair["seeded_purchase_orders"]
    need(set(old_mos) == {x["ref"] for x in live_mos}, "Unexpected existing MO outside scope")
    need(set(old_pos) == {x["ref"] for x in live_pos}, "Unexpected existing PO outside scope")
    offers = {offer_key(v): v for v in blueprint["vendors"]}
    buckets = p["manufacturing_orders"]
    purchases = p["purchase_orders"]
    allocations = {x["ref"]: x for x in p["allocations"]}
    need(
        len(allocations) == len(p["allocations"]) and set(allocations) == set(customers),
        "Plan customer set mismatch",
    )
    all_refs = [x["plan_ref"] for x in buckets + purchases]
    need(len(all_refs) == len(set(all_refs)), "Duplicate target plan_ref")
    stock = {k: integer(v, "Stock quantity") for k, v in blueprint["product_stock_units"].items()}
    stock_used = {x["code"]: integer(x["used"], "Stock used") for x in p["stock_report"]}
    # The pinned solver omits zero-stock components from its report. Their
    # usage is necessarily zero; do not infer this for a missing positive stock.
    need(len(stock_used) == len(p["stock_report"]) and set(stock_used) <= set(stock)
         and all(stock[key] == 0 for key in stock.keys() - stock_used.keys()),
         "Stock report scope mismatch")
    stock_used.update({key: 0 for key in stock.keys() - stock_used.keys()})
    for key in stock:
        need(stock_used[key] <= stock[key], "Plan overallocates current stock")
    for ref, alloc in allocations.items():
        customer = customers[ref]
        need(
            alloc["accepted"] is True and alloc["task_order_ref"] == customer["task_order_ref"],
            "Rejected/new/unknown SO unsupported",
        )
        need(
            alloc["demand"] == alloc["fulfilled_demand"] == customer["demand"],
            "SO quantity mismatch",
        )
        need(alloc["deadline"] == customer["deadline"], "Plan altered SO deadline")
        need(
            sum(
                integer(alloc[k], k)
                for k in ("stock_allocated", "purchase_units", "assembly_units")
            )
            == customer["demand"],
            "SO source quantities do not conserve demand",
        )
    need(
        sum(a["stock_allocated"] for a in allocations.values()) == stock_used[code],
        "Finished stock allocation mismatch",
    )
    for bucket in buckets:
        need(
            bucket["product_code"] == code
            and bucket["level"] == 0
            and not bucket["origin_plan_refs"],
            "Only target-level manufacturing supported",
        )
        standard = choices.get(bucket["workcenter_code"])
        need(standard is not None, "Unknown product/workcenter pair")
        integer(bucket["quantity"], "MO target quantity", 1)
        due = integer(bucket["needed_by_days"], "MO deadline")
        need(
            bucket["lead_time"] == standard["lead_time_days"]
            and bucket["time_per_unit_minutes"] == standard["time_per_unit_minutes"]
            and bucket["unit_cost"] == standard["unit_cost_dollars"],
            "Plan manufacturing standards differ from live public SKU/WC facts",
        )
        need(due >= standard["lead_time_days"], "MO must start before planning anchor")
        need(
            bool(bucket["origin_customer_refs"])
            and set(bucket["origin_customer_refs"]) <= customers.keys(),
            "MO target customer references invalid",
        )
        need(caps[bucket["workcenter_code"]] > 0, "Plan uses stopped workcenter")
    plan_offer_keys = [x["offer_key"] for x in purchases]
    need(
        len(plan_offer_keys) == len(set(plan_offer_keys)),
        "Multiple target POs for one offer unsupported",
    )
    for po in purchases:
        v = offers.get(po["offer_key"])
        need(v is not None, "Unknown live offer")
        need(po["offer_key"] != repair.get("broken_supplier_offer_key"), "Plan uses broken offer")
        qty = integer(po["quantity"], "PO target quantity", 1)
        need(
            (
                po["product_code"],
                po["vendor_ref"],
                po["unit_cost"],
                po["lead_time"],
                po["supply_role"],
            )
            == (v["product_code"], v["ref"], v["price_dollars"], v["lead_time"], v["supply_role"]),
            "PO target differs from live supplier offer",
        )
        need(
            qty >= v["min_qty"] and (v["max_qty"] is None or qty <= v["max_qty"]),
            "PO offer quantity outside bounds",
        )
        need(
            integer(po["planned_arrival_days"], "PO arrival") >= v["lead_time"],
            "PO arrival precedes public supplier lead",
        )
        if po["supply_role"] == "component":
            need(
                not po["origin_customer_refs"]
                and bool(po["origin_plan_refs"])
                and set(po["origin_plan_refs"]) <= {b["plan_ref"] for b in buckets},
                "Invalid target component PO plan references",
            )
    vendor_totals = Counter()
    vendor_limits = {}
    for po in purchases:
        pair = (po["product_code"], po["vendor_ref"])
        vendor_totals[pair] += po["quantity"]
        limit = offers[po["offer_key"]]["max_qty"]
        if limit is not None:
            vendor_limits[pair] = min(limit, vendor_limits.get(pair, limit))
    need(
        all(vendor_totals[k] <= v for k, v in vendor_limits.items()),
        "Aggregate vendor/SKU horizon cap exceeded",
    )
    needed_components = {
        sku: sum(x["quantity"] for x in buckets) * per for sku, per in components.items()
    }
    surplus_supply = {}
    for sku, qty in needed_components.items():
        rows = [x for x in purchases if x["product_code"] == sku]
        available = stock_used[sku] + sum(x["quantity"] for x in rows)
        need(
            available >= qty,
            "Prototype rejects surplus/insufficient component supply; exact allocation required",
            code="change_set_supply_quantity_mismatch",
        )
        if available > qty:
            # A minimum order quantity may force a legal remainder. Accept it
            # only when every purchase for the SKU sits exactly at its offer
            # minimum; an arbitrary extra unit is still rejected.
            need(
                bool(rows)
                and all(
                    x["quantity"] == offers[x["offer_key"]]["min_qty"] for x in rows
                ),
                "Prototype rejects surplus/insufficient component supply; exact allocation required",
                code="change_set_supply_quantity_mismatch",
            )
            surplus_supply[sku] = available - qty
    need(
        sum(x["quantity"] for x in buckets)
        == sum(a["assembly_units"] for a in allocations.values()),
        "Target manufacturing does not match SO assembly totals",
    )
    need(
        sum(x["quantity"] for x in purchases if x["supply_role"] == "finished")
        == sum(a["purchase_units"] for a in allocations.values()),
        "Finished PO/SO mismatch",
    )

    # PO reuse is unique by public offer identity; quantity/date changes update
    # the same unreceived commitment. Ambiguous existing duplicates fail closed.
    po_targets, old_po_by_offer = {}, defaultdict(list)
    for old in live_pos:
        if old["ref"] not in broken_pos:
            old_po_by_offer[old["offer_key"]].append(old)
    need(
        all(len(v) <= 1 for v in old_po_by_offer.values()),
        "Ambiguous existing duplicate supplier offer",
        code="change_set_existing_offer_ambiguous",
    )
    for i, po in enumerate(purchases, 1):
        matching = old_po_by_offer[po["offer_key"]]
        po_targets[po["plan_ref"]] = (
            target("purchase.order", old_pos[matching[0]["ref"]])
            if matching
            else target("purchase.order", serial=i)
        )

    # Retained candidates keep their public WC and daily schedule. A target bucket
    # can split across retained/new MOs, or an existing MO span compatible buckets.
    # This deliberately does not optimize moving existing MOs across WCs/dates.
    candidates = []
    for old in live_mos:
        if old["ref"] in broken_mos:
            continue
        std = choices[old["workcenter_code"]]
        need(
            old["start_days"] >= 0
            and old["deadline_days"] - old["start_days"] >= std["lead_time_days"],
            "Existing MO lacks valid public lead interval; rescheduling not supported",
        )
        candidates.append(
            {
                "target": target("mrp.production", old_mos[old["ref"]]),
                "old": old,
                "wc": old["workcenter_code"],
                "start": old["start_days"],
                "due": old["deadline_days"],
                "limit": old["quantity"],
            }
        )
    for i, bucket in enumerate(buckets, 1):
        candidates.append(
            {
                "target": target("mrp.production", serial=i),
                "old": None,
                "wc": bucket["workcenter_code"],
                "start": bucket["needed_by_days"] - bucket["lead_time"],
                "due": bucket["needed_by_days"],
                "limit": bucket["quantity"],
                "bucket": i - 1,
            }
        )
    # Optional planning dependency; importing this module performs no solver work.
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    q, used, full, xb = {}, {}, {}, {}
    for ci, cand in enumerate(candidates):
        q[ci] = model.new_int_var(0, cand["limit"], f"q{ci}")
        used[ci] = model.new_bool_var(f"used{ci}")
        model.add(q[ci] >= 1).only_enforce_if(used[ci])
        model.add(q[ci] == 0).only_enforce_if(used[ci].Not())
        if cand["old"]:
            full[ci] = model.new_bool_var(f"full{ci}")
            model.add(q[ci] == cand["limit"]).only_enforce_if(full[ci])
            model.add(q[ci] < cand["limit"]).only_enforce_if(full[ci].Not())
        for bi, bucket in enumerate(buckets):
            if cand["wc"] != bucket["workcenter_code"] or cand["due"] > bucket["needed_by_days"]:
                continue
            if not cand["old"] and cand["bucket"] != bi:
                continue
            xb[ci, bi] = model.new_int_var(0, min(cand["limit"], bucket["quantity"]), f"x{ci}_{bi}")
        model.add(sum(v for (c, _), v in xb.items() if c == ci) == q[ci])
    for bi, bucket in enumerate(buckets):
        model.add(sum(v for (_, b), v in xb.items() if b == bi) == bucket["quantity"])
    # Joint per-bucket SO allocations: keep target origin eligibility; never force
    # an existing MO's stale origin when its supply assignment has changed.
    so_flow = {}
    for (ci, bi), amount in xb.items():
        bucket = buckets[bi]
        for ref in sorted(set(bucket["origin_customer_refs"])):
            if candidates[ci]["due"] <= customers[ref]["deadline"]:
                so_flow[ci, bi, ref] = model.new_int_var(
                    0, bucket["quantity"], f"so{ci}_{bi}_{ref}"
                )
        model.add(sum(v for (c, b, _), v in so_flow.items() if (c, b) == (ci, bi)) == amount)
    for ref, alloc in allocations.items():
        model.add(sum(v for (_, _, r), v in so_flow.items() if r == ref) == alloc["assembly_units"])
    # Component supply flows jointly constrain how much can be retained at an
    # earlier old start. Future PO quantities are supply promises, never on-hand.
    supplies = []
    for sku in sorted(components):
        if stock_used[sku]:
            supplies.append(
                {
                    "token": f"stock:{sku}",
                    "sku": sku,
                    "quantity": stock_used[sku],
                    "day": 0,
                    "plan_ref": None,
                }
            )
    for po in purchases:
        if po["supply_role"] == "component":
            supplies.append(
                {
                    "token": po_targets[po["plan_ref"]]["token"],
                    "sku": po["product_code"],
                    "quantity": po["quantity"],
                    "day": po["planned_arrival_days"],
                    "plan_ref": po["plan_ref"],
                }
            )
    component_flow = {}
    unused_supply_vars = {}
    for si, supply in enumerate(supplies):
        for ci, cand in enumerate(candidates):
            if supply["day"] <= cand["start"]:
                component_flow[si, ci] = model.new_int_var(0, supply["quantity"], f"c{si}_{ci}")
        flow = sum(v for (s, _), v in component_flow.items() if s == si)
        if supply["plan_ref"] is None:
            # On-hand stock allocated by the plan must actually be consumed.
            model.add(flow == supply["quantity"])
        else:
            slack = model.new_int_var(0, supply["quantity"], f"unused_{si}")
            model.add(flow + slack == supply["quantity"])
            unused_supply_vars[si] = slack
    for ci in range(len(candidates)):
        for sku, per in components.items():
            model.add(
                sum(
                    v
                    for (s, c), v in component_flow.items()
                    if c == ci and supplies[s]["sku"] == sku
                )
                == q[ci] * per
            )
    # Origin labels must also admit a joint coverage assignment. Physical flow
    # alone is insufficient when legal MOQ surplus is split over partially
    # shared consumers: two POs can then declare more coverage than those MOs
    # need. Keep actual origin edges, quantities and surplus; solve their
    # non-overlapping coverage before optimizing retention or edit count.
    origin_coverage = {}
    for si, supply in enumerate(supplies):
        if supply["plan_ref"] is None:
            continue
        per = components[supply["sku"]]
        capacities = []
        for ci, cand in enumerate(candidates):
            actual = component_flow.get((si, ci))
            if actual is None:
                continue
            linked = model.new_bool_var(f"trace_link_{si}_{ci}")
            model.add(actual >= 1).only_enforce_if(linked)
            model.add(actual == 0).only_enforce_if(linked.Not())
            capacity = model.new_int_var(0, cand["limit"] * per, f"trace_capacity_{si}_{ci}")
            model.add(capacity == q[ci] * per).only_enforce_if(linked)
            model.add(capacity == 0).only_enforce_if(linked.Not())
            capacities.append(capacity)
            covered = model.new_int_var(0, supply["quantity"], f"trace_flow_{si}_{ci}")
            model.add(covered >= 1).only_enforce_if(linked)
            model.add(covered == 0).only_enforce_if(linked.Not())
            origin_coverage[si, ci] = covered
        required = model.new_int_var(0, supply["quantity"], f"trace_required_{si}")
        model.add_min_equality(required, [supply["quantity"], sum(capacities)])
        model.add(sum(v for (s, _), v in origin_coverage.items() if s == si) == required)
    for ci in range(len(candidates)):
        for sku, per in components.items():
            model.add(sum(v for (s, c), v in origin_coverage.items()
                          if c == ci and supplies[s]["sku"] == sku) <= q[ci] * per)
    # Finished PO flows obey the plan's customer set and daily supplier timing.
    finished_flow = {}
    for pi, po in enumerate(purchases):
        if po["supply_role"] != "finished":
            continue
        need(
            not po["origin_plan_refs"] and set(po["origin_customer_refs"]) <= customers.keys(),
            "Invalid finished PO origins",
        )
        for ref in sorted(set(po["origin_customer_refs"])):
            if po["planned_arrival_days"] <= customers[ref]["deadline"]:
                finished_flow[pi, ref] = model.new_int_var(0, po["quantity"], f"fg{pi}_{ref}")
        model.add(
            sum(v for (index, _), v in finished_flow.items() if index == pi) == po["quantity"]
        )
    for ref, alloc in allocations.items():
        model.add(
            sum(v for (_, r), v in finished_flow.items() if r == ref) == alloc["purchase_units"]
        )
    # Exact public decimal minutes; no Odoo calendar load/duration is consulted.
    for wc, cap in caps.items():
        minute = Decimal(str(choices[wc]["time_per_unit_minutes"]))
        scale = 10 ** max(0, -minute.as_tuple().exponent, -cap.as_tuple().exponent)
        model.add(
            sum(q[i] * int(minute * scale) for i, c in enumerate(candidates) if c["wc"] == wc)
            <= int(cap * scale)
        )

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = 20
    objectives = []
    retained = sum(q[i] for i, c in enumerate(candidates) if c["old"])
    model.maximize(retained)

    def optimize(label, expr):
        status = solver.solve(model)
        need(
            status == cp_model.OPTIMAL,
            f"Joint mapping {label} not proven feasible/optimal ({solver.status_name(status)})",
            code="change_set_mapping_unproven",
        )
        value = solver.value(expr) if not isinstance(expr, int) else expr
        objectives.append({"objective": label, "value": value})
        model.add(expr == value)

    optimize("maximize_retained_quantity", retained)
    full_count = sum(full.values())
    model.maximize(full_count)
    optimize("maximize_full_existing_mos", full_count)
    new_count = sum(used[i] for i, c in enumerate(candidates) if not c["old"])
    model.minimize(new_count)
    optimize("minimize_new_mo_count", new_count)
    # Prefer old traceability when equivalent joint solutions exist, without
    # accepting stale links to cancelled documents or violating target buckets.
    penalties = []
    link_changes = []

    def link_present(expression, label):
        present = model.new_bool_var(label)
        model.add(expression >= 1).only_enforce_if(present)
        model.add(expression == 0).only_enforce_if(present.Not())
        return present

    for ci, cand in enumerate(candidates):
        if not cand["old"]:
            continue
        old_refs = set(cand["old"]["origin_customer_refs"])
        for ref in customers:
            quantity = sum(v for (c, _, r), v in so_flow.items() if c == ci and r == ref)
            present = link_present(quantity, f"mo_so_link_{ci}_{ref}")
            link_changes.append(1 - present if ref in old_refs else present)
    for (ci, bi, ref), var in so_flow.items():
        old = candidates[ci]["old"]
        if old and ref not in old["origin_customer_refs"]:
            penalties.append(var)
    po_old_tokens = {
        po_targets[po["plan_ref"]]["token"]: old_po_by_offer[po["offer_key"]][0]
        for po in purchases
        if old_po_by_offer[po["offer_key"]]
    }
    for (si, ci), var in component_flow.items():
        old_po = po_old_tokens.get(supplies[si]["token"])
        old_mo = candidates[ci]["old"]
        if old_po and (not old_mo or old_mo["ref"] not in old_po["origin_plan_refs"]):
            penalties.append(var)
    for si, supply in enumerate(supplies):
        old_po = po_old_tokens.get(supply["token"])
        if not old_po:
            continue
        for ci, cand in enumerate(candidates):
            present = link_present(component_flow.get((si, ci), 0), f"po_mo_link_{si}_{ci}")
            was_linked = cand["old"] and cand["old"]["ref"] in old_po["origin_plan_refs"]
            link_changes.append(1 - present if was_linked else present)
    changed_links = sum(link_changes)
    model.minimize(changed_links)
    optimize("minimize_existing_origin_edge_changes", changed_links)
    penalty = sum(penalties)
    model.minimize(penalty)
    optimize("minimize_reassigned_trace_quantity", penalty)

    def val(variable):
        return solver.value(variable)

    mo_docs, po_docs, operations, ref_map = [], [], [], defaultdict(list)
    mo_origin_refs = defaultdict(set)
    so_flows = []
    for (ci, bi, ref), var in so_flow.items():
        if val(var):
            token = candidates[ci]["target"]["token"]
            mo_origin_refs[token].add(ref)
            so_flows.append(
                {
                    "source_token": token,
                    "sales_order": so_refs[ref],
                    "quantity": val(var),
                    "product_code": code,
                    "plan_ref": buckets[bi]["plan_ref"],
                    "available_day": candidates[ci]["due"],
                }
            )
    for (pi, ref), var in finished_flow.items():
        if val(var):
            po = purchases[pi]
            so_flows.append(
                {
                    "source_token": po_targets[po["plan_ref"]]["token"],
                    "sales_order": so_refs[ref],
                    "quantity": val(var),
                    "product_code": code,
                    "plan_ref": po["plan_ref"],
                    "available_day": po["planned_arrival_days"],
                }
            )
    for ref, alloc in allocations.items():
        if alloc["stock_allocated"]:
            so_flows.append(
                {
                    "source_token": f"stock:{code}",
                    "sales_order": so_refs[ref],
                    "quantity": alloc["stock_allocated"],
                    "product_code": code,
                    "plan_ref": None,
                    "available_day": 0,
                }
            )
    component_flows, po_mo_origins = [], defaultdict(set)
    for (si, ci), var in component_flow.items():
        if val(var):
            supply, cand = supplies[si], candidates[ci]
            component_flows.append(
                {
                    "source_token": supply["token"],
                    "manufacturing_token": cand["target"]["token"],
                    "quantity": val(var),
                    "product_code": supply["sku"],
                    "available_day": supply["day"],
                    "required_day": cand["start"],
                    "source_plan_ref": supply["plan_ref"],
                }
            )
            if supply["plan_ref"]:
                po_mo_origins[supply["plan_ref"]].add(cand["target"]["token"])
    for (ci, bi), var in xb.items():
        if val(var):
            ref_map[buckets[bi]["plan_ref"]].append(
                {"token": candidates[ci]["target"]["token"], "quantity": val(var)}
            )
    by_token = {c["target"]["token"]: c["target"] for c in candidates}
    all_so_targets = {x["name"]: target("sale.order", x) for x in sos.values()}
    all_mo_targets = {name: target("mrp.production", row) for name, row in old_mos.items()}

    def origins(text):
        result = []
        for name in sorted(set(x.strip() for x in text.split(",") if x.strip())):
            need(name in all_so_targets or name in all_mo_targets, "Unknown existing origin token")
            result.append(copy.deepcopy(all_so_targets.get(name) or all_mo_targets[name]))
        return sorted(result, key=lambda x: x["token"])

    def normalized_mo(old, raw):
        work = [
            w
            for w in models["mrp.workorder"]
            if rid(w["production_id"]) == raw["id"] and w["state"] not in ("done", "cancel")
        ]
        need(len(work) == 1, "One live WO required")
        return {
            "product_id": rid(raw["product_id"]),
            "product_code": code,
            "bom_id": rid(raw["bom_id"]),
            "company_id": rid(raw["company_id"]),
            "product_uom_id": rid(raw["product_uom_id"]),
            "quantity": old["quantity"],
            "workcenter_id": rid(work[0]["workcenter_id"]),
            "workcenter_code": old["workcenter_code"],
            "start_day": old["start_days"],
            "due_day": old["deadline_days"],
            "date_start": raw["date_start"],
            "date_deadline": raw["date_deadline"],
            "origin_refs": origins(raw["origin"]),
            "workorder_id": work[0]["id"],
        }

    def normalized_po(old, raw):
        lines = [
            x
            for x in models["purchase.order.line"]
            if rid(x["order_id"]) == raw["id"] and not x["display_type"]
        ]
        need(len(lines) == 1, "One live PO line required")
        line = lines[0]
        return {
            "partner_id": rid(raw["partner_id"]),
            "vendor_ref": old["vendor_ref"],
            "product_id": rid(line["product_id"]),
            "company_id": rid(raw["company_id"]),
            "currency_id": rid(raw["currency_id"]),
            "product_uom_id": rid(line["product_uom_id"]),
            # Collector does not include PO-line name. Null means preserve,
            # never assert a product name is the observed existing value.
            "line_name": None,
            "product_code": old["product_code"],
            "offer_key": old["offer_key"],
            "quantity": old["quantity"],
            "unit_price": old["price_unit"],
            "arrival_day": old["planned_arrival_days"],
            "date_planned": line["date_planned"],
            "line_id": line["id"],
            "supply_role": old["supply_role"],
            "origin_refs": origins(raw["origin"]),
        }

    def op(kind, dest, before, after, raw, raw_children, plan_rows):
        if before is None:
            action = "create"
        elif after is None:
            action = "cancel"
        else:
            compare_after = {
                k: v for k, v in after.items() if k not in ("workorder_plan", "public_standard")
            }
            action = "keep" if before == compare_after else "update"
        pre = {
            "scope_snapshot_sha256": canonical_hash(snapshot),
            "all_scope_records_unchanged": True,
            "unreceived_unproduced_undelivered": True,
            "source_records": copy.deepcopy({"parent": raw, "children": raw_children})
            if raw
            else None,
            "create_name_must_be_journaled_before_send": action == "create",
        }
        item = {
            "operation_id": f"op:{dest['token']}",
            "kind": kind,
            "action": action,
            "target": dest,
            "before": before,
            "after": after,
            "preconditions": pre,
            "depends_on": [],
            "plan_allocations": plan_rows,
        }
        operations.append(item)
        return item

    preflight = []
    active_old_names = set()
    boms = [
        b
        for b in models["mrp.bom"]
        if b["active"]
        and b["type"] == "normal"
        and (
            rid(b["product_id"]) == products[code]["id"]
            or (
                not rid(b["product_id"])
                and rid(b["product_tmpl_id"]) == rid(products[code]["product_tmpl_id"])
            )
        )
    ]
    need(len(boms) == 1, "Exactly one target BoM must be identified from live records")
    bom = boms[0]
    for ci, cand in enumerate(candidates):
        qty = val(q[ci])
        if not qty:
            continue
        old, dest = cand["old"], cand["target"]
        raw = old_mos[old["ref"]] if old else None
        before = normalized_mo(old, raw) if old else None
        if old:
            active_old_names.add(old["ref"])
        std = choices[cand["wc"]]
        after = {
            "product_id": products[code]["id"],
            "product_code": code,
            "bom_id": rid(raw["bom_id"]) if raw else bom["id"],
            "company_id": company_id,
            "product_uom_id": rid(products[code]["uom_id"]),
            "quantity": qty,
            "workcenter_id": centers[cand["wc"]]["id"],
            "workcenter_code": cand["wc"],
            "start_day": cand["start"],
            "due_day": cand["due"],
            "date_start": before["date_start"] if before else dates(cand["start"]),
            "date_deadline": before["date_deadline"] if before else dates(cand["due"]),
            "origin_refs": sorted(
                [so_refs[r] for r in mo_origin_refs[dest["token"]]], key=lambda x: x["token"]
            ),
            "workorder_id": before["workorder_id"] if before else None,
            "public_standard": {
                "time_per_unit_minutes": std["time_per_unit_minutes"],
                "unit_cost": std["unit_cost_dollars"],
                "lead_days": std["lead_time_days"],
            },
            "workorder_plan": {
                "existing_id": before["workorder_id"] if before else None,
                "duration_expected_minutes": float(
                    Decimal(str(std["time_per_unit_minutes"])) * qty
                ),
                "start_day": cand["start"],
                "due_day": cand["due"],
                "date_start": before["date_start"] if before else dates(cand["start"]),
                "date_finished": before["date_deadline"] if before else dates(cand["due"]),
            },
        }
        plan_rows = [
            {"plan_ref": buckets[bi]["plan_ref"], "quantity": val(v)}
            for (c, bi), v in xb.items()
            if c == ci and val(v)
        ]
        children = [
            w for w in models["mrp.workorder"] if raw and rid(w["production_id"]) == raw["id"]
        ]
        item = op("manufacturing_order", dest, before, after, raw, children, plan_rows)
        item["needs_workorder_schedule_preflight"] = True
        preflight.append(
            {
                "operation_id": item["operation_id"],
                "model": "mrp.workorder",
                "id": before["workorder_id"] if before else None,
                "parent_token": dest["token"],
                "fields": [
                    "state",
                    "workcenter_id",
                    "production_id",
                    "duration_expected",
                    "date_start",
                    "date_finished",
                ],
                "policy": "read then bind before; normalize public schedule if different; read back after",
            }
        )
        mo_docs.append({"target": dest, **after})
    for old in live_mos:
        if old["ref"] in active_old_names:
            continue
        raw = old_mos[old["ref"]]
        op(
            "manufacturing_order",
            target("mrp.production", raw),
            normalized_mo(old, raw),
            None,
            raw,
            [w for w in models["mrp.workorder"] if rid(w["production_id"]) == raw["id"]],
            [],
        )
    retained_po_names = set()
    for po in purchases:
        dest = po_targets[po["plan_ref"]]
        old = old_po_by_offer[po["offer_key"]][0] if dest["id"] else None
        raw = old_pos[old["ref"]] if old else None
        before = normalized_po(old, raw) if old else None
        if old:
            retained_po_names.add(old["ref"])
        if po["supply_role"] == "component":
            origin_refs = [by_token[t] for t in sorted(po_mo_origins[po["plan_ref"]])]
        else:
            origin_refs = [
                so_refs[ref]
                for (pi, ref), v in finished_flow.items()
                if purchases[pi]["plan_ref"] == po["plan_ref"] and val(v)
            ]
        after = {
            "partner_id": partners[po["vendor_ref"]]["id"],
            "vendor_ref": po["vendor_ref"],
            "company_id": company_id,
            "currency_id": currency_id,
            "product_uom_id": rid(products[po["product_code"]]["uom_id"]),
            "line_name": None if before else products[po["product_code"]]["name"],
            "product_id": products[po["product_code"]]["id"],
            "product_code": po["product_code"],
            "offer_key": po["offer_key"],
            "quantity": po["quantity"],
            "unit_price": po["unit_cost"],
            "arrival_day": po["planned_arrival_days"],
            "date_planned": before["date_planned"]
            if before and before["arrival_day"] == po["planned_arrival_days"]
            else dates(po["planned_arrival_days"]),
            "line_id": before["line_id"] if before else None,
            "supply_role": po["supply_role"],
            "origin_refs": sorted(origin_refs, key=lambda x: x["token"]),
        }
        children = [
            line
            for line in models["purchase.order.line"]
            if raw and rid(line["order_id"]) == raw["id"]
        ]
        item = op(
            "purchase_order",
            dest,
            before,
            after,
            raw,
            children,
            [{"plan_ref": po["plan_ref"], "quantity": po["quantity"]}],
        )
        item["depends_on"] = [
            f"op:{ref['token']}" for ref in after["origin_refs"] if ref["model"] == "mrp.production"
        ]
        po_docs.append({"target": dest, **after})
        ref_map[po["plan_ref"]].append({"token": dest["token"], "quantity": po["quantity"]})
    for old in live_pos:
        if old["ref"] not in retained_po_names:
            raw = old_pos[old["ref"]]
            op(
                "purchase_order",
                target("purchase.order", raw),
                normalized_po(old, raw),
                None,
                raw,
                [
                    line
                    for line in models["purchase.order.line"]
                    if rid(line["order_id"]) == raw["id"]
                ],
                [],
            )
    projection = {
        "manufacturing_orders": mo_docs,
        "purchase_orders": po_docs,
        "sales_order_flows": so_flows,
        "component_flows": component_flows,
        "stock_used": stock_used,
        "stock_remaining_unallocated": {sku: stock[sku] - stock_used[sku] for sku in stock},
        "unused_purchase_units": {
            supplies[si]["token"]: val(slack)
            for si, slack in unused_supply_vars.items()
            if val(slack)
        },
        "optimization": objectives,
        "totals": {
            "manufactured_quantity": sum(x["quantity"] for x in mo_docs),
            "retained_manufactured_quantity": sum(
                x["quantity"] for x in mo_docs if x["target"]["id"]
            ),
            "new_manufactured_quantity": sum(
                x["quantity"] for x in mo_docs if not x["target"]["id"]
            ),
            "purchase_order_count": len(po_docs),
            "public_assembly_cost": float(
                sum(
                    Decimal(str(x["public_standard"]["unit_cost"])) * x["quantity"] for x in mo_docs
                )
            ),
            "purchase_spend": float(
                sum(Decimal(str(x["unit_price"])) * x["quantity"] for x in po_docs)
            ),
            "unused_purchase_units": sum(
                val(slack) for slack in unused_supply_vars.values()
            ),
        },
    }
    result = {
        "schema_version": 1,
        "semantics": "target_state_to_change_set",
        "dry_run": True,
        "execution_ready": False,
        "source_sha256": {
            k: canonical_hash(v)
            for k, v in [
                ("request", request),
                ("snapshot", snapshot),
                ("blueprint", blueprint),
                ("plan", fresh_plan),
            ]
        },
        "scope": {
            "company_id": rid(next(iter(sos.values()))["company_id"]),
            "target_product_id": products[code]["id"],
            "target_product_code": code,
            "sales_order_ids": sorted(request["sales_order_ids"]),
            "anchor_date": request["anchor_date"],
        },
        "operations": sorted(operations, key=lambda o: o["operation_id"]),
        "plan_ref_map": dict(ref_map),
        "projected_state": projection,
        "required_preflight_reads": preflight,
        "limitations": [
            "Single target SKU and one-level BoM only; legal minimum-order surplus is recorded as unused supply, never consumed by an MO.",
            "Existing MO WC/dates fixed; retained quantity may reduce and a target bucket may split. This is a bounded lexicographic mapping, not globally minimal arbitrary ERP edits.",
            "Component allocations are recomputed from live public facts; target PO quantities/prices/dates and target MO WC/quantities/deadline bounds are preserved.",
            "WO schedule/duration was not in collector snapshot; executor must bind current WO fields before independent schedule_workorder operation.",
            "New record names must be deterministically allocated and journaled by executor before send; this prototype does not assign ERP names.",
            "No ERP writes, final readback, original verifier or benchmark success is performed here.",
            "Projection checks aggregate spend/margin and each original SO budget cap, not per-customer cost attribution or every external task rule.",
        ],
    }
    projection["checks"] = validate_projection(result, request, snapshot, blueprint, fresh_plan,
                                               product_constraints=product_constraints, product_objective=product_objective)
    return result


def validate_projection(result, request, snapshot, blueprint, fresh_plan, *, product_constraints=None, product_objective=None):
    """Independent arithmetic/flow validation of the emitted target documents."""
    need(objective_matches(fresh_plan, product_objective), "Plan objective binding differs",
         code="change_set_binding_mismatch")
    proj = result["projected_state"]
    need(
        constrained_blueprint(snapshot, request, product_constraints) == blueprint,
        "Projection blueprint/live binding differs"
    )
    need(
        result["source_sha256"]
        == {
            k: canonical_hash(v)
            for k, v in [
                ("request", request),
                ("snapshot", snapshot),
                ("blueprint", blueprint),
                ("plan", fresh_plan),
            ]
        },
        "Projection input binding differs",
    )
    mos, pos = proj["manufacturing_orders"], proj["purchase_orders"]
    docs = {x["target"]["token"]: x for x in mos + pos}
    need(len(docs) == len(mos) + len(pos), "Duplicate active document token")
    active = set(docs)
    cancelled = {o["target"]["token"] for o in result["operations"] if o["action"] == "cancel"}
    need(not active & cancelled, "Document both cancelled and active")
    operation_tokens = [o["target"]["token"] for o in result["operations"]]
    need(len(operation_tokens) == len(set(operation_tokens)), "Duplicate operation target")
    need(
        active == {o["target"]["token"] for o in result["operations"] if o["action"] != "cancel"},
        "Active documents and operations must have exactly one-to-one coverage",
    )
    tables = {model: {row["id"]: row for row in rows} for model, rows in snapshot["models"].items()}
    products = {p["default_code"]: p for p in snapshot["models"]["product.product"]}
    centers = {w["code"]: w for w in snapshot["models"]["mrp.workcenter"]}
    partners = {p["ref"]: p for p in snapshot["models"]["res.partner"] if p["ref"]}
    reference_so = tables["sale.order"][request["sales_order_ids"][0]]
    company_id, currency_id = rid(reference_so["company_id"]), rid(reference_so["currency_id"])

    def verify_target(ref):
        need(
            ref["model"] in ("sale.order", "mrp.production", "purchase.order"),
            "Unsupported target model",
        )
        if ref["id"] is None:
            prefix = f"new:{ref['model']}:"
            need(
                ref["model"] != "sale.order"
                and ref["name"] is None
                and ref["token"].startswith(prefix)
                and ref["token"][len(prefix) :].isdigit(),
                "Malformed generated target",
            )
        else:
            need(
                type(ref["id"]) is int
                and ref["id"] in tables[ref["model"]]
                and ref == target(ref["model"], tables[ref["model"]][ref["id"]]),
                "Target token/model/ID/name binding changed",
            )

    named_existing = {
        row["name"]: target(model, row)
        for model in ("sale.order", "mrp.production")
        for row in tables[model].values()
    }
    old_mo_facts = {
        row["ref"]: row for row in blueprint["repair_context"]["seeded_manufacturing_orders"]
    }
    old_po_facts = {
        row["ref"]: row for row in blueprint["repair_context"]["seeded_purchase_orders"]
    }

    def expected_before(dest):
        raw = tables[dest["model"]][dest["id"]]
        old_origins = sorted(
            [named_existing[name.strip()] for name in raw["origin"].split(",") if name.strip()],
            key=lambda ref: ref["token"],
        )
        if dest["model"] == "mrp.production":
            fact = old_mo_facts[raw["name"]]
            children = [
                w
                for w in snapshot["models"]["mrp.workorder"]
                if rid(w["production_id"]) == dest["id"]
            ]
            active_work = [w for w in children if w["state"] not in ("done", "cancel")]
            need(len(active_work) == 1, "Expected before WO ambiguous")
            before = {
                "product_id": rid(raw["product_id"]),
                "product_code": fact["product_code"],
                "bom_id": rid(raw["bom_id"]),
                "company_id": rid(raw["company_id"]),
                "product_uom_id": rid(raw["product_uom_id"]),
                "quantity": fact["quantity"],
                "workcenter_id": rid(active_work[0]["workcenter_id"]),
                "workcenter_code": fact["workcenter_code"],
                "start_day": fact["start_days"],
                "due_day": fact["deadline_days"],
                "date_start": raw["date_start"],
                "date_deadline": raw["date_deadline"],
                "origin_refs": old_origins,
                "workorder_id": active_work[0]["id"],
            }
        else:
            fact = old_po_facts[raw["name"]]
            children = [
                line
                for line in snapshot["models"]["purchase.order.line"]
                if rid(line["order_id"]) == dest["id"]
            ]
            order_lines = [line for line in children if not line["display_type"]]
            need(len(order_lines) == 1, "Expected before PO line ambiguous")
            line = order_lines[0]
            before = {
                "partner_id": rid(raw["partner_id"]),
                "vendor_ref": fact["vendor_ref"],
                "product_id": rid(line["product_id"]),
                "company_id": rid(raw["company_id"]),
                "currency_id": rid(raw["currency_id"]),
                "product_uom_id": rid(line["product_uom_id"]),
                "line_name": None,
                "product_code": fact["product_code"],
                "offer_key": fact["offer_key"],
                "quantity": fact["quantity"],
                "unit_price": fact["price_unit"],
                "arrival_day": fact["planned_arrival_days"],
                "date_planned": line["date_planned"],
                "line_id": line["id"],
                "supply_role": fact["supply_role"],
                "origin_refs": old_origins,
            }
        return before, {"parent": raw, "children": children}

    for doc in mos + pos:
        verify_target(doc["target"])
        product = products.get(doc["product_code"])
        need(
            product is not None
            and doc["product_id"] == product["id"]
            and doc["product_uom_id"] == rid(product["uom_id"])
            and doc["company_id"] == company_id,
            "Document product/UoM/company binding changed",
        )
        for origin in doc["origin_refs"]:
            verify_target(origin)
            if origin["model"] == "sale.order":
                need(
                    origin["id"] in request["sales_order_ids"],
                    "Origin points outside selected sales orders",
                )
            else:
                need(
                    origin["token"] in docs and origin == docs[origin["token"]]["target"],
                    "Origin target differs from active document",
                )
        if doc in mos:
            need(
                doc["workcenter_code"] in centers
                and doc["workcenter_id"] == centers[doc["workcenter_code"]]["id"],
                "Workcenter code/ID binding changed",
            )
            bom = tables["mrp.bom"].get(doc["bom_id"])
            need(
                bom is not None
                and bom["active"]
                and bom["type"] == "normal"
                and rid(bom["product_tmpl_id"]) == rid(product["product_tmpl_id"])
                and rid(bom["product_id"]) in (False, None, product["id"]),
                "BoM binding changed",
            )
            work = [
                w
                for w in tables["mrp.workorder"].values()
                if doc["target"]["id"] is not None
                and rid(w["production_id"]) == doc["target"]["id"]
                and w["state"] not in ("done", "cancel")
            ]
            expected_wo = work[0]["id"] if len(work) == 1 else None
            need(
                doc["workorder_id"] == expected_wo == doc["workorder_plan"]["existing_id"],
                "WO parent/ID binding changed",
            )
        else:
            need(
                doc["vendor_ref"] in partners
                and doc["partner_id"] == partners[doc["vendor_ref"]]["id"]
                and doc["currency_id"] == currency_id,
                "PO vendor/currency binding changed",
            )
            lines = [
                line
                for line in tables["purchase.order.line"].values()
                if doc["target"]["id"] is not None
                and rid(line["order_id"]) == doc["target"]["id"]
                and not line["display_type"]
            ]
            need(
                doc["line_id"] == (lines[0]["id"] if len(lines) == 1 else None),
                "PO line/parent binding changed",
            )
    for operation in result["operations"]:
        verify_target(operation["target"])
        need(
            operation["action"] in ("keep", "update", "cancel", "create")
            and operation["kind"] in ("manufacturing_order", "purchase_order"),
            "Unsupported operation",
        )
        need(
            operation["target"]["model"]
            == {"manufacturing_order": "mrp.production", "purchase_order": "purchase.order"}[
                operation["kind"]
            ],
            "Operation kind/model binding changed",
        )
        if operation["target"]["id"] is None:
            need(
                operation["action"] == "create" and operation["before"] is None,
                "New target action/before invalid",
            )
            source_records = None
        else:
            expected, source_records = expected_before(operation["target"])
            need(
                operation["before"] == expected and operation["action"] != "create",
                "Before does not match live snapshot",
            )
        need(
            operation["preconditions"]
            == {
                "scope_snapshot_sha256": canonical_hash(snapshot),
                "all_scope_records_unchanged": True,
                "unreceived_unproduced_undelivered": True,
                "source_records": source_records,
                "create_name_must_be_journaled_before_send": operation["action"] == "create",
            },
            "Preconditions/source records do not match live snapshot",
        )
        token = operation["target"]["token"]
        if operation["action"] != "cancel":
            need(
                token in docs
                and operation["after"] == {k: v for k, v in docs[token].items() if k != "target"},
                "Operation after differs from projected document",
            )
            if operation["action"] != "create":
                equivalent = operation["before"] == {
                    k: v
                    for k, v in operation["after"].items()
                    if k not in ("public_standard", "workorder_plan")
                }
                need(
                    operation["action"] == ("keep" if equivalent else "update"),
                    "Keep/update classification differs from before/after",
                )
        else:
            need(operation["after"] is None, "Cancel operation has an active after state")
        expected_pieces = sorted(
            [
                {"plan_ref": ref, "quantity": piece["quantity"]}
                for ref, pieces in result["plan_ref_map"].items()
                for piece in pieces
                if piece["token"] == token
            ],
            key=lambda row: row["plan_ref"],
        )
        need(
            sorted(operation["plan_allocations"], key=lambda row: row["plan_ref"])
            == expected_pieces,
            "Operation plan allocations differ from target mapping",
        )
        expected_dependencies = sorted(
            [
                f"op:{ref['token']}"
                for ref in (operation["after"] or {}).get("origin_refs", [])
                if operation["kind"] == "purchase_order" and ref["model"] == "mrp.production"
            ]
        )
        need(
            sorted(operation["depends_on"]) == expected_dependencies,
            "Operation dependencies differ from origin mapping",
        )
    broken_names = set(
        blueprint["repair_context"]["broken_manufacturing_order_refs"]
        + blueprint["repair_context"]["broken_purchase_order_refs"]
    )
    need(
        broken_names
        <= {o["target"]["name"] for o in result["operations"] if o["action"] == "cancel"},
        "Broken commitment was not cancelled",
    )
    live_names = {
        x["ref"]
        for key in ("seeded_manufacturing_orders", "seeded_purchase_orders")
        for x in blueprint["repair_context"][key]
    }
    observed_names = [o["target"]["name"] for o in result["operations"] if o["target"]["id"]]
    need(
        set(observed_names) == live_names and len(observed_names) == len(live_names),
        "Existing commitment omitted or counted twice",
    )
    standards = {
        x["workcenter_code"]: x for x in blueprint["manufactured_products"][0]["workcenter_choices"]
    }
    capacities = {x["code"]: Decimal(str(x["capacity_minutes"])) for x in blueprint["workcenters"]}
    capacity_used = defaultdict(Decimal)
    anchor = date.fromisoformat(request["anchor_date"])
    for mo in mos:
        std = standards[mo["workcenter_code"]]
        need(
            mo["due_day"] - mo["start_day"] >= std["lead_time_days"] and mo["start_day"] >= 0,
            "MO public lead interval invalid",
        )
        expected = Decimal(str(std["time_per_unit_minutes"])) * mo["quantity"]
        need(
            mo["public_standard"]
            == {
                "time_per_unit_minutes": std["time_per_unit_minutes"],
                "unit_cost": std["unit_cost_dollars"],
                "lead_days": std["lead_time_days"],
            },
            "MO public standard changed",
        )
        need(
            Decimal(str(mo["workorder_plan"]["duration_expected_minutes"])) == expected,
            "WO duration differs from public standard",
        )
        need(
            mo["workorder_plan"]["start_day"] == mo["start_day"]
            and mo["workorder_plan"]["due_day"] == mo["due_day"],
            "MO/WO daily schedule differs",
        )
        need(
            day(mo["date_start"], anchor) == mo["start_day"]
            and day(mo["date_deadline"], anchor) == mo["due_day"]
            and day(mo["workorder_plan"]["date_start"], anchor) == mo["start_day"]
            and day(mo["workorder_plan"]["date_finished"], anchor) == mo["due_day"],
            "MO/WO timestamps differ from daily dates",
        )
        capacity_used[mo["workcenter_code"]] += expected
    need(
        all(capacity_used[wc] <= cap for wc, cap in capacities.items()), "Horizon capacity exceeded"
    )
    for po in pos:
        need(
            day(po["date_planned"], anchor) == po["arrival_day"],
            "PO timestamp differs from arrival day",
        )
        need(po["origin_refs"], "PO lacks actual supply consumers")
        need(
            all(r["token"] not in cancelled for r in po["origin_refs"]),
            "PO origin references cancelled document",
        )
        if po["supply_role"] == "component":
            need(
                all(
                    r["token"] in active and r["model"] == "mrp.production"
                    for r in po["origin_refs"]
                ),
                "PO origin unresolved",
            )
    planned = {
        x["plan_ref"]: x
        for x in fresh_plan["plan"]["manufacturing_orders"] + fresh_plan["plan"]["purchase_orders"]
    }
    need(set(result["plan_ref_map"]) == set(planned), "Target plan reference lost")
    mapped = Counter()
    for ref, pieces in result["plan_ref_map"].items():
        need(
            sum(piece["quantity"] for piece in pieces) == planned[ref]["quantity"],
            "Plan bucket quantity changed",
        )
        for piece in pieces:
            need(piece["token"] in docs, "Plan maps to nonexistent active document")
            mapped[piece["token"]] += piece["quantity"]
            doc = docs[piece["token"]]
            if "workcenter_code" in planned[ref]:
                need(
                    doc["workcenter_code"] == planned[ref]["workcenter_code"]
                    and doc["due_day"] <= planned[ref]["needed_by_days"],
                    "Plan MO bucket WC/deadline changed",
                )
            else:
                original = planned[ref]
                need(
                    (doc["offer_key"], doc["product_code"], doc["unit_price"], doc["arrival_day"])
                    == (
                        original["offer_key"],
                        original["product_code"],
                        original["unit_cost"],
                        original["planned_arrival_days"],
                    ),
                    "Projected PO changed target offer/product/price/arrival",
                )
    need(
        all(mapped[token] == doc["quantity"] for token, doc in docs.items()),
        "Retained or new quantity counted more than once",
    )
    customers = {x["task_order_ref"]: x for x in blueprint["customers"]}
    so_total, finished_used = Counter(), Counter()
    so_links = defaultdict(set)
    for flow in proj["sales_order_flows"]:
        name = flow["sales_order"]["name"]
        need(name in customers and flow["quantity"] > 0, "Bad SO flow")
        raw_so = next((s for s in snapshot["models"]["sale.order"] if s["name"] == name), None)
        need(
            raw_so is not None and flow["sales_order"] == target("sale.order", raw_so),
            "SO flow ID/name binding changed",
        )
        source = flow["source_token"]
        source_day = (
            0
            if source == f"stock:{request['target_product_code']}"
            else (
                docs[source].get("due_day", docs[source].get("arrival_day"))
                if source in docs
                else None
            )
        )
        need(
            source_day is not None and source_day == flow["available_day"],
            "Finished flow forged source date/token",
        )
        need(
            flow["available_day"] <= customers[name]["deadline"], "Supply arrives after SO deadline"
        )
        so_total[name] += flow["quantity"]
        finished_used[flow["source_token"]] += flow["quantity"]
        so_links[flow["source_token"]].add(flow["sales_order"]["token"])
    need(
        all(so_total[name] == c["demand"] for name, c in customers.items()),
        "SO demand not conserved",
    )
    for mo in mos:
        need(
            finished_used[mo["target"]["token"]] == mo["quantity"], "MO supply over/underallocated"
        )
        need(
            {r["token"] for r in mo["origin_refs"]} == so_links[mo["target"]["token"]],
            "MO origin does not match SO flow",
        )
    component_need = {
        x["product"]["code"]: x["per_unit"]
        for x in blueprint["manufactured_products"][0]["components"]
    }
    supplied, consumed = Counter(), Counter()
    po_links = defaultdict(set)
    for flow in proj["component_flows"]:
        token, source, sku = flow["manufacturing_token"], flow["source_token"], flow["product_code"]
        need(token in docs and flow["quantity"] > 0, "Bad component flow")
        source_day = (
            0
            if source == f"stock:{sku}"
            else (
                docs[source]["arrival_day"]
                if source in docs
                and docs[source].get("supply_role") == "component"
                and docs[source]["product_code"] == sku
                else None
            )
        )
        need(
            source_day is not None and source_day == flow["available_day"],
            "Component flow forged source date/product/token",
        )
        need(
            flow["available_day"] <= flow["required_day"] == docs[token]["start_day"],
            "Component arrival after MO start",
        )
        supplied[source] += flow["quantity"]
        consumed[token, sku] += flow["quantity"]
        po_links[source].add(token)
    for mo in mos:
        for sku, per in component_need.items():
            need(
                consumed[mo["target"]["token"], sku] == mo["quantity"] * per,
                "MO BoM quantity not conserved",
            )
    offers = {offer_key(v): v for v in blueprint["vendors"]}
    for po in pos:
        token = po["target"]["token"]
        if po["supply_role"] == "component":
            usage = supplied[token]
            need(usage <= po["quantity"], "PO supply over/underallocated")
            unused = po["quantity"] - usage
            if unused:
                offer = offers.get(po["offer_key"])
                need(
                    offer is not None and po["quantity"] == offer["min_qty"],
                    "Unused component supply is not explained by the offer minimum",
                )
            need(
                proj.get("unused_purchase_units", {}).get(token, 0) == unused,
                "PO unused supply record differs from allocation",
            )
        else:
            usage = finished_used[token]
            need(usage == po["quantity"], "PO supply over/underallocated")
        desired = po_links[token] if po["supply_role"] == "component" else so_links[token]
        need(
            {r["token"] for r in po["origin_refs"]} == desired,
            "PO origin set differs from actual allocated consumers",
        )
    need(_origin_capacity_feasible(pos, mos, component_need),
         "PO origins overstate jointly coverable consumer demand",
         code="change_set_origin_capacity_mismatch")
    for sku, qty in proj["stock_used"].items():
        observed = (
            supplied[f"stock:{sku}"] if sku in component_need else finished_used[f"stock:{sku}"]
        )
        need(observed == qty <= blueprint["product_stock_units"][sku], "Stock double allocation")
    try:
        expected_spend = decimal_amount(fresh_plan["plan"]["expected_new_spend"])
        actual_spend = sum((decimal_amount(x["unit_price"], cents=True) * x["quantity"]
                            for x in pos), Decimal(0)) + sum(
            (decimal_amount(standards[x["workcenter_code"]]["unit_cost_dollars"], cents=True)
             * x["quantity"] for x in mos), Decimal(0))
    except ValueError as error:
        raise ChangeSetError(str(error)) from None
    need(abs(expected_spend - actual_spend) <= Decimal("0.02"), "Aggregate new spend changed")
    # These flows have already passed document identity, source dates, quantity
    # conservation and stock checks above. Count only actual finished supply
    # through purchasing/manufacturing, never the solver's claimed margin.
    backed_by_order, stock_by_order = Counter(), Counter()
    for flow in proj["sales_order_flows"]:
        source = flow["source_token"]
        quantity = integer(flow["quantity"], "Margin finished quantity", 1)
        if source == f"stock:{request['target_product_code']}":
            stock_by_order[flow["sales_order"]["name"]] += quantity
        else:
            supply = docs[source]
            need(supply["product_code"] == request["target_product_code"]
                 and (supply in mos or supply.get("supply_role") == "finished"),
                 "Margin coverage requires finished purchased or manufactured supply")
            backed_by_order[flow["sales_order"]["name"]] += quantity
    revenue = Decimal(0)
    for sid in request["sales_order_ids"]:
        lines = [
            line
            for line in snapshot["models"]["sale.order.line"]
            if rid(line["order_id"]) == sid and not line["display_type"]
        ]
        need(len(lines) == 1, "SO financial scope ambiguous")
        line = lines[0]
        try:
            price = decimal_amount(line["price_unit"], cents=True)
            amount = decimal_amount(line["product_uom_qty"]) * price
        except ValueError as error:
            raise ChangeSetError(str(error)) from None
        need(
            amount <= Decimal(str(request["sales_order_budget_caps"][str(sid)])),
            "Preserved sales order exceeds explicit customer budget cap",
        )
        name = tables["sale.order"][sid]["name"]
        revenue += backed_by_order[name] * price
    need(
        margin_satisfied(actual_spend, revenue, request["margin"]),
        "Projected combined new-spend margin is below explicit requirement",
    )
    product_checks = check_product_margin(actual_spend, revenue, product_constraints, request)
    ratio = margin_ratio(revenue, actual_spend)
    return {
        "quantity_conservation": True,
        "target_bucket_conservation": True,
        "stock_used_once": True,
        "public_sku_wc_standard": True,
        "horizon_capacity": True,
        "supplier_lead_and_so_daily_deadlines": True,
        "component_before_mo_start": True,
        "mo_wo_daily_dates_agree": True,
        "po_to_mo_and_so_links": True,
        "no_cancelled_origin": True,
        "aggregate_spend_unchanged": True,
        "active_operation_coverage": True,
        "live_id_bindings": True,
        "sales_order_budget_caps": True,
        "margin_basis": MARGIN_BASIS,
        "new_spend_units": sum(backed_by_order.values()),
        "new_spend_revenue": decimal_text(revenue),
        "stock_units": sum(stock_by_order.values()),
        "combined_new_spend_margin": None if ratio is None else float(ratio),
        "required_margin": request["margin"],
        **product_checks,
        "capacity_minutes": {
            wc: {"used": float(capacity_used[wc]), "limit": float(cap)}
            for wc, cap in capacities.items()
        },
        "projected_new_spend": float(actual_spend),
    }
