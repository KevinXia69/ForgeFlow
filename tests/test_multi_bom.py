from __future__ import annotations

import pytest

from forgeflow.planning.multi_bom import (
    BomError,
    BomNode,
    Capacity,
    MultiBomPlanner,
    Product,
    explosion,
)


def test_explosion_aggregates_shared_components() -> None:
    bom = BomNode("FG", 1, (
        BomNode("SUB", 2, (BomNode("C1", 3),)),
        BomNode("C1", 1),
    ))
    assert explosion(bom) == {"C1": 7}


def test_cycle_fails_closed() -> None:
    with pytest.raises(BomError, match="bom_cycle"):
        explosion(BomNode("A", children=(BomNode("A"),)))


def test_shared_resource_shortfall_is_planned() -> None:
    first = Product("A", 10, BomNode("A"), {"press": 1.0})
    second = Product("B", 10, BomNode("B"), {"press": 1.0})
    result = MultiBomPlanner([first, second], [Capacity("press", 12)]).solve()
    assert result.produced["A"] + result.produced["B"] == 12
    assert sum(result.shortfall.values()) == 8


def test_shared_component_supply_limits_both_products() -> None:
    bom = BomNode("FG", children=(BomNode("SHARED", 2),))
    first = Product("A", 5, bom, {"line": 0.1})
    second = Product("B", 5, bom, {"line": 0.1})
    result = MultiBomPlanner([first, second], [Capacity("line", 10)],
                             component_supply={"SHARED": 12}).solve()
    assert result.component_need["SHARED"] == 12
    assert sum(result.shortfall.values()) == 4


def test_planning_rules_see_plan_facts() -> None:
    from forgeflow.planning.rules import BusinessRule, RuleSet
    rule = BusinessRule("escalate", "mrp.production",
                        {"gt": {"field": "shortfall", "value": 0}})
    first = Product("A", 10, BomNode("A"), {"press": 1.0})
    result = MultiBomPlanner([first], [Capacity("press", 5)]).solve(RuleSet([rule]))
    assert result.feasible is False
    assert result.rule_hits[0].name == "escalate"


def test_shortfall_rule_can_constrain_the_solver() -> None:
    from forgeflow.planning.rules import BusinessRule, RuleSet
    rule = BusinessRule("no gaps", "mrp.production",
                        {"lte": {"field": "shortfall", "value": 0}})
    first = Product("A", 10, BomNode("A"), {"press": 1.0})
    with pytest.raises(BomError, match="plan_infeasible"):
        MultiBomPlanner([first], [Capacity("press", 5)]).solve(
            RuleSet([rule]), enforce_rules=True)


def test_fractional_hours_do_not_round_to_zero_capacity() -> None:
    product = Product("A", 10, BomNode("A"), {"line": 0.04})
    feasible = MultiBomPlanner([product], [Capacity("line", 0.4)]).solve()
    assert feasible.produced["A"] == 10
    short = MultiBomPlanner([product], [Capacity("line", 0.39)]).solve()
    assert short.produced["A"] < 10
