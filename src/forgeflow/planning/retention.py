"""Opt-in allocation refinement with fixed upstream repair and spending optima.

This adapter uses the pinned generic model without changing upstream sources.
It fixes purchase and workcenter totals, then jointly assigns finished stock,
due buckets and reusable old MOs. It does not optimize arbitrary ERP edits.
Every replacement must also pass the independent recovery projection checks.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import sys
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

RETENTION_PROFILE = "fixed-totals-retention-v1"
DEFAULT_SOLVE_SECONDS = 2.0
MAX_SOLVE_SECONDS = 20.0


class RefinementUnavailable(ValueError):
    """A bounded, non-secret reason to retain the original verified plan."""


def _digest(value):
    # Match persisted recovery artifact identities, including their final newline.
    encoded = (json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n").encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_refinement_audit(plan):
    """Check durable source evidence and status semantics without re-solving."""
    try:
        audit = plan["retention_refinement"]
        original = audit["original_plan"]
        valid = (
            audit["schema_version"] == 1 and audit["profile"] == RETENTION_PROFILE
            and isinstance(original, dict) and "retention_refinement" not in original
            and audit["original_plan_sha256"] == _digest(original)
            and original["blueprint_sha256"] == plan["blueprint_sha256"]
            and original.get("product_objective") == plan.get("product_objective")
            and original["plan"]["objective_kind"] == plan["plan"]["objective_kind"]
            and all(type(audit[key]) is int and audit[key] >= 0
                    for key in ("retained_before", "retained_after"))
        )
        if audit["status"] == "improved":
            valid = (valid and audit["retained_after"] > audit["retained_before"]
                     and audit["solver"]["status"] == "OPTIMAL"
                     and audit["solver"]["retained_quantity_optimum"] == audit["retained_after"]
                     and _totals(plan["plan"]) == _totals(original["plan"])
                     and _spend_cents(plan["plan"]) == audit["spend_optimum_cents"]
                     == _spend_cents(original["plan"])
                     and plan["plan"]["objective_value"] == audit["primary_optimum"]
                     == original["plan"]["objective_value"])
        else:
            valid = (valid and audit["status"] in {"unchanged", "fallback", "skipped"}
                     and audit["retained_after"] == audit["retained_before"]
                     and plan["plan"] == original["plan"])
        if not valid:
            raise ValueError
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("retention_audit_invalid") from None


def _integer(value):
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise RefinementUnavailable("unsupported_numeric_precision")
    return int(number)


def _spend_cents(plan):
    # Derive cents from the actual lines, not a possibly rounded display total.
    return sum(_integer(Decimal(str(row["unit_cost"])) * 100) * row["quantity"]
               for row in plan["purchase_orders"] + plan["manufacturing_orders"])


def _totals(plan):
    purchases, manufacturing = defaultdict(int), defaultdict(int)
    for row in plan["purchase_orders"]:
        purchases[row["offer_key"]] += row["quantity"]
    for row in plan["manufacturing_orders"]:
        manufacturing[row["workcenter_code"]] += row["quantity"]
    return dict(purchases), dict(manufacturing), {
        row["code"]: row["used"] for row in plan["stock_report"]
    }


def _proven_optima(plan):
    trace = plan.get("trace", {})
    if not trace.get("models") or any(
        model.get("objective_kind") != "repair_plan" for model in trace["models"]
    ):
        return False
    # The last attempt of each phase must certify the optimum. Earlier retries
    # may be FEASIBLE/UNKNOWN; lexicographic display tie-breaks are irrelevant.
    calls = trace.get("solve_calls", [])
    for suffix in (" primary", " spend secondary"):
        phases = [call for call in calls if call.get("phase", "").endswith(
            (suffix, suffix + " retry"))]
        if not phases or phases[-1].get("status") != "OPTIMAL":
            return False
    return True


def _validate(request, snapshot, blueprint, plan, constraints, objective):
    from forgeflow.recovery.change_set import build_change_set, validate_projection

    changes = build_change_set(request, snapshot, blueprint, plan,
                               product_constraints=constraints, product_objective=objective)
    validate_projection(changes, request, snapshot, blueprint, plan,
                        product_constraints=constraints, product_objective=objective)
    return changes["projected_state"]["totals"]["retained_manufactured_quantity"]


def _joint_model(generator, scenario, original, primary, cents):
    """Retain upstream feasibility, adding a bounded old/new MO material flow."""
    model, variables = generator._build_model(scenario)
    purchases, manufactured, stock = _totals(original)
    context = generator._build_model_context(scenario)
    target = scenario.product.code
    choices = context.final_product_workcenter_choices
    if len(scenario.manufactured_products) != 1 or not context.final_product:
        raise RefinementUnavailable("unsupported_manufacturing_scope")
    if variables.manufactured_choice_quantity_by_day:
        raise RefinementUnavailable("unsupported_manufacturing_scope")

    # Installing the original repair expression also installs outage exclusions.
    model.add(generator._repair_penalty_expr(model, scenario, variables) == primary)
    model.add(sum(generator._new_spend_terms(scenario, variables)) == cents)
    for vi, vendor in enumerate(context.finished_vendors):
        model.add(sum(q for (index, _), q in variables.finished_vendor_quantity_by_due_day.items()
                      if index == vi) == purchases.get(vendor.offer_key, 0))
    for (code, ref), values in variables.component_vendor_vars.items():
        vendors = [v for v in scenario.vendors if v.product_code == code and v.ref == ref]
        if len(vendors) != 1:
            raise RefinementUnavailable("ambiguous_component_offer")
        model.add(values.quantity == purchases.get(vendors[0].offer_key, 0))
    for ci, choice in enumerate(choices):
        model.add(sum(q for (index, _), q in
                      variables.final_product_workcenter_choice_quantity_by_due_day.items()
                      if index == ci) == manufactured.get(choice.workcenter_code, 0))
    model.add(sum(variables.stock_to_due_day.values()) == stock.get(target, 0))
    for code, quantity in variables.stock_use_by_code.items():
        model.add(quantity == stock.get(code, 0))

    buckets = variables.final_product_workcenter_choice_quantity_by_due_day
    repair = scenario.repair_context
    broken = set(repair.broken_manufacturing_order_refs)
    old = [row for row in repair.seeded_manufacturing_orders if row.ref not in broken]
    old_quantities, old_flows = [], defaultdict(list)
    candidates = []
    for oi, row in enumerate(old):
        limit = _integer(row.quantity)
        quantity = model.new_int_var(0, limit, f"retained_{oi}")
        edges = []
        for (ci, due), bucket in buckets.items():
            if choices[ci].workcenter_code == row.workcenter_code and row.deadline_days <= due:
                flow = model.new_int_var(0, limit, f"retained_bucket_{oi}_{ci}_{due}")
                old_flows[ci, due].append(flow)
                edges.append(flow)
        model.add(quantity == sum(edges))
        old_quantities.append(quantity)
        candidates.append((quantity, row.start_days))
    for (ci, due), quantity in buckets.items():
        new = model.new_int_var(0, context.total_demand, f"new_bucket_{ci}_{due}")
        model.add(sum(old_flows[ci, due]) + new == quantity)
        candidates.append((new, due - choices[ci].lead_time_days))

    # Old MOs can start earlier than the generic due bucket. Prove that their
    # material is already available, rather than counting a future PO as stock.
    for component in context.final_product.components:
        code, per = component.product.code, component.per_unit
        supplies = [(stock.get(code, 0), 0, True)]
        supplies.extend((row["quantity"], row["planned_arrival_days"], False)
                        for row in original["purchase_orders"]
                        if row["supply_role"] == "component" and row["product_code"] == code)
        consumption = defaultdict(list)
        for si, (available, day, on_hand) in enumerate(supplies):
            flows = []
            for ci, (_, start) in enumerate(candidates):
                if day <= start:
                    flow = model.new_int_var(0, available, f"material_{code}_{si}_{ci}")
                    consumption[ci].append(flow)
                    flows.append(flow)
            model.add(sum(flows) == available if on_hand else sum(flows) <= available)
        for ci, (quantity, _) in enumerate(candidates):
            model.add(sum(consumption[ci]) == quantity * per)
    retained = sum(old_quantities)
    model.maximize(retained)
    return model, variables, retained


def _solve_refinement(blueprint, original, upstream, primary, cents, seconds):
    """One bounded CP-SAT call, with no retry and no additional tie-break pass."""
    sys.path.insert(0, str(upstream))
    try:
        from ortools.sat.python import cp_model
        from procurement.procurement_objectives import ProcurementObjectiveKind
        from procurement.solver import ScenarioBlueprint, ScenarioGenerator

        scenario = ScenarioBlueprint.model_validate(blueprint)
        generator = ScenarioGenerator(scenario, num_search_workers=1, seed=0,
                                      randomize_search=False, log_level=logging.WARNING)
        started = time.monotonic()
        model, variables, retained = _joint_model(generator, scenario, original, primary, cents)
        remaining = seconds - (time.monotonic() - started)
        if remaining <= 0:
            return None, {"status": "BUDGET_EXHAUSTED", "solve_calls": 0}
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        solver.parameters.randomize_search = False
        solver.parameters.max_time_in_seconds = remaining
        status = solver.solve(model)
        trace = {"status": solver.status_name(status), "solve_calls": 1,
                 "time_limit_seconds": remaining, "solver_wall_seconds": solver.wall_time}
        if status != cp_model.OPTIMAL:
            return None, trace
        trace["retained_quantity_optimum"] = (retained if isinstance(retained, int)
                                              else solver.value(retained))
        result = generator._extract_plan(scenario, solver, variables,
            objective_kind=ProcurementObjectiveKind.repair_plan, objective_value=float(primary))
        return result.model_dump(mode="json"), trace
    finally:
        sys.path.pop(0)


def refine_retention(request, snapshot, blueprint, original_plan, upstream: Path, *,
                     product_constraints=None, product_objective=None,
                     time_limit_seconds=DEFAULT_SOLVE_SECONDS):
    """Return an audited improvement or the unchanged, independently valid plan.

    Call only when explicitly enabled. The returned wrapper is new even on a
    fallback: its audit is part of the new plan identity and needs fresh approval.
    Invalid original plans raise; they are never turned into successful fallbacks.
    """
    if (type(time_limit_seconds) not in (int, float) or not math.isfinite(time_limit_seconds)
            or not 0 <= time_limit_seconds <= MAX_SOLVE_SECONDS):
        raise ValueError("retention_time_limit_invalid")
    if isinstance(original_plan, dict) and "retention_refinement" in original_plan:
        raise ValueError("retention_already_refined")
    before = _validate(request, snapshot, blueprint, original_plan,
                       product_constraints, product_objective)
    result = copy.deepcopy(original_plan)
    audit = {"schema_version": 1, "profile": RETENTION_PROFILE, "status": "fallback",
             "original_plan_sha256": _digest(original_plan),
             "original_plan": copy.deepcopy(original_plan),
             "scope": "fixed_purchase_workcenter_and_stock_totals",
             "solve_budget_seconds": float(time_limit_seconds),
             "retained_before": before, "retained_after": before,
             "solver": {"status": "NOT_RUN", "solve_calls": 0}}
    result["retention_refinement"] = audit
    if product_objective not in (None, "preserve_quantities"):
        audit.update(status="skipped", reason="non_repair_objective")
        return result
    if not _proven_optima(original_plan):
        audit.update(status="skipped", reason="original_optima_not_proven")
        return result
    if time_limit_seconds == 0:
        audit.update(reason="budget_exhausted")
        return result
    try:
        primary = _integer(original_plan["plan"]["objective_value"])
        cents = _spend_cents(original_plan["plan"])
        audit.update(primary_optimum=primary, spend_optimum_cents=cents)
        plan, trace = _solve_refinement(blueprint, original_plan["plan"], upstream,
                                      primary, cents, time_limit_seconds)
        audit["solver"] = trace
        if plan is None:
            audit["reason"] = "refinement_optimum_not_proven"
            return result
        if (_totals(plan) != _totals(original_plan["plan"])
                or _integer(plan["objective_value"]) != primary or _spend_cents(plan) != cents):
            raise RefinementUnavailable("fixed_objectives_changed")
        candidate = copy.deepcopy(result)
        candidate["plan"] = plan
        after = _validate(request, snapshot, blueprint, candidate,
                          product_constraints, product_objective)
        if after < before or after != trace["retained_quantity_optimum"]:
            raise RefinementUnavailable("retention_mapping_not_proven")
        if after == before:
            audit.update(status="unchanged", reason="no_retention_improvement")
            return result
        candidate["retention_refinement"].update(status="improved", reason="validated_improvement",
                                                retained_after=after)
        return candidate
    except Exception as error:
        # A replacement is optional. Do not echo ERP data or exception messages;
        # return the verified original, recording a bounded diagnostic category.
        audit["reason"] = (str(error) if isinstance(error, RefinementUnavailable)
                           else "refinement_failed_validation_or_solver")
        return result
