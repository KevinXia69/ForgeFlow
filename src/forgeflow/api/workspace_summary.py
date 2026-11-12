"""Compact workspace evidence, using the same case membership as the home cards."""

from __future__ import annotations

from typing import Any


def case_home_views(case: dict[str, Any]) -> list[str]:
    views = []
    if case.get("state") in ("awaiting_selection", "awaiting_approval"):
        views.append("decision")
    elif case.get("state") in ("preparing", "executing"):
        views.append("executing")
    result = case.get("result")
    if isinstance(result, dict) and all(
        result.get(key) is True for key in ("verified", "strict_success", "evaluation_valid")
    ):
        views.append("verified")
    return views


def workspace_summary(snapshot: dict[str, Any], offset: int, limit: int) -> dict[str, Any]:
    cases, runs = snapshot.get("cases"), snapshot.get("runs")
    if not isinstance(cases, list) or not isinstance(runs, list):
        raise ValueError("evidence_scope_unavailable")
    counts = {"decision": 0, "executing": 0, "verified": 0, "evidence": len(runs)}
    decision_breakdown = {"awaiting_selection": 0, "awaiting_approval": 0}
    references = []
    for index, case in enumerate(cases):
        if (not isinstance(case, dict) or not isinstance(case.get("case_id"), str)
                or not isinstance(case.get("state"), str)):
            raise ValueError("evidence_scope_unavailable")
        for view in case_home_views(case):
            counts[view] += 1
        if case.get("state") in decision_breakdown:
            decision_breakdown[case["state"]] += 1
        if offset <= index < offset + limit:
            references.append({key: case.get(key) for key in ("case_id", "scenario", "state")})
    return {
        "total_cases": len(cases), "counts": counts, "decision_breakdown": decision_breakdown,
        "verified_historical_runs": sum(
            isinstance(run, dict) and run.get("strict_success") is True and run.get("evaluation_valid") is True
            for run in runs
        ),
        "cases": references,
        "next_offset": offset + limit if limit and offset + limit < len(cases) else None,
    }
