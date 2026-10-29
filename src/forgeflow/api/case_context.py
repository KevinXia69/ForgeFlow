"""Bounded, valid JSON facts for read-only case chat; no cut-off JSON fragments."""

from __future__ import annotations

import json
from typing import Any

from .budget_context import BUDGET_CONTEXT_RULE, budget_fact
from .evidence import public_evidence
from .impact_context import IMPACT_CONTEXT_RULE, impact_summary

MAX_CONTEXT_CHARS = 6000
MARGIN_CONTEXT_RULE = ("Recorded margin checks may be historical. Only new_spend_coverage_v2 establishes the corrected basis; "
    "legacy margins and headroom cannot establish current-floor compliance. Zero revenue and zero cost means ratio not applicable.")


def answer_context(case: dict[str, Any]) -> dict[str, Any]:
    """Describe the exact public case read for this answer, never a later read."""
    def field(section, key):
        value = case.get(section)
        return value.get(key) if isinstance(value, dict) else None

    return {"schema_version": 1, "case_id": case.get("case_id"), "state": case.get("state"),
            "version": field("revision", "version"), "plan_id": field("plan", "plan_id"),
            "comparison_id": field("comparison", "comparison_id"),
            "selection_revision": field("comparison", "selection_revision"),
            "selection_pending": field("decision", "selection_pending") is True,
            "updated_at": case.get("updated_at"), "outcome": field("result", "outcome")}


def _scalars(value: Any, keys: tuple[str, ...] | None = None) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in keys if keys is not None else list(value)[:32]:
        item = value.get(key)
        if key in value and (item is None or isinstance(item, (str, bool, int, float))):
            result[key] = item[:160] + " [truncated]" if isinstance(item, str) and len(item) > 160 else item
    return result


