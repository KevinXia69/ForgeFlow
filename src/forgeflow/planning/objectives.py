"""Reviewed product preferences; the autonomous repair objective stays fixed."""
from __future__ import annotations

PRODUCT_OBJECTIVES = ("preserve_quantities", "min_planned_cost")
CAPACITY_PRODUCT_OBJECTIVES = (*PRODUCT_OBJECTIVES, "preserve_capacity")
LEGACY_CANDIDATE_PROFILE = "quantity-cost-v1"
CAPACITY_CANDIDATE_PROFILE = "quantity-cost-capacity-v2"
PRODUCT_PROFILES = {
    LEGACY_CANDIDATE_PROFILE: PRODUCT_OBJECTIVES,
    CAPACITY_CANDIDATE_PROFILE: CAPACITY_PRODUCT_OBJECTIVES,
}
PRODUCT_OBJECTIVE_KINDS = {
    "preserve_quantities": "repair_plan",
    "min_planned_cost": "min_new_spend",
    "preserve_capacity": "capacity_preservation",
}


class ProductObjectiveError(ValueError):
    pass


def canonical_objectives(value):
    """Accept only complete reviewed profiles, retaining legacy ordering."""
    if value is None:
        return None
    if type(value) is not list or not any(value == list(items) for items in PRODUCT_PROFILES.values()):
        raise ProductObjectiveError("product_objectives_invalid")
    return list(value)


def profile_objectives(profile=LEGACY_CANDIDATE_PROFILE):
    if type(profile) is not str or profile not in PRODUCT_PROFILES:
        raise ProductObjectiveError("invalid_candidate_profile")
    return PRODUCT_PROFILES[profile]


def objective_kind(product_objective=None):
    if product_objective is None:
        return "repair_plan"
    if type(product_objective) is not str or product_objective not in PRODUCT_OBJECTIVE_KINDS:
        raise ProductObjectiveError("product_objective_invalid")
    return PRODUCT_OBJECTIVE_KINDS[product_objective]


def objective_matches(plan, product_objective=None):
    """A product-only preference cannot silently enter the autonomous path."""
    return (plan.get("product_objective") == product_objective
            and plan.get("plan", {}).get("objective_kind") == objective_kind(product_objective))


def bound_objective(manifest, plan=None):
    """Do not accept an orphan preference or silently convert an old approval."""
    keys = {"product_objectives", "product_objective"} & manifest.keys()
    if not keys:
        selected = None
    else:
        if keys != {"product_objectives", "product_objective"} or "product" not in manifest:
            raise ProductObjectiveError("product_objective_binding_changed")
        choices = canonical_objectives(manifest["product_objectives"])
        selected = manifest["product_objective"]
        if choices is None or type(selected) is not str or selected not in choices:
            raise ProductObjectiveError("product_objective_binding_changed")
    if plan is not None and not objective_matches(plan, selected):
        raise ProductObjectiveError("product_objective_binding_changed")
    return selected
