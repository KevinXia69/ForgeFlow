"""Bounded read-only origin diagnostics for an already authorized recorded plan.

Only recorded recovery inputs are read. No reference solution, verifier source,
agent trace, connection, planner solve or ERP mutation is involved. Recompiling
fixed dispatch records is permitted only with the originally bound compiler.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from forgeflow.recovery import compiler
from forgeflow.recovery.change_set import offer_key
from forgeflow.recovery.storage import digest

from .plans import _read, find_plan_dir

_REFERENCE = re.compile(r"[A-Za-z0-9_./-]{1,120}")


def _reference(value):
    return value if isinstance(value, str) and _REFERENCE.fullmatch(value) else None


def _origins(value):
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) > 64 or any(_reference(part) is None for part in parts):
        return None
    return parts


def read_traceability(run_dir, plan_id):
    directory = find_plan_dir(run_dir, plan_id)
    if directory is None:
        return None
    unavailable = {"available": False, "reason": "recorded_origin_inputs_unavailable"}
    payload = {name: _read(directory / f"{name}.json") for name in (
        "manifest", "binding", "change-set", "snapshot", "baseline",
    )}
    if any(value is None for value in payload.values()):
        return unavailable
    try:
        manifest = payload["manifest"]
        if digest(manifest) != plan_id or any(
            digest(payload[name]) != manifest["artifacts"][name]
            for name in ("binding", "change-set", "snapshot", "baseline")
        ):
            return {"available": False, "reason": "recorded_origin_binding_mismatch"}
        source_hash = hashlib.sha256(Path(compiler.__file__).read_bytes()).hexdigest()
        if payload["binding"].get("compiler_source_sha256") != source_hash:
            return {"available": False, "reason": "original_compiler_unavailable"}
        changes, snapshot = payload["change-set"], payload["snapshot"]
        blueprint = _read(directory / "blueprint.json")
        if blueprint is not None and digest(blueprint) != manifest["artifacts"].get("blueprint"):
            return {"available": False, "reason": "recorded_origin_binding_mismatch"}
        offers = {offer_key(row): row for row in (blueprint or {}).get("vendors", [])}
        if len(changes["operations"]) > 128:
            return {"available": False, "reason": "recorded_origin_limit_exceeded"}
        compiled = compiler.compile_steps(changes, snapshot, payload["baseline"]["payload"]["workorders"])
        if compiler.digest([step.record() for step in compiled["steps"]]) != payload["binding"]["steps_sha256"]:
            return {"available": False, "reason": "recorded_origin_binding_mismatch"}
        operations = {op["target"]["token"]: op for op in changes["operations"]}
        by_name = {identity["name"]: operations[token] for token, identity in compiled["bindings"].items()}
        bom_lines = snapshot["models"]["mrp.bom.line"]

        def relation(value):
            return value[0] if isinstance(value, list) and value else value

        documents = []
        for token, identity in compiled["bindings"].items():
            if identity["model"] != "purchase.order":
                continue
            op = operations[token]
            after = op.get("after") or {}
            source_records = (op.get("preconditions") or {}).get("source_records") or {}
            source = source_records.get("parent") or {}
            expected = compiled["final_expectations"].get(token, {}).get("fields", {})
            origin_names = _origins(expected.get("origin"))
            consumers = []
            for name in origin_names or []:
                consumer = by_name.get(name)
                target = (consumer or {}).get("target", {})
                fields = (consumer or {}).get("after") or {}
                required = None
                component_demand = None
                if target.get("model") == "mrp.production" and fields.get("bom_id") is not None:
                    matching = [row for row in bom_lines if
                        relation(row.get("bom_id")) == fields["bom_id"]
                        and relation(row.get("product_id")) == after.get("product_id")
                    ]
                    required = bool(matching)
                    if matching:
                        component_demand = sum(row["product_qty"] for row in matching) * fields["quantity"]
                consumers.append({
                    "name": name, "model": target.get("model"),
                    "action": (consumer or {}).get("action"),
                    "product_code": _reference(fields.get("product_code")),
                    "consumes_purchase_product": required,
                    "component_demand_quantity": component_demand,
                })
            documents.append({
                "name": _reference(identity["name"]), "action": op["action"],
                "product_code": _reference(after.get("product_code")),
                "supply_role": after.get("supply_role") if after.get("supply_role") in {"component", "finished"} else None,
                "source_origins": _origins(source.get("origin")),
                "expected_origins": origin_names,
                "quantity": after.get("quantity"),
                "offer_minimum_quantity": offers.get(after.get("offer_key"), {}).get("min_qty"),
                "unused_purchase_quantity": changes.get("projected_state", {}).get("unused_purchase_units", {}).get(token, 0),
                "consumers": consumers,
                "recorded_observed_origin": None,
            })
        return {"available": True, "source": "bound_recovery_inputs_and_original_compiler",
                "live_erp_checked": False, "documents": documents}
    except (KeyError, TypeError, ValueError, OSError, IndexError):
        return unavailable
