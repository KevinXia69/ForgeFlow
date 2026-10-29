"""Additional product constraints; never change the original task request/policy.

Percent values use percentage points (30 means 30%), at most two decimals.
The solver receives an effective floor, while the independent projection checker
uses actual document amounts. A rejected candidate is not an infeasibility proof.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from forgeflow.planning.live import build_blueprint
from forgeflow.planning.objectives import bound_objective
from forgeflow.recovery.margin import margin_satisfied
from forgeflow.recovery.storage import StorageError, digest, read_json


class ConstraintError(ValueError):
    pass


def canonical_constraints(value, request=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"minimum_margin_pct"}:
        raise ConstraintError("product_constraints_invalid")
    if not value:
        return {}
    raw = value["minimum_margin_pct"]
    if type(raw) not in (int, float):
        raise ConstraintError("product_constraints_invalid")
    floor = Decimal(str(raw))
    if (not floor.is_finite() or not 0 < floor < 100
            or floor * 100 != (floor * 100).to_integral_value()):
        raise ConstraintError("product_constraints_invalid")
    if request is not None and floor <= Decimal(str(request["margin"])) * 100:
        raise ConstraintError("product_constraints_must_tighten")
    return {"minimum_margin_pct": float(floor)}


def check_no_relaxation(previous, current):
    previous, current = canonical_constraints(previous), canonical_constraints(current)
    if previous and (not current or current["minimum_margin_pct"] < previous["minimum_margin_pct"]):
        raise ConstraintError("product_constraints_relaxed")


def constrained_blueprint(snapshot, request, constraints=None):
    constraints = canonical_constraints(constraints, request)
    blueprint = build_blueprint(snapshot, request)
    if constraints:
        blueprint["margin"] = float(Decimal(str(constraints["minimum_margin_pct"])) / 100)
    return blueprint


def check_product_margin(actual_spend: Decimal, revenue: Decimal, constraints, request):
    constraints = canonical_constraints(constraints, request)
    if not constraints:
        return {}
    floor = Decimal(str(constraints["minimum_margin_pct"])) / 100
    if not margin_satisfied(actual_spend, revenue, floor):
        raise ConstraintError("product_constraint_not_satisfied")
    return {"product_minimum_margin_pct": constraints["minimum_margin_pct"],
            "product_margin_satisfied": True}


def load_constraints(directory: Path, manifest: dict, request: dict):
    """Recheck the separate artifact and the root version's input binding.

    Old preparations had no additional constraints. Preserve their original
    behavior, but reject an unbound extra file rather than silently ignoring it.
    """
    path = directory / "product-constraints.json"
    try:
        selected = bound_objective(manifest)
        if "product_constraints_sha256" not in manifest:
            if path.exists() or path.is_symlink() or selected is not None:
                raise ValueError
            return {}
        value = read_json(path, 4096)
        canonical = canonical_constraints(value, request)
        inputs = {"request": request, "policy_sha256": manifest["policy_sha256"], "constraints": canonical}
        if selected is not None:
            inputs["product_objectives"] = manifest["product_objectives"]
        if manifest.get("retention_profile") is not None:
            inputs["retention_profile"] = manifest["retention_profile"]
        if (value != canonical or digest(canonical) != manifest["product_constraints_sha256"]
                or manifest["product"]["inputs_sha256"] != digest(inputs)):
            raise ValueError
        return canonical
    except (OSError, ValueError, KeyError, TypeError, StorageError):
        raise ConstraintError("product_constraints_binding_changed") from None
