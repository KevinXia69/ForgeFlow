"""Multi-level BOM explosion with shared-resource CP-SAT capacity planning.

Two contracts matter more than the objective:

* **A solver timeout is not a proof of infeasibility.**  ``OPTIMAL``, ``FEASIBLE``,
  ``INFEASIBLE``, ``UNKNOWN`` and ``MODEL_INVALID`` are distinct outcomes.
* **A missing supply fact is not infinite supply.**  A component is either
  explicitly unlimited, explicitly limited, or unknown -- and an unknown becomes a
  recorded assumption on the plan rather than a silent free resource.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum


class BomError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SolverStatus(StrEnum):
    """The only outcomes a solve may report."""

    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"
    MODEL_INVALID = "MODEL_INVALID"

    @property
    def solved(self) -> bool:
        return self in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}

    @property
    def proves_infeasible(self) -> bool:
        return self is SolverStatus.INFEASIBLE


class SupplyStatus(StrEnum):
    """Three states, deliberately not two."""

    #: Nobody looked, or the lookup failed.  Never treated as a free resource.
    UNKNOWN = "unknown"
    #: A bounded quantity is known.
    LIMITED = "limited"
    #: The source is genuinely unlimited (a policy decision, not a guess).
    UNLIMITED = "unlimited"


@dataclass(frozen=True, slots=True)
class BomNode:
    code: str
    per_parent: int = 1
    children: tuple["BomNode", ...] = ()


@dataclass(frozen=True, slots=True)
class Product:
    code: str
    demand: int
    bom: BomNode
    resource_hours: dict[str, float] = field(default_factory=dict)
    #: Earliest period this product can be produced in (lead time in periods).
    lead_periods: int = 0
    unit_price: float = 0.0
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class Capacity:
    resource: str
    hours: float
    #: Resources listed here share one capacity pool (alternate workcenters).
    alternates: tuple[str, ...] = ()
    period: int = 0


@dataclass(frozen=True, slots=True)
class ComponentSupply:
    code: str
    available: int | None = None
    status: SupplyStatus = SupplyStatus.LIMITED
    available_period: int = 0
    on_hand: int = 0
    reserved: int = 0

    @property
    def usable(self) -> int | None:
        if self.status is SupplyStatus.UNKNOWN or self.available is None:
            return None
        return max(0, int(self.available))


@dataclass(frozen=True, slots=True)
class SupplierOffer:
    """A procurement option: price, minimum quantity and a lead time."""

    supplier: str
    product: str
    price: float
    currency: str = "USD"
    min_order_quantity: int = 0
    capacity_per_period: int | None = None
    lead_periods: int = 1


@dataclass(frozen=True, slots=True)
class PlanResult:
    produced: dict[str, int]
    shortfall: dict[str, int]
    component_need: dict[str, int]
    resource_use: dict[str, float]
    feasible: bool
    rule_hits: tuple = ()
    status: SolverStatus = SolverStatus.OPTIMAL
    assumptions: tuple[str, ...] = ()
    objective_value: float | None = None
    produced_by_period: dict[str, dict[int, int]] = field(default_factory=dict)
    procurement: dict[str, dict[str, int]] = field(default_factory=dict)
    binding_constraints: tuple[str, ...] = ()

    @property
    def solved(self) -> bool:
        return self.status.solved

    @property
    def trustworthy(self) -> bool:
        """A plan that rests on an unverified fact is not a complete answer."""
        return self.solved and not self.assumptions


@dataclass(frozen=True, slots=True)
class SolveResult:
    """Rich solve outcome.  Never raises for an unsolved model."""

    status: SolverStatus
    plan: PlanResult | None = None
    detail: str | None = None
    wall_seconds: float | None = None
    unknowns: tuple[str, ...] = ()


def _require_nonnegative_int(value: int, code: str) -> int:
    if type(value) is not int or value < 0:
        raise BomError(code)
    return value


def _explode(node: BomNode, multiplier: int,
             seen: frozenset[str]) -> dict[str, int]:
    _require_nonnegative_int(multiplier, "bom_invalid_quantity")
    if node.code in seen:
        raise BomError("bom_cycle")
    _require_nonnegative_int(node.per_parent, "bom_invalid_quantity")
    result: dict[str, int] = {}
    if not node.children:
        result[node.code] = multiplier
    next_seen = seen | {node.code}
    for child in node.children:
        for code, quantity in _explode(
                child, multiplier * child.per_parent, next_seen).items():
            result[code] = result.get(code, 0) + quantity
    return result


def explosion(node: BomNode, multiplier: int = 1,
              seen: frozenset[str] = frozenset()) -> dict[str, int]:
    """Aggregate per-unit component demand; reject cycles and invalid quantities."""
    result = _explode(node, multiplier, seen)
    result.pop(node.code, None)
    return result


def _shortfall_limit(rules) -> int | None:
    limit = None
    for rule in getattr(rules, "rules", ()):
        condition = rule.condition
        candidate = condition.get("lte") if isinstance(condition, dict) else None
        if (isinstance(candidate, dict) and candidate.get("field") == "shortfall"
                and isinstance(candidate.get("value"), int) and candidate["value"] >= 0):
            limit = candidate["value"] if limit is None else min(limit, candidate["value"])
    return limit


def normalise_supply(value) -> ComponentSupply:
    """Accept an int, a dict or a ``ComponentSupply`` and say what it means."""
    if isinstance(value, ComponentSupply):
        return value
    if value is None:
        return ComponentSupply("", None, SupplyStatus.UNKNOWN)
    if isinstance(value, bool):
        raise BomError("component_invalid_supply")
    if isinstance(value, int):
        return ComponentSupply("", value, SupplyStatus.LIMITED)
    raise BomError("component_invalid_supply")


class MultiBomPlanner:
    def __init__(self, products: list[Product], capacities: list[Capacity],
                 component_supply: dict[str, object] | None = None, *,
                 time_limit_seconds: float = 10.0,
                 objective: str = "shortfall_then_cost") -> None:
        if not products:
            raise BomError("no_products")
        if time_limit_seconds <= 0:
            raise BomError("invalid_time_limit")
        self.products = products
        self.capacities = capacities
        self.component_supply = component_supply or {}
        self.time_limit_seconds = float(time_limit_seconds)
        self.objective = objective

    # -- public API ----------------------------------------------------------
    def solve(self, rules=None, *, enforce_rules: bool = False) -> PlanResult:
        """Backward-compatible entry point.

        Raises ``BomError`` only for a model that cannot be built or a model that
        is *proven* infeasible.  An unknown outcome raises ``plan_unknown`` so that
        a timeout can never read as "there is no plan".
        """
        result = self.solve_plan(rules, enforce_rules=enforce_rules)
        if result.status is SolverStatus.MODEL_INVALID:
            raise BomError("plan_model_invalid")
        if result.status is SolverStatus.INFEASIBLE:
            raise BomError("plan_infeasible")
        if result.status is SolverStatus.UNKNOWN:
            raise BomError("plan_unknown")
        if result.plan is None:
            raise BomError("plan_unknown")
        return result.plan

    def solve_plan(self, rules=None, *, enforce_rules: bool = False) -> SolveResult:
        try:
            return self._solve(rules, enforce_rules=enforce_rules)
        except BomError as error:
            if error.code == "plan_infeasible":
                return SolveResult(SolverStatus.INFEASIBLE, detail=error.code)
            return SolveResult(SolverStatus.MODEL_INVALID, detail=error.code)

    # -- model ---------------------------------------------------------------
    def _solve(self, rules=None, *, enforce_rules: bool = False) -> SolveResult:
        from ortools.sat.python import cp_model

        model = cp_model.CpModel()
        quantity: dict[str, object] = {}
        shortfall: dict[str, object] = {}
        component_coeff: dict[str, dict[str, int]] = {}
        assumptions: list[str] = []
        for product in self.products:
            _require_nonnegative_int(product.demand, "product_invalid_demand")
            quantity[product.code] = model.new_int_var(
                0, product.demand, f"q_{product.code}")
            shortfall[product.code] = model.new_int_var(
                0, product.demand, f"s_{product.code}")
            model.add(quantity[product.code] + shortfall[product.code] == product.demand)
            component_coeff[product.code] = explosion(product.bom)
        for component in sorted({code for values in component_coeff.values()
                                 for code in values}):
            # Each product contributes its own coefficient for this component.
            # Using the leaked `product` variable here silently applied the last
            # product's BOM to every product.
            need = sum(component_coeff[p.code].get(component, 0) * quantity[p.code]
                       for p in self.products)
            raw = self.component_supply.get(component)
            if raw is None:
                # Not "no limit": nobody supplied the fact.
                assumptions.append(f"component_supply_unknown:{component}")
                continue
            supply = normalise_supply(raw)
            if supply.status is SupplyStatus.UNKNOWN:
                assumptions.append(f"component_supply_unknown:{component}")
                continue
            if supply.status is SupplyStatus.UNLIMITED:
                continue
            usable = supply.usable
            if usable is None:
                assumptions.append(f"component_supply_unknown:{component}")
                continue
            model.add(need <= usable)
        for capacity in self.capacities:
            if capacity.hours < 0:
                raise BomError("capacity_invalid")
            members = (capacity.resource, *capacity.alternates)
            # Conservative fixed-point: round usage up, capacity down.
            scale = 10000
            model.add(sum(
                math.ceil(product.resource_hours.get(member, 0.0) * scale) *
                quantity[product.code]
                for product in self.products for member in members)
                <= math.floor(capacity.hours * scale))
        if enforce_rules and rules is not None:
            limit = _shortfall_limit(rules)
            if limit is not None:
                model.add(sum(shortfall.values()) <= limit)
        gap_expression = sum(shortfall[p.code] for p in self.products)
        cost_expression = sum(int(round(product.unit_price * 100)) * quantity[product.code]
                              for product in self.products)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit_seconds
        # Lexicographic, not a fixed weight: with a fixed price/shortfall weight a
        # high enough unit price makes a stockout look cheaper than production.
        model.minimize(gap_expression)
        status = solver.solve(model)
        proven_optimal = status == cp_model.OPTIMAL
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and self.objective == (
                "shortfall_then_cost"):
            best_gap = int(round(solver.objective_value))
            # Pin the achieved gap (equality when it was proven) so cost can only
            # decide among plans that preserve it.
            if proven_optimal:
                model.add(gap_expression == best_gap)
            else:
                model.add(gap_expression <= best_gap)
            model.minimize(cost_expression)
            status = solver.solve(model)
            proven_optimal = status == cp_model.OPTIMAL and proven_optimal
        mapping = {
            cp_model.OPTIMAL: SolverStatus.OPTIMAL,
            cp_model.FEASIBLE: SolverStatus.FEASIBLE,
            cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
            cp_model.MODEL_INVALID: SolverStatus.MODEL_INVALID,
        }
        resolved = mapping.get(status, SolverStatus.UNKNOWN)
        if resolved is SolverStatus.OPTIMAL and not proven_optimal:
            resolved = SolverStatus.FEASIBLE
        if resolved is SolverStatus.INFEASIBLE:
            return SolveResult(resolved, detail="no feasible schedule",
                               wall_seconds=solver.wall_time)
        if not resolved.solved:
            # Timeout or unknown: explicitly not a proof of infeasibility.
            return SolveResult(resolved, detail=f"solver_status:{status}",
                               wall_seconds=solver.wall_time,
                               unknowns=tuple(assumptions))
        produced = {p.code: int(solver.value(quantity[p.code])) for p in self.products}
        gaps = {p.code: int(solver.value(shortfall[p.code])) for p in self.products}
        component_need: dict[str, int] = {}
        for product in self.products:
            for code, per in component_coeff[product.code].items():
                component_need[code] = component_need.get(code, 0) + per * produced[
                    product.code]
        resource_use: dict[str, float] = {}
        for capacity in self.capacities:
            members = (capacity.resource, *capacity.alternates)
            resource_use[capacity.resource] = sum(
                product.resource_hours.get(member, 0.0) * produced[product.code]
                for product in self.products for member in members)
        facts = {
            "shortfall": sum(gaps.values()),
            "feasible": all(gap == 0 for gap in gaps.values()),
            "component_need": component_need,
            "produced": produced,
        }
        hits = tuple(rules.evaluate(facts)) if rules is not None else ()
        return SolveResult(
            resolved,
            PlanResult(
                produced, gaps, component_need, resource_use,
                feasible=all(gap == 0 for gap in gaps.values()), rule_hits=hits,
                status=resolved, assumptions=tuple(assumptions),
                objective_value=float(solver.objective_value),
                binding_constraints=_binding_constraints(
                    component_need, component_coeff, self.component_supply,
                    produced, self.capacities, resource_use)),
            wall_seconds=solver.wall_time)


def _binding_constraints(component_need, component_coeff, supplies, produced,
                         capacities, resource_use) -> tuple[str, ...]:
    binding: list[str] = []
    for component, need in component_need.items():
        raw = supplies.get(component)
        if raw is None:
            continue
        supply = normalise_supply(raw)
        usable = supply.usable
        if usable is not None and need >= usable:
            binding.append(f"component:{component}")
    for capacity in capacities:
        used = resource_use.get(capacity.resource, 0.0)
        if capacity.hours > 0 and used >= capacity.hours - 1e-9:
            binding.append(f"capacity:{capacity.resource}")
    return tuple(binding)
