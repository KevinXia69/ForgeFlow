"""Replacement planning for a cancelled supply commitment.

This is the supply-side counterpart of :mod:`forgeflow.planning.recovery`.  A
recovery plan answers "how do we fulfil demand with production and purchases";
this planner answers "the supplier cancelled a commitment -- what do we buy now,
from whom, and by when".  They are deliberately separate models: the quantities
in a replacement plan are driven by one lost commitment, and the only decisions
are which open commitment to rely on, how much stock to consume, and which
supplier offers to accept.

Facts that are not supplied stay visible:

* a supplier lead time longer than the remaining horizon is reported as an
  infeasible offer, not silently dropped;
* a minimum order quantity that exceeds the supplier's capacity makes that
  offer infeasible instead of inventing extra supply;
* a shortfall is reported as ``unmet``; the plan is then *not* executable and
  action compilation refuses it.

The output is converted into Odoo actions through the same
:func:`forgeflow.planning.actions.compile_actions` used by the recovery path, so
every created order carries the operation that produced it and the cancelled
commitment it replaces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from .actions import ActionCompileError, compile_actions
from .multi_bom import SolverStatus


class ReplacementError(ValueError):
    """A bounded, machine-readable refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Period:
    """One decision period; the horizon is short and deliberately daily."""

    index: int
    label: str
    start: date | None = None
    end: date | None = None


@dataclass(frozen=True, slots=True)
class ReplacementDemand:
    """The lost commitment that must be restored."""

    product_id: int
    product_code: str
    quantity: int
    due_period: int
    origin_refs: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.product_id <= 0:
            raise ReplacementError("product_required")
        if not self.product_code:
            raise ReplacementError("product_code_required")
        if self.quantity < 0:
            raise ReplacementError("quantity_invalid")
        if self.due_period < 0:
            raise ReplacementError("due_period_invalid")


@dataclass(frozen=True, slots=True)
class OpenCommitment:
    """An existing open purchase line that can still cover part of the loss.

    It is *already bought*: using it costs nothing now, so the planner only
    checks that it arrives before the due period.
    """

    line_id: int
    partner_id: int
    quantity: int
    available_period: int = 0
    unit_price: float = 0.0

    def __post_init__(self) -> None:
        if self.line_id <= 0:
            raise ReplacementError("commitment_line_required")
        if self.quantity < 0:
            raise ReplacementError("commitment_quantity_invalid")


@dataclass(frozen=True, slots=True)
class PurchaseOffer:
    """A supplier offer observed in the ERP's supplier pricelist."""

    supplier_id: int
    supplier_ref: str
    price: float
    currency: str = "USD"
    min_order_quantity: int = 0
    capacity_per_period: int | None = None
    lead_periods: int = 0

    def __post_init__(self) -> None:
        if self.supplier_id <= 0:
            raise ReplacementError("supplier_required")
        if not self.supplier_ref:
            raise ReplacementError("supplier_ref_required")
        if self.price < 0 or not math.isfinite(float(self.price)):
            raise ReplacementError("offer_price_invalid")
        if self.min_order_quantity < 0:
            raise ReplacementError("offer_minimum_invalid")
        if self.capacity_per_period is not None and self.capacity_per_period < 0:
            raise ReplacementError("offer_capacity_invalid")
        if self.lead_periods < 0:
            raise ReplacementError("offer_lead_time_invalid")


@dataclass(frozen=True, slots=True)
class ReplacementPlan:
    status: SolverStatus
    periods: tuple[Period, ...]
    demand: ReplacementDemand
    buy: dict[str, dict[int, int]] = field(default_factory=dict)
    stock_use: int = 0
    committed_use: int = 0
    surplus: int = 0
    unmet: int = 0
    cost: float = 0.0
    currency: str = "USD"
    binding_constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    infeasible_offers: tuple[dict[str, object], ...] = ()
    offers: tuple[PurchaseOffer, ...] = ()
    detail: str | None = None

    @property
    def solved(self) -> bool:
        return self.status.solved

    @property
    def covered(self) -> int:
        return (self.stock_use + self.committed_use
                + sum(sum(values.values()) for values in self.buy.values()))

    @property
    def executable(self) -> bool:
        """Only a solved plan with no shortfall may become an approval."""
        return self.solved and self.unmet == 0

    def buy_by_supplier(self) -> dict[str, int]:
        return {supplier: sum(values.values())
                for supplier, values in self.buy.items() if sum(values.values()) > 0}


