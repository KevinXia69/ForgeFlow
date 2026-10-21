"""Validated structured-exception drafts for the two reviewed sandbox contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..product.state import SCENARIOS


@dataclass(frozen=True)
class DraftResult:
    valid: bool
    scenario: str | None
    errors: tuple[str, ...]


def validate_case_draft(body: dict[str, Any]) -> DraftResult:
    kind = str(body.get("kind") or "")
    product = str(body.get("product_code") or "").strip()
    units = body.get("units")
    due_days = body.get("due_days")
    scenario = next((name for name, spec in SCENARIOS.items() if spec["disruption"]["kind"] == kind), None)
    errors: list[str] = []
    if scenario is None:
        errors.append("unsupported_exception_kind")
        return DraftResult(False, None, tuple(errors))
    spec = SCENARIOS[scenario]
    if product != spec["product_code"]:
        errors.append("product_not_in_contract")
    if not isinstance(units, int) or isinstance(units, bool) or units != spec["order"]["units"]:
        errors.append("quantity_not_in_contract")
    allowed_days = {spec["order"]["due_days"]}
    if "due_days_max" in spec["order"]:
        allowed_days.add(spec["order"]["due_days_max"])
    if not isinstance(due_days, int) or isinstance(due_days, bool) or due_days not in allowed_days:
        errors.append("due_date_not_in_contract")
    return DraftResult(not errors, scenario, tuple(errors))


def draft_view(body: dict[str, Any]) -> dict[str, Any]:
    result = validate_case_draft(body)
    spec = SCENARIOS.get(result.scenario or "", {})
    return {
        "valid": result.valid,
        "scenario": result.scenario,
        "errors": list(result.errors),
        "contract": {
            "kind": spec.get("disruption", {}).get("kind"),
            "product_code": spec.get("product_code"),
            "units": spec.get("order", {}).get("units"),
            "due_days": spec.get("order", {}).get("due_days"),
        } if spec else None,
        "writes_nothing": True,
    }
