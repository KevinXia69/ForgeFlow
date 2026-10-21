"""Compile a change set into fixed dispatch steps; no RPC or filesystem I/O.

Input is a trusted computed change set plus its bound live snapshot and fresh
workorder preflight records. Stable names are assigned BEFORE creating a step.
The adapter must validate current scope and read every postcondition.
"""

from __future__ import annotations

import copy
import hashlib
import json

from forgeflow.recovery.journal import Step


class CompileError(ValueError):
    pass


def need(value, code):
    if not value:
        raise CompileError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _relation(value):
    return value[0] if isinstance(value, list) else value


def compile_steps(change_set, snapshot, workorders):
    """Prepare records only. Execution requires a separate reviewed adapter."""
    need(
        change_set["schema_version"] == 1
        and change_set["semantics"] == "target_state_to_change_set",
        "unsupported_change_set",
    )
    need(change_set["source_sha256"]["snapshot"] == digest(snapshot), "snapshot_mismatch")
    # Input workorder rows must come from a fresh read, including scheduling
    # fields absent from the original planning snapshot, never invented defaults.
    required_wo = {
        "id",
        "production_id",
        "workcenter_id",
        "state",
        "date_start",
        "date_finished",
        "duration_expected",
    }
    wo_by_id = {}
    for row in workorders:
        need(required_wo <= row.keys(), "incomplete_workorder_preflight")
        need(row["id"] not in wo_by_id, "duplicate_workorder_preflight")
        wo_by_id[row["id"]] = row
    changes = copy.deepcopy(change_set)
    seed = digest(changes)
    bindings = {}
    all_ops = changes["operations"]
    for op in all_ops:
        t = op["target"]
        need(t["model"] in ("mrp.production", "purchase.order"), "unsupported_document_model")
        need(t["token"] not in bindings, "duplicate_document_token")
        if op["action"] == "create":
            need(t["id"] is None and t["name"] is None, "invalid_new_identity")
            suffix = hashlib.sha256((seed + ":" + t["token"]).encode()).hexdigest()[:24]
            t["name"] = "FF/" + ("MO/" if t["model"] == "mrp.production" else "PO/") + suffix
        else:
            need(
                type(t["id"]) is int and t["id"] > 0 and bool(t["name"]),
                "invalid_existing_identity",
            )
        bindings[t["token"]] = copy.deepcopy(t)

    def origins(after):
        names = []
        for ref in after["origin_refs"]:
            if ref["model"] == "sale.order":
                need(ref["id"] in changes["scope"]["sales_order_ids"], "out_of_scope_sale")
                names.append(ref["name"])
            else:
                need(ref["token"] in bindings, "unresolved_consumer")
                consumer = next(x for x in all_ops if x["target"]["token"] == ref["token"])
                need(consumer["action"] != "cancel", "cancelled_consumer")
                need(consumer["target"]["model"] == "mrp.production", "invalid_consumer_type")
                names.append(bindings[ref["token"]]["name"])
        need(names and len(set(names)) == len(names), "invalid_consumer_set")
        return sorted(names)

    steps = []
    final = {}

    def add(op, kind, phase, arguments, before, after, target=None):
        t = copy.deepcopy(target or op["target"])
        key = op["operation_id"] + ":" + phase
        payload = {
            "identity": t,
            "arguments": arguments,
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
        }
        step = Step(key, kind, t["token"], payload, (steps[-1].key,) if steps else ())
        steps.append(step)
        final[t["token"]] = {"identity": t, "fields": copy.deepcopy(after)}

    # Cancel disrupted or unused existing commitments, then establish all active
    # MO identities, and only then relink component purchases to real names.
    ordered = sorted(
        all_ops,
        key=lambda op: (
            0 if op["action"] == "cancel" else 1 if op["kind"] == "manufacturing_order" else 2,
            op["operation_id"],
        ),
    )
    for op in ordered:
        t, action, after = op["target"], op["action"], op["after"]
        is_mo = t["model"] == "mrp.production"
        if action == "cancel":
            source = op["preconditions"]["source_records"]["parent"]
            before = {field: _relation(value) for field, value in source.items()}
            if not is_mo:
                children = op["preconditions"]["source_records"]["children"]
                need(len(children) == 1, "ambiguous_cancel_purchase_line")
                before["line"] = {field: _relation(value) for field, value in children[0].items()}
            add(
                op,
                "cancel_manufacturing" if is_mo else "cancel_purchase",
                "cancel",
                {},
                before,
                {**before, "state": "cancel"},
            )
            continue
        need(action in ("keep", "update", "create"), "unsupported_change_action")
        names = origins(after)
        if is_mo:
            wanted = {
                "name": t["name"],
                "product_id": after["product_id"],
                "product_uom_id": after["product_uom_id"],
                "company_id": after["company_id"],
                "bom_id": after["bom_id"],
                "product_qty": after["quantity"],
                "origin": ", ".join(names),
                "date_start": after["date_start"],
                "date_deadline": after["date_deadline"],
                "qty_produced": 0,
            }
            if action == "create":
                args = {
                    "name": t["name"],
                    "company_id": after["company_id"],
                    "product_id": after["product_id"],
                    "product_uom_id": after["product_uom_id"],
                    "bom_id": after["bom_id"],
                    "quantity": after["quantity"],
                    "consumer_names": names,
                    "start": after["date_start"],
                    "deadline": after["date_deadline"],
                }
                add(op, "create_manufacturing", "create", args, None, {**wanted, "state": "draft"})
                add(
                    op,
                    "confirm_manufacturing",
                    "confirm",
                    {},
                    {**wanted, "state": "draft"},
                    {**wanted, "state": "confirmed"},
                )
            else:
                source = op["preconditions"]["source_records"]["parent"]
                current = {field: _relation(source[field]) for field in wanted}
                current["state"] = source["state"]
                if current["product_qty"] != wanted["product_qty"]:
                    args = {"quantity": after["quantity"], "mode": "quantity"}
                    updated = {**current, "product_qty": wanted["product_qty"]}
                    add(op, "update_manufacturing", "quantity", args, current, updated)
                    current = updated
                if any(
                    current[field] != wanted[field]
                    for field in ("origin", "date_start", "date_deadline")
                ):
                    dates_changed = any(
                        current[field] != wanted[field] for field in ("date_start", "date_deadline")
                    )
                    mode = "plan" if dates_changed else "origin"
                    args = {"mode": mode, "consumer_names": names}
                    if dates_changed:
                        args.update(start=after["date_start"], deadline=after["date_deadline"])
                    add(
                        op,
                        "update_manufacturing",
                        mode,
                        args,
                        current,
                        {**wanted, "state": "confirmed"},
                    )
                final[t["token"]] = {"identity": t, "fields": {**wanted, "state": "confirmed"}}
            wo = after["workorder_plan"]
            existing_id = wo["existing_id"]
            before_wo = None
            if existing_id is not None:
                need(existing_id in wo_by_id, "missing_workorder_preflight")
                row = wo_by_id[existing_id]
                need(_relation(row["production_id"]) == t["id"], "wrong_workorder_parent")
                need(row["state"] not in ("done", "cancel", "progress"), "executed_workorder")
                before_wo = {
                    field: _relation(row[field])
                    for field in (
                        "workcenter_id",
                        "date_start",
                        "date_finished",
                        "duration_expected",
                    )
                }
            wanted_wo = {
                "workcenter_id": after["workcenter_id"],
                "date_start": wo["date_start"],
                "date_finished": wo["date_finished"],
                "duration_expected": wo["duration_expected_minutes"],
            }
            wo_target = {
                "token": "workorder:" + t["token"],
                "model": "mrp.workorder",
                "id": existing_id,
                "name": None,
                "production": t,
            }
            preceding_mo_update = any(
                s.document_token == t["token"]
                and s.kind == "update_manufacturing"
                and s.payload["arguments"]["mode"] in ("quantity", "plan")
                for s in steps
            )
            if existing_id is None or preceding_mo_update:
                operations = [
                    r
                    for r in snapshot["models"]["mrp.routing.workcenter"]
                    if _relation(r["bom_id"]) == after["bom_id"]
                ]
                need(len(operations) == 1, "ambiguous_workorder_operation")
                bind_key = op["operation_id"] + ":bind_workorder"
                add(
                    op,
                    "bind_workorder",
                    "bind_workorder",
                    {
                        "product_id": after["product_id"],
                        "operation_id": operations[0]["id"],
                    },
                    None,
                    {"binding_key": bind_key},
                    wo_target,
                )
                before_wo = {"binding_key": bind_key}
            if before_wo != wanted_wo:
                args = {
                    "workcenter_id": after["workcenter_id"],
                    "start": wo["date_start"],
                    "finish": wo["date_finished"],
                    "duration_minutes": wo["duration_expected_minutes"],
                }
                add(op, "schedule_workorder", "workorder", args, before_wo, wanted_wo, wo_target)
            final[wo_target["token"]] = {"identity": wo_target, "fields": wanted_wo}
        else:
            wanted = {
                "name": t["name"],
                "company_id": after["company_id"],
                "partner_id": after["partner_id"],
                "currency_id": after["currency_id"],
                "origin": ", ".join(names),
                "state": "purchase",
                "line": {
                    "product_id": after["product_id"],
                    "product_uom_id": after["product_uom_id"],
                    "product_qty": after["quantity"],
                    "price_unit": after["unit_price"],
                    "date_planned": after["date_planned"],
                    "qty_received": 0,
                },
            }
            if action == "create":
                args = {
                    "name": t["name"],
                    "company_id": after["company_id"],
                    "currency_id": after["currency_id"],
                    "partner_id": after["partner_id"],
                    "product_id": after["product_id"],
                    "product_uom_id": after["product_uom_id"],
                    "product_name": after["line_name"],
                    "quantity": after["quantity"],
                    "price_unit": after["unit_price"],
                    "arrival": after["date_planned"],
                    "consumer_names": names,
                }
                add(op, "create_purchase", "create", args, None, {**wanted, "state": "draft"})
                add(op, "confirm_purchase", "confirm", {}, {**wanted, "state": "draft"}, wanted)
            else:
                source = op["preconditions"]["source_records"]["parent"]
                line = op["preconditions"]["source_records"]["children"][0]
                current = {field: _relation(source[field]) for field in wanted if field != "line"}
                current["line"] = {field: _relation(line[field]) for field in wanted["line"]}
                if current["line"] != wanted["line"]:
                    args = {
                        "mode": "line",
                        "line_id": after["line_id"],
                        "quantity": after["quantity"],
                        "price_unit": after["unit_price"],
                        "arrival": after["date_planned"],
                    }
                    updated = {**current, "line": wanted["line"]}
                    add(op, "update_purchase", "line", args, current, updated)
                    current = updated
                if current["origin"] != wanted["origin"]:
                    add(
                        op,
                        "update_purchase",
                        "origin",
                        {"mode": "origin", "consumer_names": names},
                        current,
                        wanted,
                    )
            final[t["token"]] = {"identity": t, "fields": wanted}
    return {
        "schema_version": 1,
        "execution_ready": False,
        "change_set_sha256": seed,
        "bindings": bindings,
        "steps": steps,
        "final_expectations": final,
        "limitations": [
            "Compile-only prototype; adapter preflight and live postconditions required."
        ],
    }
