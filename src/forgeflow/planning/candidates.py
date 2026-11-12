"""Candidate comparison for recovery plans.

Candidates are only comparable when they answer the same question.  Two plans are
*equivalent* when they produce the same quantities with the same cost profile;
a plan is *dominated* when another candidate is at least as good on every
objective and better on at least one.  Presenting a dominated plan as a choice is
how a comparison turns into a menu of noise.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .multi_bom import SolverStatus
from .recovery import RecoveryPlan

#: Objective order used for comparison; lower is better for every metric.
#: "Produced units" is deliberately not an objective: producing less is only good
#: if demand is still met, and unmet demand is already measured directly.
OBJECTIVES: tuple[str, ...] = (
    "unmet_units",
    "total_cost",
    "late_orders",
)


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    label: str
    plan: RecoveryPlan
    objectives: tuple[str, ...] = OBJECTIVES
    profile: str = "default"
    assumptions: tuple[str, ...] = ()
    notes: str = ""

    @property
    def signature(self) -> tuple[float, ...]:
        return tuple(self.metric(name) for name in self.objectives)

    def metric(self, name: str) -> float:
        plan = self.plan
        if name == "unmet_units":
            return float(plan.total_unmet())
        if name == "total_cost":
            return float(plan.total_cost())
        if name == "produce_units":
            return float(plan.total_units)
        if name == "late_orders":
            return float(sum(1 for value in plan.unmet.values() if value > 0))
        raise ValueError(f"unknown_objective:{name}")

    @property
    def comparable(self) -> bool:
        return self.plan.solved

    @property
    def trustworthy(self) -> bool:
        return self.plan.trustworthy


@dataclass(frozen=True, slots=True)
class Comparison:
    candidates: tuple[Candidate, ...]
    frontier: tuple[str, ...]
    dominated: dict[str, str]
    equivalent: dict[str, str]
    incomparable: tuple[str, ...]
    best_by_objective: dict[str, str | None]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "candidates": [
                {"candidate_id": candidate.candidate_id, "label": candidate.label,
                 "status": candidate.plan.status.value,
                 "solved": candidate.comparable,
                 "trustworthy": candidate.trustworthy,
                 "objectives": {name: candidate.metric(name) for name in candidate.objectives},
                 "assumptions": list(candidate.plan.assumptions),
                 "notes": candidate.notes}
                for candidate in self.candidates],
            "frontier": list(self.frontier),
            "dominated": dict(self.dominated),
            "equivalent": dict(self.equivalent),
            "incomparable": list(self.incomparable),
            "best_by_objective": dict(self.best_by_objective),
            "warnings": list(self.warnings),
        }


def fingerprint(plan: RecoveryPlan) -> str:
    """Stable identity of *what the plan does*, not of how it was computed."""
    material = (
        tuple(sorted((code, tuple(sorted(values.items())))
                     for code, values in plan.produce.items())),
        tuple(sorted((code, tuple(sorted(
            (supplier, tuple(sorted(values.items())))
            for supplier, values in suppliers.items())))
            for code, suppliers in plan.buy.items())),
        tuple(sorted(plan.stock_use.items())),
        tuple(sorted(plan.unmet.items())),
    )
    return hashlib.sha256(repr(material).encode("utf-8")).hexdigest()


def compare(candidates: list[Candidate]) -> Comparison:
    if not candidates:
        raise ValueError("candidates_required")
    solved = [candidate for candidate in candidates if candidate.comparable]
    warnings: list[str] = []
    unsolved = [candidate.candidate_id for candidate in candidates
                if not candidate.comparable]
    if unsolved:
        warnings.append("unsolved_candidates:" + ",".join(sorted(unsolved)))
    if any(not candidate.trustworthy for candidate in solved):
        warnings.append("candidate_has_unverified_assumptions")

    by_fingerprint: dict[str, Candidate] = {}
    equivalent: dict[str, str] = {}
    for candidate in solved:
        key = fingerprint(candidate.plan)
        if key in by_fingerprint:
            equivalent[candidate.candidate_id] = by_fingerprint[key].candidate_id
            continue
        by_fingerprint[key] = candidate

    unique = list(by_fingerprint.values())
    dominated: dict[str, str] = {}
    for candidate in unique:
        for other in unique:
            if other is candidate:
                continue
            if _dominates(other, candidate):
                dominated[candidate.candidate_id] = other.candidate_id
                break

    frontier = [candidate for candidate in unique
                if candidate.candidate_id not in dominated]
    best_by_objective: dict[str, str | None] = {}
    for name in OBJECTIVES:
        if not frontier:
            best_by_objective[name] = None
            continue
        best = min(frontier, key=lambda item: item.metric(name))
        best_by_objective[name] = best.candidate_id
    incomparable = tuple(
        candidate.candidate_id for candidate in frontier
        if not _same_profile(candidate, frontier[0])) if frontier else ()
    return Comparison(
        tuple(candidates),
        tuple(candidate.candidate_id for candidate in frontier),
        dominated, equivalent, incomparable, best_by_objective, tuple(warnings))


def _dominates(left: Candidate, right: Candidate) -> bool:
    better_or_equal = all(left.metric(name) <= right.metric(name)
                          for name in left.objectives)
    strictly_better = any(left.metric(name) < right.metric(name)
                          for name in left.objectives)
    return better_or_equal and strictly_better


def _same_profile(left: Candidate, right: Candidate) -> bool:
    return left.objectives == right.objectives


def candidate_from_plan(plan: RecoveryPlan, *, candidate_id: str, label: str,
                        profile: str = "default", notes: str = "") -> Candidate:
    if plan.status is SolverStatus.MODEL_INVALID:
        raise ValueError("candidate_model_invalid")
    return Candidate(candidate_id, label, plan, profile=profile,
                     assumptions=tuple(plan.assumptions), notes=notes)