class ReplacementPlanner:
    """CP-SAT over lead time, minimum quantity, capacity, stock and commitments."""

    def __init__(self, *, periods: list[Period], demand: ReplacementDemand,
                 stock: int = 0, commitments: list[OpenCommitment] | None = None,
                 offers: list[PurchaseOffer] | None = None,
                 time_limit_seconds: float = 10.0) -> None:
        if not periods:
            raise ReplacementError("periods_required")
        if time_limit_seconds <= 0:
            raise ReplacementError("invalid_time_limit")
        if stock < 0:
            raise ReplacementError("stock_invalid")
        if demand.due_period > max(period.index for period in periods):
            raise ReplacementError("due_period_outside_horizon")
        self.periods = sorted(periods, key=lambda item: item.index)
        self.demand = demand
        self.stock = int(stock)
        self.commitments = list(commitments or [])
        self.offers = list(offers or [])
        self.time_limit_seconds = float(time_limit_seconds)
        self.infeasible_offers: list[dict[str, object]] = []

    # -- public API ----------------------------------------------------------
    def solve(self) -> ReplacementPlan:
        from ortools.sat.python import cp_model

        need = int(self.demand.quantity)
        due = int(self.demand.due_period)
        periods = [period.index for period in self.periods if period.index <= due]

        committed = sum(item.quantity for item in self.commitments
                        if item.available_period <= due)
        committed_use = min(need, max(0, committed))
        stock_cap = min(need, self.stock)

        model = cp_model.CpModel()
        stock_use = model.new_int_var(0, stock_cap, "stock_use")
        # A minimum order quantity may force a purchase larger than the loss;
        # that surplus is explicit instead of being hidden in the equation.
        surplus_cap = max(1, need) + sum(
            max(0, int(offer.min_order_quantity)) for offer in self.offers)
        surplus = model.new_int_var(0, surplus_cap, "surplus")
        unmet = model.new_int_var(0, need, "unmet")
        buy: dict[str, dict[int, object]] = {}
        buy_price: dict[str, dict[int, int]] = {}
        for offer in self.offers:
            if offer.lead_periods > due:
                self.infeasible_offers.append({
                    "supplier": offer.supplier_ref, "reason": "lead_time_misses_due_date",
                    "lead_periods": offer.lead_periods, "due_period": due})
                continue
            capacity = offer.capacity_per_period
            if capacity is not None and capacity < offer.min_order_quantity:
                self.infeasible_offers.append({
                    "supplier": offer.supplier_ref, "reason": "moq_exceeds_capacity",
                    "capacity": capacity, "min_order_quantity": offer.min_order_quantity})
                continue
            limit = (int(capacity) if capacity is not None
                     else max(need, int(offer.min_order_quantity), 1))
            per_period: dict[int, object] = {}
            prices: dict[int, int] = {}
            for period in periods:
                if period < offer.lead_periods:
                    continue
                variable = model.new_int_var(
                    0, limit, f"buy_{offer.supplier_ref}_{period}")
                if offer.min_order_quantity > 0:
                    used = model.new_bool_var(
                        f"buy_used_{offer.supplier_ref}_{period}")
                    model.add(variable >= int(offer.min_order_quantity)).only_enforce_if(used)
                    model.add(variable == 0).only_enforce_if(used.negated())
                per_period[period] = variable
                prices[period] = int(round(offer.price * 100))
            if per_period:
                buy[offer.supplier_ref] = per_period
                buy_price[offer.supplier_ref] = prices

        supply = [stock_use, committed_use, unmet]
        for per_period in buy.values():
            supply.extend(per_period.values())
        model.add(sum(supply) == need + surplus)

        cost_terms = [buy_price[supplier][period] * variable
                      for supplier, per_period in buy.items()
                      for period, variable in per_period.items()]
        model.minimize(unmet)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit_seconds
        status = solver.solve(model)
        proven = status == cp_model.OPTIMAL
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and cost_terms:
            best_unmet = int(round(solver.objective_value))
            if proven:
                model.add(unmet == best_unmet)
            else:
                model.add(unmet <= best_unmet)
            model.minimize(sum(cost_terms))
            second = solver.solve(model)
            if second in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                status = second
                proven = second == cp_model.OPTIMAL and proven
        resolved = _status(status, proven)
        if not resolved.solved:
            return ReplacementPlan(
                resolved, tuple(self.periods), self.demand,
                assumptions=(), infeasible_offers=tuple(self.infeasible_offers),
                currency=self._currency())

        chosen: dict[str, dict[int, int]] = {}
        for supplier, per_period in buy.items():
            values = {period: int(solver.value(variable))
                      for period, variable in per_period.items()
                      if int(solver.value(variable)) > 0}
            if values:
                chosen[supplier] = values
        plan = ReplacementPlan(
            resolved, tuple(self.periods), self.demand,
            buy=chosen,
            stock_use=int(solver.value(stock_use)),
            committed_use=committed_use,
            surplus=int(solver.value(surplus)),
            unmet=int(solver.value(unmet)),
            cost=round(sum(buy_price[supplier][period] * quantity
                           for supplier, values in chosen.items()
                           for period, quantity in values.items()) / 100.0, 2),
            currency=self._currency(),
            assumptions=(),
            infeasible_offers=tuple(self.infeasible_offers),
            offers=tuple(self.offers),
        )
        binding = self._binding(plan, buy, solver)
        return ReplacementPlan(
            plan.status, plan.periods, plan.demand, plan.buy, plan.stock_use,
            plan.committed_use, plan.surplus, plan.unmet, plan.cost, plan.currency,
            binding_constraints=binding, assumptions=plan.assumptions,
            infeasible_offers=plan.infeasible_offers, offers=plan.offers, detail=None)

    # -- helpers -------------------------------------------------------------
    def _currency(self) -> str:
        currencies = {offer.currency for offer in self.offers}
        return currencies.pop() if len(currencies) == 1 else "USD"

    def _binding(self, plan: ReplacementPlan, variables: dict[str, dict[int, object]],
                 solver) -> tuple[str, ...]:
        binding: list[str] = []
        if plan.unmet > 0:
            binding.append(f"unmet:{self.demand.product_code}")
        if self.stock and plan.stock_use >= min(self.demand.quantity, self.stock):
            binding.append("stock")
        for offer in self.offers:
            per_period = variables.get(offer.supplier_ref) or {}
            if not per_period:
                continue
            for period, variable in per_period.items():
                quantity = int(solver.value(variable))
                if offer.capacity_per_period is not None and (
                        quantity >= int(offer.capacity_per_period)):
                    binding.append(
                        f"supplier_capacity:{offer.supplier_ref}:period{period}")
                if offer.min_order_quantity and quantity == int(
                        offer.min_order_quantity):
                    binding.append(f"min_order_quantity:{offer.supplier_ref}")
        return tuple(dict.fromkeys(binding))


