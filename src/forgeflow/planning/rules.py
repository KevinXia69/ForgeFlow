"""Strict, non-eval business-rule compilation over planning facts."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


class RuleError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BusinessRule:
    name: str
    scope_model: str
    condition: dict[str, Any]
    version: int = 1
    source_ref: str | None = None
    rule_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RuleHit:
    name: str
    version: int
    source_ref: str | None


def _field(facts: dict[str, Any], name: str) -> Any:
    if name not in facts:
        raise RuleError("rule_field_missing")
    return facts[name]


def compile_condition(condition: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    """Compile a bounded JSON DSL; never executes strings as code."""
    if not isinstance(condition, dict) or len(condition) != 1:
        raise RuleError("rule_condition_invalid")
    operator, value = next(iter(condition.items()))
    if operator in {"all", "any"}:
        if not isinstance(value, list) or not value:
            raise RuleError("rule_condition_invalid")
        compiled = [compile_condition(item) for item in value]
        if operator == "all":
            return lambda facts: all(item(facts) for item in compiled)
        return lambda facts: any(item(facts) for item in compiled)
    if operator == "not":
        child = compile_condition(value)
        return lambda facts: not child(facts)
    if operator not in {"eq", "ne", "gt", "gte", "lt", "lte", "in"}:
        raise RuleError("rule_operator_invalid")
    if not isinstance(value, dict) or "field" not in value or "value" not in value:
        raise RuleError("rule_condition_invalid")
    field, expected = str(value["field"]), value["value"]
    operations = {
        "eq": lambda actual: actual == expected,
        "ne": lambda actual: actual != expected,
        "gt": lambda actual: actual > expected,
        "gte": lambda actual: actual >= expected,
        "lt": lambda actual: actual < expected,
        "lte": lambda actual: actual <= expected,
        "in": lambda actual: actual in expected if isinstance(expected, (list, tuple, set)) else False,
    }
    operation = operations[operator]
    return lambda facts: operation(_field(facts, field))


class RuleSet:
    def __init__(self, rules: list[BusinessRule]) -> None:
        self.rules = list(rules)
        self._compiled = [(rule, compile_condition(rule.condition)) for rule in self.rules]

    def evaluate(self, facts: dict[str, Any]) -> list[RuleHit]:
        hits = []
        for rule, predicate in self._compiled:
            try:
                matched = bool(predicate(facts))
            except RuleError:
                raise
            except TypeError:
                raise RuleError("rule_evaluation_failed") from None
            if matched:
                hits.append(RuleHit(rule.name, rule.version, rule.source_ref))
        return hits


def rule_from_odoo(row: dict[str, Any]) -> BusinessRule:
    try:
        condition = json.loads(row["condition_json"])
    except (KeyError, TypeError, ValueError):
        raise RuleError("rule_condition_invalid") from None
    return BusinessRule(str(row.get("name", "")), str(row.get("scope_model", "")),
                        condition, int(row.get("version", 1) or 1),
                        row.get("source_ref"), row.get("rule_hash"))
