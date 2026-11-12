from __future__ import annotations

import pytest

from forgeflow.planning.rules import BusinessRule, RuleError, RuleSet, compile_condition


def test_compiles_nested_rule_without_eval() -> None:
    condition = {"all": [
        {"gt": {"field": "shortfall", "value": 0}},
        {"in": {"field": "sku", "value": ["A", "B"]}},
        {"not": {"eq": {"field": "approved", "value": True}}},
    ]}
    rule = BusinessRule("escalate", "mrp.production", condition, version=3, source_ref="doc-1")
    hits = RuleSet([rule]).evaluate({"shortfall": 2, "sku": "A", "approved": False})
    assert hits[0].name == "escalate" and hits[0].version == 3


def test_unknown_field_and_invalid_operator_fail_closed() -> None:
    with pytest.raises(RuleError, match="rule_field_missing"):
        RuleSet([BusinessRule("x", "m", {"eq": {"field": "missing", "value": 1}})]).evaluate({})
    with pytest.raises(RuleError, match="rule_operator_invalid"):
        compile_condition({"exec": {"field": "x", "value": 1}})


def test_rule_from_odoo_rejects_malformed_json() -> None:
    from forgeflow.planning.rules import rule_from_odoo
    with pytest.raises(RuleError, match="rule_condition_invalid"):
        rule_from_odoo({"name": "bad", "condition_json": "{"})