def _status(status: int, proven: bool) -> SolverStatus:
    from ortools.sat.python import cp_model

    mapping = {
        cp_model.OPTIMAL: SolverStatus.OPTIMAL,
        cp_model.FEASIBLE: SolverStatus.FEASIBLE,
        cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
        cp_model.MODEL_INVALID: SolverStatus.MODEL_INVALID,
    }
    resolved = mapping.get(status, SolverStatus.UNKNOWN)
    if resolved is SolverStatus.OPTIMAL and not proven:
        return SolverStatus.FEASIBLE
    return resolved


def replacement_actions(plan: ReplacementPlan, *, origin_ref: str,
                        unit: str = "Units",
                        identity_suffix: str = "") -> dict[str, object]:
    """Compile a solved replacement plan into approved-shape Odoo actions.

    The compilation goes through :func:`compile_actions` so the same provenance
    rules apply as for a recovery plan: every created order names the cancelled
    commitment it replaces, and a plan with a shortfall or an unconfirmed fact
    cannot be compiled at all.
    """
    if not plan.executable:
        raise ActionCompileError(
            "plan_has_unmet_demand" if plan.solved else "plan_status_not_executable")
    offers = {offer.supplier_ref: offer for offer in plan.offers}
    operations: list[dict[str, object]] = []
    for supplier, per_period in sorted(plan.buy.items()):
        offer = offers.get(supplier)
        if offer is None:
            raise ActionCompileError("offer_missing_for_plan")
        for period, quantity in sorted(per_period.items()):
            if quantity <= 0:
                continue
            start = plan.periods[period].start
            after: dict[str, object] = {
                "quantity": int(quantity),
                "product_id": plan.demand.product_id,
                "partner_id": offer.supplier_id,
                "unit_price": round(float(offer.price), 4),
                "product_code": plan.demand.product_code,
                "origin_refs": [{"model": "purchase.order", "id": _origin_id(origin_ref)}],
            }
            if start is not None:
                after["date_planned"] = f"{start.isoformat()} 09:00:00"
            operations.append({
                "operation_id": f"buy-{supplier}-p{period}{identity_suffix}",
                "kind": "purchase_order",
                "action": "create",
                "target": {},
                "before": {},
                "after": after,
            })
    changes = {
        "semantics": "target_state_to_change_set",
        "operations": operations,
        "projected_state": {"checks": {
            "demand_covered": plan.unmet == 0,
            "supply_known": bool(operations),
            "stock_and_commitments_within_need": (
                plan.stock_use + plan.committed_use <= plan.demand.quantity),
            "minimum_order_surplus_is_explicit": plan.surplus >= 0,
        }},
    }
    plan_document = {
        "trace": {"solve_calls": [{"status": plan.status.value}]},
        "assumptions": list(plan.assumptions),
    }
    return compile_actions(changes, plan=plan_document, currency=plan.currency,
                          unit=unit)


