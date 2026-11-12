"""Time-phased manufacturing recovery planning.

The general model behind "supplier A cannot deliver; what is the recovery plan":

* multi-level BOMs with shared components and shared workcenters;
* several products, several sales orders, several due dates and priorities;
* stock on hand (including already reserved quantity), purchase options with
  price, minimum order quantity, per-period supply limit and lead time;
* workcenters with per-period capacity and alternate resources;
* formal rules compiled into hard constraints.

Missing facts stay missing: an unknown supply or an unknown budget is recorded as
an assumption on the result, never used as a free resource.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .multi_bom import (
    BomError,
    BomNode,
    SolverStatus,
    SupplyStatus,
    explosion,
    normalise_supply,
)


@dataclass(frozen=True, slots=True)
class Period:
    index: int
    label: str
    start: date | None = None
    end: date | None = None


@dataclass(frozen=True, slots=True)
class DemandLine:
    order_id: str
    product: str
    quantity: int
    due_period: int
    priority: int = 1
    budget_cap: float | None = None
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class StockOnHand:
    product: str
    quantity: int
    available_period: int = 0
    reserved: int = 0
    location: str | None = None

    @property
    def usable(self) -> int:
        return max(0, int(self.quantity) - int(self.reserved))


@dataclass(frozen=True, slots=True)
class SupplierOffer:
    supplier: str
    product: str
    price: float
    currency: str = "USD"
    min_order_quantity: int = 0
    capacity_per_period: int | None = None
    lead_periods: int = 1

    @property
    def usable(self) -> bool:
        return self.price >= 0 and self.capacity_per_period != 0


@dataclass(frozen=True, slots=True)
class Workcenter:
    code: str
    hours_per_period: dict[int, float]
    alternates: tuple[str, ...] = ()
    unavailable_periods: tuple[int, ...] = ()
    calendar: str | None = None


@dataclass(frozen=True, slots=True)
class ProductPlanInput:
    code: str
    bom: BomNode
    workcenter_hours: dict[str, float] = field(default_factory=dict)
    produce_lead_periods: int = 0
    sales_price: float = 0.0
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    status: SolverStatus
    periods: tuple[Period, ...]
    produce: dict[str, dict[int, int]] = field(default_factory=dict)
    buy: dict[str, dict[str, dict[int, int]]] = field(default_factory=dict)
    stock_use: dict[str, dict[int, int]] = field(default_factory=dict)
    unmet: dict[str, int] = field(default_factory=dict)
    fulfilled: dict[str, int] = field(default_factory=dict)
    cost_by_currency: dict[str, float] = field(default_factory=dict)
    workcenter_load: dict[str, dict[int, float]] = field(default_factory=dict)
    component_need: dict[str, int] = field(default_factory=dict)
    objective_value: float | None = None
    assumptions: tuple[str, ...] = ()
    binding_constraints: tuple[str, ...] = ()
    rule_hits: tuple = ()
    detail: str | None = None

    @property
    def solved(self) -> bool:
        return self.status.solved

    @property
    def trustworthy(self) -> bool:
        """A plan resting on an unverified fact is not a complete answer."""
        return self.solved and not self.assumptions

    @property
    def total_units(self) -> int:
        return sum(sum(values.values()) for values in self.produce.values())

    def produce_by_product(self) -> dict[str, int]:
        return {product: sum(values.values()) for product, values in self.produce.items()}

    def total_cost(self) -> float:
        return round(sum(self.cost_by_currency.values()), 6)

    def total_unmet(self) -> int:
        return sum(self.unmet.values())


class RecoveryPlanner:
    """CP-SAT model over periods, supply options, stock and workcenter capacity."""

    def __init__(self, *, periods: list[Period], products: list[ProductPlanInput],
                 demands: list[DemandLine], stock: list[StockOnHand] | None = None,
                 offers: list[SupplierOffer] | None = None,
                 workcenters: list[Workcenter] | None = None,
                 component_supply: dict[str, object] | None = None,
                 time_limit_seconds: float = 20.0) -> None:
        if not periods:
            raise BomError("periods_required")
        if not products:
            raise BomError("no_products")
        if time_limit_seconds <= 0:
            raise BomError("invalid_time_limit")
        codes = {product.code for product in products}
        for demand in demands:
            if demand.product not in codes:
                raise BomError(f"demand_product_unknown:{demand.product}")
            if demand.quantity < 0 or demand.due_period < 0:
                raise BomError("demand_invalid")
        self.periods = sorted(periods, key=lambda item: item.index)
        self.horizon = max(period.index for period in self.periods) + 1
        self.products = {product.code: product for product in products}
        self.demands = list(demands)
        self.stock = list(stock or [])
        self.offers = [offer for offer in (offers or []) if offer.usable]
        self.workcenters = list(workcenters or [])
        self.component_supply = component_supply or {}
        self.time_limit_seconds = float(time_limit_seconds)
        self.infeasible_offers: list[dict] = []
        self._groups = self._build_groups()

    # -- public API ----------------------------------------------------------
    def solve(self, rules=None, *, objective: str = "unmet_then_cost") -> RecoveryPlan:
        try:
            return self._solve(rules, objective=objective)
        except BomError as error:
            status = (SolverStatus.INFEASIBLE if error.code == "plan_infeasible"
                      else SolverStatus.MODEL_INVALID)
            return RecoveryPlan(status, tuple(self.periods), detail=error.code)

    # -- capacity groups (alternate workcenters share one pool) --------------
    def _build_groups(self) -> dict[str, tuple[str, ...]]:
        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(first: str, second: str) -> None:
            left, right = find(first), find(second)
            if left != right:
                parent[right] = left

        for workcenter in self.workcenters:
            find(workcenter.code)
            for alternate in workcenter.alternates:
                union(workcenter.code, alternate)
        groups: dict[str, list[str]] = {}
        for code in list(parent):
            groups.setdefault(find(code), []).append(code)
        return {root: tuple(sorted(members)) for root, members in groups.items()}

    def _hours_for(self, members: tuple[str, ...], period: int):
        """Hours available to a capacity pool in one period.

        Returns ``None`` only when no workcenter in this pool is declared at all.
        A declared but unavailable workcenter contributes zero, which is very
        different from "unconstrained".
        """
        declared = False
        total = 0.0
        for workcenter in self.workcenters:
            if workcenter.code not in members:
                continue
            declared = True
            if period in workcenter.unavailable_periods:
                continue
            total += workcenter.hours_per_period.get(period, 0.0)
        return total if declared else None

    # -- model ---------------------------------------------------------------
    def _solve(self, rules, *, objective: str) -> RecoveryPlan:
        from ortools.sat.python import cp_model

        self.infeasible_offers = []
        model = cp_model.CpModel()
        periods = list(range(self.horizon))
        caps = {code: self._quantity_cap(code) for code in self.products}
        produce = {code: {t: model.new_int_var(0, caps[code], f"prod_{code}_{t}")
                          for t in periods} for code in self.products}

        # -- demand ----------------------------------------------------------
        fulfil: dict[str, object] = {}
        unmet: dict[str, object] = {}
        for demand in self.demands:
            fulfil[demand.order_id] = model.new_int_var(
                0, demand.quantity, f"fulfil_{demand.order_id}")
            unmet[demand.order_id] = model.new_int_var(
                0, demand.quantity, f"unmet_{demand.order_id}")
            model.add(fulfil[demand.order_id] + unmet[demand.order_id] == demand.quantity)

        # -- procurement -----------------------------------------------------
        buy: dict[str, dict[str, dict[int, object]]] = {}
        for code in self.products:
            buy[code] = {}
            for offer in self.offers:
                if offer.product != code:
                    continue
                # An explicit supplier capacity is a hard limit.  A minimum
                # order above it cannot be satisfied by this offer; skipping it
                # reports a shortage instead of inventing extra supply.
                moq = int(offer.min_order_quantity or 0)
                if (offer.capacity_per_period is not None
                        and moq > int(offer.capacity_per_period)):
                    self.infeasible_offers.append({
                        "product": code, "supplier": offer.supplier,
                        "capacity": int(offer.capacity_per_period), "moq": moq,
                        "reason": "moq_exceeds_supplier_capacity"})
                    continue
                limit = (int(offer.capacity_per_period)
                         if offer.capacity_per_period is not None
                         else max(int(caps[code]), moq))
                per_period: dict[int, object] = {}
                for t in periods:
                    if t < offer.lead_periods:
                        continue
                    variable = model.new_int_var(0, limit, f"buy_{code}_{offer.supplier}_{t}")
                    if offer.min_order_quantity > 0:
                        used = model.new_bool_var(
                            f"buy_used_{code}_{offer.supplier}_{t}")
                        model.add(variable >= offer.min_order_quantity).only_enforce_if(used)
                        model.add(variable == 0).only_enforce_if(used.negated())
                    per_period[t] = variable
                if per_period:
                    buy[code][offer.supplier] = per_period

        # -- stock: one pool per stock item, counted once --------------------
        stock_used: dict[int, object] = {}
        for index, item in enumerate(self.stock):
            if item.product not in self.products or item.usable <= 0:
                continue
            stock_used[index] = model.new_int_var(
                0, item.usable, f"stock_{item.product}_{index}")

        # -- cumulative availability -----------------------------------------
        # ``available[p][t]`` is production and procurement that becomes usable in
        # period t.  Stock is not cumulative: it is present from its period on.
        available: dict[str, dict[int, object]] = {}
        for code, product in self.products.items():
            available[code] = {}
            for t in periods:
                terms = [produce[code][s] for s in periods
                         if s + product.produce_lead_periods == t]
                terms += [variable for supplier in buy[code].values()
                          for s, variable in supplier.items() if s == t]
                if not terms:
                    continue
                variable = model.new_int_var(0, caps[code], f"avail_{code}_{t}")
                model.add(variable == sum(terms))
                available[code][t] = variable

        # Cumulative, per product, by due date: several orders compete for one
        # supply pool instead of each being promised the same units.
        for demand in self.demands:
            shared = self._pool_members(demand.product)
            supply = [available[code][t] for code in shared for t in periods
                      if t <= demand.due_period and t in available.get(code, {})]
            supply += [variable for index, variable in stock_used.items()
                       if self.stock[index].product in shared
                       and self.stock[index].available_period <= demand.due_period]
            competing = sum(fulfil[order.order_id] for order in self.demands
                            if order.product in shared
                            and order.due_period <= demand.due_period)
            if supply:
                model.add(competing <= sum(supply))
            else:
                model.add(competing == 0)

        # -- component supply -------------------------------------------------
        component_coeff = {code: explosion(self.products[code].bom)
                           for code in self.products}
        assumptions: list[str] = [f"budget_unknown:{demand.order_id}"
                                  for demand in self.demands
                                  if demand.budget_cap is None]
        need_vars: dict[str, object] = {}
        for component in sorted({name for values in component_coeff.values()
                                 for name in values}):
            need = sum(component_coeff[code].get(component, 0) * produce[code][t]
                       for code in self.products for t in periods)
            need_vars[component] = need
            raw = self.component_supply.get(component)
            if raw is None:
                assumptions.append(f"component_supply_unknown:{component}")
                continue
            supply = normalise_supply(raw)
            if supply.status is SupplyStatus.UNLIMITED:
                continue
            if supply.status is SupplyStatus.UNKNOWN or supply.usable is None:
                assumptions.append(f"component_supply_unknown:{component}")
                continue
            model.add(need <= supply.usable)

        # -- workcenter capacity ---------------------------------------------
        for members in self._groups.values():
            for t in periods:
                hours = self._hours_for(members, t)
                if hours is None:
                    continue
                scale = 10000
                terms = []
                for code, product in self.products.items():
                    per_unit = max(product.workcenter_hours.get(member, 0.0)
                                   for member in members)
                    if per_unit:
                        terms.append(math.ceil(per_unit * scale) * produce[code][t])
                if terms:
                    model.add(sum(terms) <= math.floor(hours * scale))

        # -- formal rules -----------------------------------------------------
        applied_rules = self._apply_rules(model, rules, unmet, buy)

        # -- budgets ----------------------------------------------------------
        # A budget cap belongs to one sales order, not to a currency.  The
        # previous model summed every cap per currency and, if any order in that
        # currency had an unknown cap, skipped the currency entirely: one missing
        # budget silently removed every other order's limit.  To bound an order
        # we must know what it buys, so each order's fulfilment is allocated to
        # the purchase, production and stock units the solver actually chose.
        cost_terms_for_objective: list = []
        for code, suppliers in buy.items():
            for supplier, variables in suppliers.items():
                offer = self._offer(code, supplier)
                cost_terms_for_objective.extend(
                    int(round(offer.price * 100)) * variable
                    for variable in variables.values())

        budgeted = [demand for demand in self.demands
                    if demand.budget_cap is not None]
        allocations = None
        sources: list[dict] = []
        if budgeted:
            allocations, sources = self._apply_budgets(
                model, budgeted, produce, buy, stock_used, fulfil)

        # -- objective --------------------------------------------------------
        weights = {demand.order_id: max(1, 10 - min(9, demand.priority))
                   for demand in self.demands}
        weighted_unmet = sum(unmet[key] * weights[key] for key in unmet)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit_seconds
        # Lexicographic: unmet demand first, then cost.  A fixed weight can decide
        # that a stockout is cheaper than a high unit price.
        model.minimize(weighted_unmet)
        status = solver.solve(model)
        proven_optimal = status == cp_model.OPTIMAL
        if objective != "unmet_only" and status in (cp_model.OPTIMAL,
                                                     cp_model.FEASIBLE):
            best_unmet = int(round(solver.objective_value))
            if proven_optimal:
                model.add(weighted_unmet == best_unmet)
            else:
                model.add(weighted_unmet <= best_unmet)
            model.minimize(sum(cost_terms_for_objective))
            status = solver.solve(model)
            proven_optimal = status == cp_model.OPTIMAL and proven_optimal
        if allocations is not None:
            # A budget that had to be compared across currencies was not
            # actually enforced.  Record that on the plan instead of claiming
            # the cap held.
            demand_by_id = {demand.order_id: demand for demand in self.demands}
            for (order_id, index), variable in allocations.items():
                if not solver.value(variable):
                    continue
                source = sources[index]
                if source["kind"] != "buy":
                    continue
                demand = demand_by_id[order_id]
                if source["currency"] == demand.currency:
                    continue
                note = f"budget_currency_mismatch:{order_id}:{source['currency']}"
                if note not in assumptions:
                    assumptions.append(note)
        resolved = {
            cp_model.OPTIMAL: SolverStatus.OPTIMAL,
            cp_model.FEASIBLE: SolverStatus.FEASIBLE,
            cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
            cp_model.MODEL_INVALID: SolverStatus.MODEL_INVALID,
        }.get(status, SolverStatus.UNKNOWN)
        if resolved is SolverStatus.OPTIMAL and not proven_optimal:
            resolved = SolverStatus.FEASIBLE
        if resolved is SolverStatus.INFEASIBLE:
            return RecoveryPlan(resolved, tuple(self.periods),
                                detail="no feasible schedule",
                                rule_hits=tuple(applied_rules))
        if not resolved.solved:
            return RecoveryPlan(resolved, tuple(self.periods),
                                detail=f"solver_status:{status}",
                                assumptions=tuple(assumptions),
                                rule_hits=tuple(applied_rules))
        return self._materialise(solver, resolved, produce, buy, stock_used,
                                 fulfil, unmet, need_vars, assumptions,
                                 applied_rules, rules, objective)

    # -- result --------------------------------------------------------------
    def _materialise(self, solver, status, produce, buy, stock_used, fulfil, unmet,
                     need_vars, assumptions, applied_rules, rules,
                     objective) -> RecoveryPlan:
        periods = list(range(self.horizon))
        produce_values = {
            code: {t: int(solver.value(produce[code][t])) for t in periods
                   if solver.value(produce[code][t])}
            for code in self.products}
        buy_values = {
            code: {supplier: {t: int(solver.value(variable))
                              for t, variable in variables.items()
                              if solver.value(variable)}
                   for supplier, variables in suppliers.items()}
            for code, suppliers in buy.items()}
        stock_values: dict[str, dict[int, int]] = {}
        for index, variable in stock_used.items():
            used = int(solver.value(variable))
            if not used:
                continue
            item = self.stock[index]
            stock_values.setdefault(item.product, {})[item.available_period] = (
                stock_values.get(item.product, {}).get(item.available_period, 0) + used)
        unmet_values = {key: int(solver.value(value)) for key, value in unmet.items()}
        fulfilled_values = {key: int(solver.value(value))
                            for key, value in fulfil.items()}
        cost_by_currency: dict[str, float] = {}
        for code, suppliers in buy_values.items():
            for supplier, variables in suppliers.items():
                offer = self._offer(code, supplier)
                spent = sum(variables.values()) * offer.price
                cost_by_currency[offer.currency] = (
                    cost_by_currency.get(offer.currency, 0.0) + spent)
        load: dict[str, dict[int, float]] = {}
        for members in self._groups.values():
            root = members[0]
            for t in periods:
                used = sum(
                    sum(product.workcenter_hours.get(member, 0.0) for member in members)
                    * int(solver.value(produce[code][t]))
                    for code, product in self.products.items())
                if used:
                    load.setdefault(root, {})[t] = round(used, 6)
        component_need = {component: int(solver.value(variable))
                          for component, variable in need_vars.items()}
        binding = self._binding(component_need, load, buy_values)
        total_unmet = sum(unmet_values.values())
        facts = {
            "unmet": total_unmet,
            "feasible": total_unmet == 0,
            "cost": sum(cost_by_currency.values()),
            "produce": sum(sum(values.values()) for values in produce_values.values()),
        }
        hits = tuple(rules.evaluate(facts)) if rules is not None else ()
        return RecoveryPlan(
            status, tuple(self.periods), produce_values, buy_values, stock_values,
            unmet_values, fulfilled_values,
            {key: round(value, 6) for key, value in cost_by_currency.items()},
            load, component_need, float(solver.objective_value),
            tuple(assumptions), binding, hits)

    def _apply_rules(self, model, rules, unmet, buy) -> list[str]:
        if rules is None:
            return []
        applied: list[str] = []
        for rule in getattr(rules, "rules", ()):
            condition = rule.condition if isinstance(rule.condition, dict) else {}
            for operator, bounded in (("lte", True), ("gte", False)):
                candidate = condition.get(operator)
                if not isinstance(candidate, dict):
                    continue
                field_name = candidate.get("field")
                value = candidate.get("value")
                if not isinstance(value, (int, float)):
                    continue
                if field_name == "unmet":
                    expression = sum(unmet.values()) if unmet else 0
                elif field_name == "spend":
                    terms = []
                    for code, suppliers in buy.items():
                        for supplier, variables in suppliers.items():
                            offer = self._offer(code, supplier)
                            for variable in variables.values():
                                terms.append(int(round(offer.price * 100)) * variable)
                    expression = sum(terms) if terms else 0
                    value = int(round(float(value) * 100))
                else:
                    continue
                model.add(expression <= int(value) if bounded
                          else expression >= int(value))
                applied.append(rule.name)
        return applied

    def _apply_budgets(self, model, budgeted, produce, buy, stock_used, fulfil
                       ) -> tuple[dict[tuple[str, int], object], list[dict]]:
        """Budget each order's own purchases, without letting a missing cap help.

        The unit of a budget is one sales order.  A purchase variable is shared
        by every order for the same product, so the model first allocates the
        supply it chose to the orders that consume it (a small transportation
        model), then caps each budgeted order's same-currency purchase value.
        An order with an unknown cap simply has no cap: it cannot lend or remove
        another order's limit.
        """
        products = {demand.product for demand in budgeted}
        covered = [demand for demand in self.demands if demand.product in products]
        sources: list[dict] = []
        by_product: dict[str, list[int]] = {code: [] for code in products}
        for code in products:
            for supplier, variables in buy.get(code, {}).items():
                offer = self._offer(code, supplier)
                for period, variable in variables.items():
                    by_product[code].append(len(sources))
                    sources.append({"kind": "buy", "product": code,
                                    "variable": variable, "period": period,
                                    "upper": max(int(offer.capacity_per_period
                                                     or self._quantity_cap(code)),
                                                 int(offer.min_order_quantity
                                                     or 0)),
                                    "price": offer.price,
                                    "currency": offer.currency})
            product = self.products[code]
            for period, variable in produce.get(code, {}).items():
                by_product[code].append(len(sources))
                sources.append({"kind": "produce", "product": code,
                                "variable": variable,
                                "period": period + product.produce_lead_periods,
                                "upper": self._quantity_cap(code)})
            for index, variable in stock_used.items():
                item = self.stock[index]
                if item.product != code:
                    continue
                by_product[code].append(len(sources))
                sources.append({"kind": "stock", "product": code,
                                "variable": variable, "period": item.available_period,
                                "upper": max(0, item.usable)})

        # A purchase that serves exactly one order may keep a minimum-order
        # surplus as stock.  The budget still charges the full purchase: buying
        # five units for a demand of three spends five.  A purchase shared by
        # several orders keeps the exact allocation so one order cannot consume
        # another's units.
        eligible: dict[int, list[str]] = {}
        for index, source in enumerate(sources):
            eligible[index] = [demand.order_id for demand in covered
                               if demand.product == source["product"]
                               and source["period"] <= demand.due_period]
        single_buy = {index for index, source in enumerate(sources)
                      if source["kind"] == "buy" and len(eligible[index]) == 1}
        single_buy_order = {index: eligible[index][0] for index in single_buy}

        allocations: dict[tuple[str, int], object] = {}
        for demand in covered:
            chosen: list = []
            for index in by_product.get(demand.product, ()):
                source = sources[index]
                if source["period"] > demand.due_period:
                    continue
                upper = min(int(source["upper"]), int(demand.quantity))
                if upper <= 0:
                    continue
                variable = model.new_int_var(
                    0, upper, f"alloc_{demand.order_id}_{index}")
                allocations[(demand.order_id, index)] = variable
                chosen.append(variable)
            # The order must be fulfilled from sources it can actually use; a
            # purchase allocated to nobody cannot be used to satisfy it.
            model.add(sum(chosen) == fulfil[demand.order_id])
            if demand.budget_cap is None:
                continue
            cents = []
            for index in by_product.get(demand.product, ()):
                source = sources[index]
                if source["kind"] != "buy" or source["currency"] != demand.currency:
                    # An amount in another currency cannot be compared with this
                    # cap; using one would silently invent an exchange rate.
                    continue
                price_cents = int(round(float(source["price"]) * 100))
                if index in single_buy:
                    if single_buy_order[index] == demand.order_id:
                        # Charge the whole purchase, not only the allocated units:
                        # MOQ surplus is real spend even when it becomes stock.
                        cents.append(price_cents * source["variable"])
                    continue
                variable = allocations.get((demand.order_id, index))
                if variable is not None:
                    cents.append(price_cents * variable)
            if cents:
                model.add(sum(cents) <= int(round(float(demand.budget_cap) * 100)))
        for index, source in enumerate(sources):
            used = [variable for (order_id, source_index), variable in allocations.items()
                    if source_index == index]
            if not used:
                continue
            if index in single_buy:
                # The surplus stays as stock instead of blocking a legal MOQ buy.
                model.add(sum(used) <= source["variable"])
            else:
                # Every produced, purchased or consumed unit backs a real order.
                # Without this equality the solver may over-produce for free
                # simply because the allocation variables changed its search.
                model.add(sum(used) == source["variable"])
        return allocations, sources

    # -- helpers -------------------------------------------------------------
    def _offer(self, code: str, supplier: str) -> SupplierOffer:
        for offer in self.offers:
            if offer.product == code and offer.supplier == supplier:
                return offer
        raise BomError("offer_missing")

    def _pool_members(self, code: str) -> frozenset[str]:
        """Products that compete for the same finished-goods supply pool.

        Cross-product competition happens through the shared component supply and
        shared workcenter constraints, which are modelled separately; inventing a
        larger final-product pool here would double-count the same capacity.
        """
        return frozenset({code})

    def _quantity_cap(self, code: str) -> int:
        total = sum(demand.quantity for demand in self.demands if demand.product == code)
        for item in self.stock:
            if item.product == code:
                total += item.usable
        for offer in self.offers:
            if offer.product == code and offer.capacity_per_period:
                total += offer.capacity_per_period * self.horizon
        # The availability variable must be able to hold a legal minimum order
        # even when the demand alone is smaller than that minimum.
        minimums = [int(offer.min_order_quantity or 0) for offer in self.offers
                    if offer.product == code]
        if minimums:
            total = max(total, max(minimums))
        return max(1, total)

    def _binding(self, component_need: dict[str, int],
                 load: dict[str, dict[int, float]],
                 buy: dict[str, dict[str, dict[int, int]]]) -> tuple[str, ...]:
        binding: list[str] = []
        for component, need in component_need.items():
            raw = self.component_supply.get(component)
            if raw is None:
                continue
            usable = normalise_supply(raw).usable
            if usable is not None and need >= usable:
                binding.append(f"component:{component}")
        for members in self._groups.values():
            for t, used in load.get(members[0], {}).items():
                hours = self._hours_for(members, t)
                if hours is not None and used >= hours - 1e-9:
                    binding.append(f"workcenter:{members[0]}:period{t}")
        for code, suppliers in buy.items():
            for supplier, variables in suppliers.items():
                offer = self._offer(code, supplier)
                if offer.capacity_per_period:
                    for t, value in variables.items():
                        if value >= offer.capacity_per_period:
                            binding.append(f"supplier_capacity:{code}:{supplier}:{t}")
        return tuple(dict.fromkeys(binding))


def plan_to_dict(plan: RecoveryPlan) -> dict:
    """Serialise a plan for an approval payload or an API response."""
    return {
        "status": plan.status.value,
        "solved": plan.solved,
        "trustworthy": plan.trustworthy,
        "periods": [{"index": period.index, "label": period.label}
                    for period in plan.periods],
        "produce": plan.produce,
        "buy": plan.buy,
        "stock_use": plan.stock_use,
        "unmet": plan.unmet,
        "fulfilled": plan.fulfilled,
        "cost_by_currency": plan.cost_by_currency,
        "workcenter_load": plan.workcenter_load,
        "component_need": plan.component_need,
        "objective_value": plan.objective_value,
        "assumptions": list(plan.assumptions),
        "binding_constraints": list(plan.binding_constraints),
        "rule_hits": [{"name": hit.name, "version": hit.version,
                       "source_ref": hit.source_ref} for hit in plan.rule_hits],
        "detail": plan.detail,
    }
