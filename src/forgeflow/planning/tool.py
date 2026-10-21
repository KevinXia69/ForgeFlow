"""A read-only live-snapshot and generic CP-SAT CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from forgeflow.odoo_credentials import read_key
from forgeflow.planning.objectives import objective_kind

PLANNING_FAILURE_MESSAGE = (
    "Planning did not complete. Inspect saved non-secret inputs; "
    "use a new output directory to retry."
)


class SolverFailure(RuntimeError):
    """A bounded reason backed by an observed solver status, not prose parsing."""

    def __init__(self, reason, trace):
        super().__init__(reason)
        self.trace = trace


def _live_input_failure(error: Exception) -> dict:
    """Expose one bounded protocol, never exception text or arbitrary metadata."""
    code = getattr(error, "code", None)
    details = getattr(error, "details", None)
    if (
        type(code) is str
        and code == "unselected_active_demand"
        and type(details) is dict
        and set(details) == {"missing_sales_order_ids"}
    ):
        ids = details["missing_sales_order_ids"]
        if (
            type(ids) is list
            and 0 < len(ids) <= 500
            and all(type(value) is int and 0 < value <= 2**53 - 1 for value in ids)
            and ids == sorted(set(ids))
        ):
            return {
                "schema_version": 1,
                "ok": False,
                "odoo_writes": False,
                "error_code": "unselected_active_demand",
                "details": {"missing_sales_order_ids": list(ids)},
            }
    return {"ok": False, "error": PLANNING_FAILURE_MESSAGE}


def solve_blueprint(blueprint: dict, upstream: Path, *, product_objective=None) -> dict:
    """Recompute from explicit inputs; no fixed task or reference files."""
    repair = blueprint.get("repair_context") or {}
    if blueprint.get("objective_kind") != "repair_plan":
        raise ValueError("This tool requires the repair_plan objective.")
    if repair.get("baseline_plan"):
        raise ValueError("Reference plans are not accepted.")
    selected_kind = objective_kind(product_objective)
    # The schema names existing commitments "seeded_*". Our live mapper builds
    # those rows from explicit Odoo records, never the initializer's private file.
    sys.path.insert(0, str(upstream))
    try:
        from procurement.solver import (
            ScenarioBlueprint,
            ScenarioGenerator,
            capture_procurement_solver_trace,
        )
        generator = ScenarioGenerator
        if product_objective is not None:
            from procurement.procurement_objectives import ProcurementObjectiveKind

            class ProductGenerator(ScenarioGenerator):
                def _build_primary_objective(self, model, scenario, variables, *, objective_kind):
                    # In this pinned upstream, repair construction also installs
                    # outage/cancellation exclusions. Keep them for cost preference.
                    if objective_kind != ProcurementObjectiveKind.repair_plan:
                        self._repair_penalty_expr(model, scenario, variables)
                    return super()._build_primary_objective(
                        model, scenario, variables, objective_kind=objective_kind)

            generator = ProductGenerator

        parsed = ScenarioBlueprint.model_validate(blueprint)
        with capture_procurement_solver_trace() as trace:
            try:
                solver = generator(
                    parsed, solve_time_limit=10, num_search_workers=1,
                    seed=0, randomize_search=False, log_level=logging.WARNING,
                )
                plan = (solver.solve_plan_for_objective(ProcurementObjectiveKind(selected_kind))
                        if product_objective is not None else solver.solve_plan_for_objective())
            except RuntimeError:
                observed = trace.model_dump(mode="json")
                calls = observed.get("solve_calls", [])
                if (calls and calls[-1].get("status") == "INFEASIBLE"
                        and str(calls[-1].get("phase", "")).endswith((" primary", " primary retry"))
                        and observed.get("models")
                        and observed["models"][-1].get("fixed_spend_cents") is None):
                    # Infeasible for this modeled snapshot and all its bounds;
                    # this does not identify a unique conflicting constraint.
                    raise SolverFailure("planning_model_infeasible", observed) from None
                raise
        result = {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": "fresh solve from explicit live business inputs",
            "odoo_writes": False,
            "execution_validated": False,
            "blueprint_sha256": hashlib.sha256(
                json.dumps(blueprint, sort_keys=True, allow_nan=False).encode()
            ).hexdigest(),
            "solver": {"name": "OR-Tools CP-SAT", "version": "9.15.6755",
                       "workers": 1, "seed": 0, "phase_limit_seconds": 10,
                       "upstream_retry_multiplier": 3},
            "trace": trace.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
        }
        if product_objective is not None:
            result["product_objective"] = product_objective
        return result
    finally:
        sys.path.pop(0)


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    from forgeflow.planning.live import (
        LiveInputError,
        build_blueprint,
        collect_snapshot,
        validate_request,
    )
    audit = Path("/home/forgeflow/logs/planning") / uuid.uuid4().hex

    def save(name: str, value: object) -> None:
        _write_json(args.output / name, value)
        _write_json(audit / name, value)

    try:
        request = validate_request(json.loads(args.request.read_text()))
        args.output.mkdir(parents=True, exist_ok=False)
        audit.mkdir(parents=True, exist_ok=False)
        save("request.json", request)
        import odoolib
        connection = odoolib.get_connection(
            hostname="127.0.0.1", protocol="json2", port=8069,
            database="bench", password=read_key(),
        )
        snapshot = collect_snapshot(connection, request)
        save("snapshot.json", snapshot)
        blueprint = build_blueprint(snapshot, request)
        save("blueprint.json", blueprint)
        upstream = Path(__file__).resolve().parents[2] / "upstream"
        result = solve_blueprint(blueprint, upstream)
        save("plan.json", result)
    except LiveInputError as error:
        print(json.dumps(_live_input_failure(error)))
        return 2
    except Exception as error:
        # RPC errors may echo credentials or response bodies. Expose only type.
        print(json.dumps({"ok": False, "error_type": type(error).__name__,
                          "error": PLANNING_FAILURE_MESSAGE}))
        return 2
    print(json.dumps({"ok": True, "odoo_writes": False,
                      "plan_path": str(args.output / "plan.json"),
                      "objective_value": result["plan"]["objective_value"],
                      "expected_new_spend": result["plan"]["expected_new_spend"]}))
    return 0