def _origin_id(origin_ref: str) -> int:
    model, _, identifier = str(origin_ref).partition(":")
    if model != "purchase.order" or not identifier.isdigit():
        raise ActionCompileError("origin_ref_invalid")
    return int(identifier)


def plan_to_dict(plan: ReplacementPlan) -> dict[str, object]:
    """Serialise a replacement plan for an approval payload or evidence file."""
    return {
        "status": plan.status.value,
        "solved": plan.solved,
        "executable": plan.executable,
        "periods": [{"index": period.index, "label": period.label,
                     "start": period.start.isoformat() if period.start else None}
                    for period in plan.periods],
        "product_id": plan.demand.product_id,
        "product_code": plan.demand.product_code,
        "required_quantity": plan.demand.quantity,
        "buy": {supplier: {str(period): quantity for period, quantity in values.items()}
                for supplier, values in plan.buy.items()},
        "stock_use": plan.stock_use,
        "committed_use": plan.committed_use,
        "surplus": plan.surplus,
        "unmet": plan.unmet,
        "cost": plan.cost,
        "currency": plan.currency,
        "binding_constraints": list(plan.binding_constraints),
        "assumptions": list(plan.assumptions),
        "infeasible_offers": [dict(item) for item in plan.infeasible_offers],
    }


def periods_until(today: date, due: date, *, minimum: int = 3) -> list[Period]:
    """Daily periods from ``today`` through ``due`` (at least three periods).

    A due date in the past yields one usable period: the commitment is already
    late, and pretending there is more time would hide that fact.
    """
    horizon = max(0, (due - today).days)
    span = max(minimum - 1, horizon)
    periods = []
    for index in range(span + 1):
        start = date.fromordinal(today.toordinal() + index)
        periods.append(Period(index, start.isoformat(), start, start))
    return periods


def parse_odoo_datetime(value: object) -> datetime | None:
    """Parse the two datetime shapes Odoo returns, or say it is unknown."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
