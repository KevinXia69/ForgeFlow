#!/usr/bin/env python3
"""Host-only diagnostic of three pure original origin-capacity predicates.

Reads the authorized API's bounded diagnostic JSON and the pinned task checker.
It never imports/executes the checker module, reads solutions, connects to ERP,
or emits checker source. This is an offline failure diagnosis, not a task score.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ("_scaled_qty", "_origin_traceability_requirement", "_traceability_capacity_feasible")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("preserve prior evidence and choose a new output")
    checker = ROOT / "vendor/erp-bench/tasks/2283_medium_repair_plan_medium/tests/checks.py"
    source = checker.read_bytes()
    functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    selected = [functions[name] for name in FUNCTIONS]
    # No module-level initializers or imports are executed. The selected pure
    # functions use only numeric/list/dict primitives and residual-flow queues.
    calls = {node.func.id for function in selected for node in ast.walk(function)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    allowed = {"abs", "float", "int", "round", "len", "set", "list", "min", "max", "sum", "any",
               "defaultdict", "deque", "add_edge", "_scaled_qty"}
    if calls - allowed or any(isinstance(node, (ast.Import, ast.ImportFrom))
                              for function in selected for node in ast.walk(function)):
        raise SystemExit("original pure-function interface changed; review required")
    namespace = {"defaultdict": defaultdict, "deque": deque}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "<original-origin-predicates>", "exec"), namespace)
    requirement = namespace["_origin_traceability_requirement"]
    feasible = namespace["_traceability_capacity_feasible"]
    naming = []
    for names in (("WH/MO/synthetic-a", "WH/MO/synthetic-b"), ("FF/MO/synthetic-a", "FF/MO/synthetic-b")):
        naming.append(requirement(list(names), 12, {name: {"qty": 10} for name in names}))
    diagnostic = json.loads(args.diagnostic.read_text())
    products = []
    for plan in diagnostic["plans"]:
        if plan.get("available") is not True:
            raise SystemExit("bound diagnostic unavailable")
        groups = defaultdict(list)
        for row in plan["documents"]:
            if row["supply_role"] == "component" and row["action"] != "cancel":
                groups[row["product_code"]].append(row)
        for product, documents in groups.items():
            demands = {consumer["name"]: {"qty": consumer["component_demand_quantity"]}
                       for row in documents for consumer in row["consumers"]}
            supplies = []
            for row in documents:
                required = requirement(row["expected_origins"], row["quantity"], demands,
                                       min_qty=row.get("offer_minimum_quantity"))
                if required is None:
                    raise SystemExit("individual origin requirement unavailable")
                supplies.append({"id": row["name"], "qty": row["quantity"],
                                 "origin_tokens": row["expected_origins"], **required})
            products.append({"product_code": product, "purchase_quantity": sum(row["qty"] for row in supplies),
                             "consumer_demand": sum(row["qty"] for row in demands.values()),
                             "declared_coverage": sum(row["required_covered_qty"] for row in supplies),
                             "original_capacity_feasible": feasible(supplies, demands)})
    # Independently authored, small-number reproduction; not a reference plan.
    demand = {name: {"qty": quantity} for name, quantity in zip(("a", "b", "c", "d"), (2, 5, 3, 1), strict=True)}
    def graph(origins):
        return [{"id": index, "qty": quantity, "origin_tokens": linked,
                 **requirement(linked, quantity, demand, min_qty=quantity)}
                for index, (quantity, linked) in enumerate(zip((8, 4), origins, strict=True))]
    payload = {
        "category": "offline_original_pure_predicate_diagnostic_not_benchmark",
        "run_id": diagnostic["run_id"], "checker_task": "2283_medium_repair_plan_medium",
        "checker_sha256": hashlib.sha256(source).hexdigest(), "functions": list(FUNCTIONS),
        "reference_solution_read": False, "erp_accessed": False,
        "synthetic_name_prefixes_equivalent": naming[0] == naming[1], "products": products,
        "synthetic_overlap_feasible": feasible(graph((("a", "b", "c"), ("c", "d"))), demand),
        "synthetic_reallocated_feasible": feasible(graph((("a", "b", "d"), ("c",))), demand),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