def _plan(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = _scalars(value, ("plan_id", "objective", "margin", "margin_basis", "on_time", "new_spend", "cost_delta",
                              "policy_sha256", "snapshot_sha256", "prepared_at"))
    actions = value.get("actions")
    result["action_count"] = len(actions) if isinstance(actions, list) else None
    checks = value.get("checks")
    result["check_counts"] = ({status: sum(isinstance(check, dict) and check.get("status") == status
                                         for check in checks)
                               for status in ("pass", "warn", "block", "unknown")}
                              if isinstance(checks, list) else None)
    budget = value.get("budget_check") if isinstance(value.get("budget_check"), dict) else budget_fact(value)
    if budget is not None:
        result["budget_check"] = budget
    if isinstance(value.get("capacity"), dict):
        result["capacity"] = _scalars(value["capacity"], ("manufactured_units", "finished_purchase_units", "standard_minutes"))
    costs = value.get("costs")
    if isinstance(costs, dict):
        result["costs"] = _scalars(costs, ("known_delta", "new_purchase_value", "purchase_increase",
                                          "new_manufacturing_standard", "comparable", "full_delta", "actual_cost"))
        for key in ("baseline", "planned"):
            result["costs"][key] = _scalars(costs.get(key))
        result["costs"]["basis"] = _scalars(costs.get("basis"), ("as_of", "currency", "snapshot_sha256"))
    return result


def _plan_reference(value: Any, *, selected: bool = False) -> dict | None:
    """Keep every candidate's decision facts before adding document details."""
    if not isinstance(value, dict):
        return None
    result = _scalars(value, ("plan_id", "objective", "margin", "margin_basis", "on_time", "new_spend"))
    actions = value.get("actions")
    result["action_count"] = len(actions) if isinstance(actions, list) else None
    if selected:
        result["check_counts"] = _plan(value)["check_counts"]
    budget = value.get("budget_check")
    if isinstance(budget, dict):
        result["budget_check"] = budget
    if isinstance(value.get("capacity"), dict):
        result["capacity"] = _scalars(value["capacity"], ("manufactured_units", "finished_purchase_units", "standard_minutes"))
    costs = value.get("costs")
    if isinstance(costs, dict):
        result["costs"] = _scalars(costs, ("known_delta", "comparable"))
        for key in ("baseline", "planned"):
            result["costs"][key] = _scalars(costs.get(key), ("total",))
        result["costs"]["basis"] = _scalars(costs.get("basis"), ("currency",))
    return result


def _execution_reference(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = _scalars(value, ("status", "reason"))
    result["readback"] = _scalars(value.get("readback"), ("execution_validated", "verified_steps"))
    result["verifier"] = _scalars(value.get("verifier"), (
        "source", "evaluation_scope", "status", "strict_success", "evaluation_valid", "score",
        "passed_rule_count", "applicable_rule_count", "failed_rule_count"))
    result["failure"] = _scalars(value.get("failure"), ("code", "reason", "stage"))
    writes = value.get("writes")
    result["write_count"] = len(writes) if isinstance(writes, list) else None
    return result


def case_context(case: dict[str, Any]) -> str:
    public = public_evidence(case)
    has_impact = False

    def annotate(raw, visible):
        nonlocal has_impact
        if not isinstance(raw, dict) or not isinstance(visible, dict):
            return
        # Classify before redaction: removing a forbidden field must not repair
        # an invalid basis into apparent valid evidence.
        fact = budget_fact(raw)
        visible.pop("budget_check", None)
        if fact is not None:
            visible["budget_check"] = fact
        # A full graph is bounded but still too large for chat context. Keep
        # only a checked overview; detailed paths remain in the case evidence.
        impact = impact_summary(raw)
        visible.pop("impact_chain", None)
        visible.pop("impact_chain_summary", None)
        if impact is not None:
            has_impact = True
            visible["impact_chain_summary"] = impact

    annotate(case.get("plan"), public.get("plan"))
    for raw_group, visible_group in [(case.get("comparison"), public.get("comparison")),
                                    *[(row.get("comparison"), clean.get("comparison"))
                                      for row, clean in zip(case.get("history") or [], public.get("history") or [])
                                      if isinstance(row, dict) and isinstance(clean, dict)]]:
        if isinstance(raw_group, dict) and isinstance(visible_group, dict):
            for raw, visible in zip(raw_group.get("plans") or [], visible_group.get("plans") or []):
                annotate(raw, visible)
    for raw, visible in zip(case.get("history") or [], public.get("history") or []):
        if isinstance(raw, dict) and isinstance(visible, dict):
            annotate(raw.get("plan"), visible.get("plan"))

    def encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    plan_rows = [public.get("plan"), *((public.get("comparison") or {}).get("plans") or [])]
    has_budget = any(isinstance(plan, dict) and plan.get("budget_check") is not None for plan in plan_rows)
    public["margin_check_interpretation"] = MARGIN_CONTEXT_RULE
    if has_budget:
        public["budget_check_interpretation"] = BUDGET_CONTEXT_RULE
    if has_impact:
        public["impact_chain_interpretation"] = IMPACT_CONTEXT_RULE
        public["omitted_sections"] = ["detailed_impact_chain"]
    full = encode(public)
    if len(full) <= MAX_CONTEXT_CHARS:
        return full
    # Reserve the complete bounded candidate set and execution outcome before
    # optional lifecycle text, cost breakdowns or receipt hashes can crowd them out.
    facts = {"context_projection": "summary; omitted fields are unknown here, not absent or zero",
             "margin_check_interpretation": MARGIN_CONTEXT_RULE}
    if has_budget:
        facts["budget_check_interpretation"] = "Check counts include recorded historical statuses; only explicit bound per_order budget_check establishes individual sales-value compliance."
    omitted = ["detailed_actions", "detailed_cost_documents", "detailed_policy_checks", "history", "audit", "detailed_order_readiness"]
    if has_impact:
        omitted.append("detailed_impact_chain")

    def add(key, value, *, omission=None):
        if len(encode({**facts, key: value, "omitted_sections": omitted})) <= MAX_CONTEXT_CHARS - 500:
            facts[key] = value
        else:
            omitted.append(omission or key)

    for key in ("case_id", "state", "reason", "scenario", "synthetic"):
        add(key, _scalars(public, (key,)).get(key))
    comparison = public.get("comparison")
    candidates = comparison.get("plans", []) if isinstance(comparison, dict) else []
    candidates = candidates[:3] if isinstance(candidates, list) else []
    selected = public.get("plan")
    add("plan", _plan_reference(selected, selected=True))
    if isinstance(comparison, dict):
        summary = _scalars(comparison, ("status", "reason", "version", "comparison_id", "selection_revision", "selected_plan_id"))
        summary["plans"] = [_plan_reference(plan) for plan in candidates]
        add("comparison", summary)
    execution = public.get("execution")
    add("execution", _execution_reference(execution))
    for key in ("result", "decision", "revision", "revision_limits", "selection_request"):
        summary = _scalars(public[key]) if isinstance(public.get(key), dict) else None
        if key == "revision" and summary is not None and isinstance(public[key].get("changes"), dict):
            summary["changes"] = _scalars(public[key]["changes"])
        add(key, summary)
    approval = public.get("approval")
    add("approval", _scalars(approval, ("phase", "plan_id")) if isinstance(approval, dict) else None)

    # Enrich already-preserved sections only after decision and receipt facts.
    # A failed enrichment omits its details, never the existing core section.
    add("plan", _plan(selected), omission="detailed_selected_plan")
    if isinstance(comparison, dict) and "comparison" in facts:
        attempts = comparison.get("attempts")
        add("comparison", {**facts["comparison"], "attempts": [
            _scalars(attempt, ("objective", "status", "reason")) for attempt in attempts[:3]
        ] if isinstance(attempts, list) else []}, omission="comparison_attempts")
    if isinstance(execution, dict):
        summary = _scalars(execution)
        for key in ("readback", "verifier", "failure"):
            summary[key] = _scalars(execution.get(key))
        writes = execution.get("writes")
        summary["write_count"] = len(writes) if isinstance(writes, list) else None
        add("execution", summary, omission="detailed_execution_receipt")
    if isinstance(approval, dict):
        add("approval", _scalars(approval), omission="approval_details")
    for key in ("order", "disruption"):
        add(key, _scalars(public.get(key)))
    readiness = selected.get("order_readiness") if isinstance(selected, dict) else None
    if isinstance(readiness, dict) and isinstance(readiness.get("orders"), list):
        rows = readiness["orders"]
        buffers = [row.get("buffer_days") for row in rows if isinstance(row, dict)]
        # Optional detail cannot displace the comparison, choice or receipt.
        add("selected_order_readiness", {
            "recorded_order_count": len(rows),
            "minimum_buffer_days": min(buffers) if buffers and all(type(x) is int for x in buffers) else None,
            "time_basis": "planned supply availability in calendar days; transport and actual delivery unmeasured",
            "detail": "per-order rows omitted from this summary; do not infer individual dates",
        })
    metrics = selected.get("decision_metrics") if isinstance(selected, dict) else None
    if isinstance(metrics, dict):
        add("selected_decision_metrics", {"margin": _scalars(metrics.get("margin")),
            "schema_version": metrics.get("schema_version"),
            "margin_basis": (metrics.get("basis") or {}).get("margin_basis", "legacy_unverified"),
            "readiness": _scalars(metrics.get("readiness")),
            "basis": "same selected plan; planning values, not actual funds, daily calendar or risk probabilities",
            "detail": "per-workcenter and per-order headroom omitted; consult case details"})
    if isinstance(selected, dict) and isinstance(selected.get("impact_chain_summary"), dict):
        add("selected_impact_chain", selected["impact_chain_summary"])
    facts["omitted_sections"] = omitted
    return encode(facts)
