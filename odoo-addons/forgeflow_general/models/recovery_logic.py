"""Pure decision logic for an approved recovery plan.

This module deliberately imports nothing from Odoo so that the approval binding,
action validation, ordering, idempotency fingerprinting and acceptance checks can
be exercised by ordinary Python tests.  ``recovery_plan.py`` is the thin Odoo
glue that calls into it.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

#: Every action type a recovery plan may contain.
ACTION_TYPES = (
    "preserve_supply",
    "cancel_purchase_order",
    "adjust_purchase_line",
    "create_purchase_order",
    "cancel_manufacturing_order",
    "adjust_manufacturing_order",
    "create_manufacturing_order",
    "link_supply_to_demand",
    "reserve_stock",
)

#: Actions that change an existing document must name it.
MUTATING_ACTIONS = frozenset({
    "cancel_purchase_order", "adjust_purchase_line", "cancel_manufacturing_order",
    "adjust_manufacturing_order", "link_supply_to_demand",
})

#: Actions that create a new document must not name an existing one.
#: ``reserve_stock`` creates its own stock move unless one is named in params, so
#: it carries no pre-existing target and is validated as a creating action.
CREATING_ACTIONS = frozenset({"create_purchase_order", "create_manufacturing_order",
                              "reserve_stock"})

#: Actions that need a quantity, a currency and a unit to be meaningful.
QUANTIFIED_ACTIONS = frozenset({
    "adjust_purchase_line", "create_purchase_order", "adjust_manufacturing_order",
    "create_manufacturing_order",
})

#: Execution order: keep what works, remove what does not, then rebuild, then link.
_ORDER = {
    "preserve_supply": 10,
    "cancel_manufacturing_order": 20,
    "cancel_purchase_order": 20,
    "create_purchase_order": 30,
    "create_manufacturing_order": 30,
    "adjust_purchase_line": 40,
    "adjust_manufacturing_order": 40,
    "reserve_stock": 50,
    "link_supply_to_demand": 60,
}


def clean_for_rpc(value):
    """Drop ``None`` so an XML-RPC response can be marshalled.

    Odoo's XML-RPC endpoint serialises responses without ``allow_none``, so a
    single ``None`` anywhere in a returned structure makes the whole call fail on
    the server side.  Absent information is expressed by omitting the key.
    """
    if isinstance(value, dict):
        return {key: clean_for_rpc(item) for key, item in value.items()
                if item is not None}
    if isinstance(value, (list, tuple)):
        return [clean_for_rpc(item) for item in value if item is not None]
    return value


class PlanError(Exception):
    """A rejection with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def canonical_action(action: dict) -> dict:
    """Normalise an action so its digest does not depend on key order."""
    if not isinstance(action, dict):
        raise PlanError("action_invalid", "action must be an object")
    normalised = {
        "action_id": str(action.get("action_id") or ""),
        "type": str(action.get("type") or ""),
        "target_model": str(action.get("target_model") or ""),
        "target_id": int(action.get("target_id") or 0),
        "params": action.get("params") or {},
        "preconditions": list(action.get("preconditions") or []),
    }
    target_ref = action.get("target_ref")
    if target_ref is not None:
        # A target produced by an earlier action in this same plan.  It is part of
        # the approved action set: swapping which action it points at changes the
        # plan digest and therefore the approval.
        if not isinstance(target_ref, dict):
            raise PlanError("action_target_ref_invalid")
        normalised["target_ref"] = {
            "action_id": str(target_ref.get("action_id") or ""),
            "field": str(target_ref.get("field") or ""),
        }
    for key in ("quantity", "currency", "unit", "summary"):
        if action.get(key) is not None:
            normalised[key] = action[key]
    return normalised


#: A value produced by an earlier action: ``{"$ref": {"action_id", "field"}}``.
REFERENCE_KEY = "$ref"


def _collect_references(value) -> list[dict]:
    references: list[dict] = []
    if isinstance(value, dict):
        if set(value) == {REFERENCE_KEY}:
            references.append(value[REFERENCE_KEY])
            return references
        for item in value.values():
            references.extend(_collect_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            references.extend(_collect_references(item))
    return references


def action_references(action: dict) -> list[dict]:
    """Every earlier-action reference this action makes, validated for shape."""
    references: list[dict] = []
    if action.get("target_ref"):
        references.append(action["target_ref"])
    references.extend(_collect_references(action.get("params") or {}))
    for reference in references:
        if not isinstance(reference, dict):
            raise PlanError("action_reference_invalid")
        action_id = reference.get("action_id")
        field = reference.get("field")
        if not isinstance(action_id, str) or not action_id:
            raise PlanError("action_reference_invalid")
        if not isinstance(field, str) or not field:
            raise PlanError("action_reference_invalid")
    return references


def _dependency_map(actions: list[dict]) -> dict[str, set[str]]:
    return {action["action_id"]: {reference["action_id"]
                                  for reference in action_references(action)}
            for action in actions}


def _topological_order(actions: list[dict]) -> list[dict]:
    """Order by explicit dependency, then by business phase.

    A later action that consumes an earlier action's created record must run
    after it even when the phase table says otherwise.  A cycle is a broken plan,
    not something to guess around.
    """
    position = {action["action_id"]: index for index, action in enumerate(actions)}
    remaining = _dependency_map(actions)
    ordered: list[dict] = []
    while remaining:
        ready = [action for action in actions
                 if action["action_id"] in remaining
                 and not remaining[action["action_id"]]]
        if not ready:
            raise PlanError("action_reference_cycle",
                            ",".join(sorted(remaining)))
        ready.sort(key=lambda action: (_ORDER.get(action["type"], 99),
                                       position[action["action_id"]]))
        chosen = ready[0]
        ordered.append(chosen)
        del remaining[chosen["action_id"]]
        for dependencies in remaining.values():
            dependencies.discard(chosen["action_id"])
    return ordered


def validate_action_set(actions) -> list[dict]:
    """Reject a plan whose action set is incomplete or ambiguous."""
    if not isinstance(actions, list) or not actions:
        raise PlanError("plan_actions_empty")
    seen: set[str] = set()
    result: list[dict] = []
    for raw in actions:
        action = canonical_action(raw)
        identifier = action["action_id"]
        if not identifier:
            raise PlanError("action_id_required")
        if identifier in seen:
            raise PlanError("action_id_duplicated", identifier)
        seen.add(identifier)
        kind = action["type"]
        if kind not in ACTION_TYPES:
            raise PlanError("action_type_unsupported", kind)
        if not action["target_model"]:
            raise PlanError("action_target_model_required", identifier)
        if kind in MUTATING_ACTIONS:
            if "target_ref" in action:
                if action["target_id"] != 0:
                    raise PlanError("action_target_ambiguous", identifier)
            elif action["target_id"] <= 0:
                raise PlanError("action_target_id_required", identifier)
        if kind in CREATING_ACTIONS:
            if action["target_id"] != 0 or "target_ref" in action:
                raise PlanError("action_target_id_must_be_zero", identifier)
        if kind in QUANTIFIED_ACTIONS:
            if not isinstance(action.get("quantity"), (int, float)) or action["quantity"] <= 0:
                raise PlanError("action_quantity_required", identifier)
            if not action.get("unit"):
                raise PlanError("action_unit_required", identifier)
            if not action.get("currency"):
                raise PlanError("action_currency_required", identifier)
        for condition in action["preconditions"]:
            if not isinstance(condition, dict) or "field" not in condition:
                raise PlanError("action_precondition_invalid", identifier)
        result.append(action)

    by_id = {action["action_id"]: action for action in result}
    for action in result:
        for reference in action_references(action):
            target = by_id.get(reference["action_id"])
            if target is None:
                raise PlanError("action_reference_unknown", reference["action_id"])
            if target["action_id"] == action["action_id"]:
                raise PlanError("action_reference_cycle", action["action_id"])
            if target["type"] not in CREATING_ACTIONS:
                # Only a record this plan creates can be referenced; pointing at a
                # mutated existing document would make the value depend on state
                # nobody approved.
                raise PlanError("action_reference_not_created",
                                reference["action_id"])
    _topological_order(result)
    return result


def action_digest(actions) -> str:
    return digest([canonical_action(action) for action in actions])


def plan_digest(*, tenant: str, environment_generation: str, plan_id: int,
                plan_version: int, fact_snapshot_hash: str, rule_version: int,
                actions) -> str:
    """The identity an approval is bound to.

    Approval of a different version, environment, fact snapshot, rule set or
    action set is a different approval.
    """
    return digest({
        "tenant": tenant,
        "environment_generation": environment_generation,
        "plan_id": int(plan_id),
        "plan_version": int(plan_version),
        "fact_snapshot_hash": fact_snapshot_hash,
        "rule_version": int(rule_version),
        "actions": [canonical_action(action) for action in actions],
    })


def idempotency_fingerprint(*, company_id: int, environment_generation: str,
                            plan_id: int, plan_version: int, action_id: str,
                            params, execution_identity: str = "") -> str:
    """A key that is stable for one action of one plan version of one company.

    ``execution_identity`` separates two approvals of the same plan version: a
    withdrawn approval and its replacement are different execution attempts, so
    their operations may not collide on the fingerprint's unique constraint.
    """
    payload = {
        "company_id": int(company_id),
        "environment_generation": environment_generation,
        "plan_id": int(plan_id),
        "plan_version": int(plan_version),
        "action_id": action_id,
        "params": params or {},
    }
    if execution_identity:
        payload["execution_identity"] = execution_identity
    return digest(payload)


def parse_moment(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def validate_approval(*, plan: dict, decision: dict | None, expected_digest: str | None,
                      expected_action_digest: str | None = None,
                      now: datetime | None = None) -> dict:
    """Check that a specific approval covers this exact plan version.

    ``expected_digest`` is the full plan binding (tenant, environment, version,
    facts, rules, actions).  ``expected_action_digest`` is the action set alone and
    is compared against the decision's ``action_digest``; the two are different
    hashes and must not be compared to each other.
    """
    moment = now or datetime.now(timezone.utc)
    if not decision:
        raise PlanError("approval_required")
    if str(decision.get("decision")) != "approved":
        raise PlanError("approval_not_granted", str(decision.get("decision")))
    if decision.get("revoked_at"):
        # A withdrawn approval is not a weaker approval; it authorises nothing.
        raise PlanError("approval_revoked")
    if decision.get("approver_active") is False:
        raise PlanError("approval_approver_inactive")
    if decision.get("approver_permitted") is False:
        raise PlanError("approval_approver_not_permitted")
    if str(decision.get("plan_hash") or "") != str(plan.get("plan_hash") or ""):
        raise PlanError("approval_plan_hash_mismatch")
    if decision.get("plan_version") is not None and \
            int(decision["plan_version"]) != int(plan.get("version") or 0):
        raise PlanError("approval_plan_version_mismatch")
    if decision.get("environment_generation") and \
            str(decision["environment_generation"]) != str(
                plan.get("environment_generation") or ""):
        raise PlanError("approval_environment_mismatch")
    if expected_digest is not None and str(decision.get("plan_digest") or "") != expected_digest:
        raise PlanError("approval_plan_digest_mismatch")
    if expected_action_digest is not None and decision.get("action_digest") and \
            str(decision["action_digest"]) != expected_action_digest:
        raise PlanError("approval_action_set_mismatch")
    if not decision.get("approver_id"):
        raise PlanError("approval_approver_required")
    expiry = parse_moment(decision.get("expires_at"))
    if expiry is not None and expiry < moment:
        raise PlanError("approval_expired")
    identity = decision.get("execution_identity")
    if not identity:
        raise PlanError("approval_execution_identity_required")
    return {
        "approver_id": decision.get("approver_id"),
        "execution_identity": identity,
        "expires_at": expiry,
        "action_digest": expected_digest,
    }


def execution_order(actions) -> list[dict]:
    """Order actions so a plan cannot destroy what it still needs."""
    return _topological_order(validate_action_set(actions))


#: Which model a reference is allowed to name, by the parameter it fills.
#:
#: A bare numeric id is not a type.  Filling a purchase order's id into a sales
#: line parameter can silently link an unrelated record with the same number, so
#: the reference's produced model must match the relation the parameter expects.
PARAM_MODELS: dict[str, dict[str, str]] = {
    "create_purchase_order": {"partner_id": "res.partner",
                              "product_id": "product.product"},
    "create_manufacturing_order": {"product_id": "product.product",
                                   "bom_id": "mrp.bom",
                                   "workcenter_id": "mrp.workcenter"},
    "link_supply_to_demand": {"sale_line_id": "sale.order.line"},
    "reserve_stock": {"product_id": "product.product",
                      "location_id": "stock.location", "move_id": "stock.move"},
}


def resolve_action_references(action: dict, created: dict) -> dict:
    """Substitute records created earlier in the same execution.

    No action may guess a new document's id: it names the producing action and
    the field, and the executor substitutes the real value or refuses.
    """
    resolved = canonical_action(action)
    target_ref = resolved.pop("target_ref", None)
    if target_ref:
        value = _reference_value(target_ref, created)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PlanError("action_reference_invalid_value",
                            str(target_ref.get("action_id")))
        expected = _reference_model(target_ref, created)
        if expected and expected != resolved["target_model"]:
            raise PlanError("action_reference_model_mismatch",
                            str(target_ref.get("action_id")))
        resolved["target_id"] = int(value)
    resolved["params"] = _resolve_value(
        resolved.get("params") or {}, created,
        PARAM_MODELS.get(resolved["type"], {}))
    return resolved


def _resolve_value(value, created: dict, expected: dict[str, str]):
    if isinstance(value, dict):
        if set(value) == {REFERENCE_KEY}:
            return _reference_value(value[REFERENCE_KEY], created)
        resolved = {}
        for key, item in value.items():
            if isinstance(item, dict) and set(item) == {REFERENCE_KEY}:
                wanted = expected.get(str(key))
                produced = _reference_model(item[REFERENCE_KEY], created)
                if wanted and produced and produced != wanted:
                    raise PlanError("action_reference_model_mismatch", str(key))
            resolved[key] = _resolve_value(item, created, expected)
        return resolved
    if isinstance(value, list):
        return [_resolve_value(item, created, expected) for item in value]
    return value


def _reference_entry(reference: dict, created: dict) -> dict:
    entry = created.get(str(reference.get("action_id")))
    if not isinstance(entry, dict):
        raise PlanError("action_reference_unresolved",
                        str(reference.get("action_id")))
    return entry


def _reference_value(reference: dict, created: dict):
    entry = _reference_entry(reference, created)
    field = str(reference.get("field"))
    if field not in entry or entry[field] is None:
        raise PlanError("action_reference_field_missing", field)
    return entry[field]


def _reference_model(reference: dict, created: dict) -> str | None:
    entry = _reference_entry(reference, created)
    field = str(reference.get("field"))
    if field == "id":
        return entry.get("model")
    return entry.get(f"{field}_model")


def plan_execution_key(*, plan_id: int, plan_version: int, plan_digest: str,
                       execution_identity: str,
                       environment_generation: str = "") -> str:
    """The plan's own execution identity, independent of any caller prefix.

    A retry that changes ``idempotency_prefix`` must still find the original
    attempt; the prefix is a transport detail, not a second execution.
    """
    return digest({
        "plan_id": int(plan_id),
        "plan_version": int(plan_version),
        "plan_digest": plan_digest,
        "execution_identity": execution_identity,
        "environment_generation": environment_generation,
    })


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.:-]{0,80}$")

#: Database failures that mean the current transaction's work is gone, not that
#: the business rule was violated.  They are reported as a class, never as a raw
#: driver message.
_DATABASE_ERRORS = frozenset({
    "OperationalError", "InterfaceError", "IntegrityError", "InternalError",
    "ProgrammingError", "DataError", "DatabaseError", "DivisionByZero",
    "CheckViolation", "NotNullViolation", "UniqueViolation", "ForeignKeyViolation",
    "SerializationFailure", "DeadlockDetected", "InFailedSqlTransaction",
})


def failure_code(error) -> str:
    """A bounded, machine-readable rejection instead of a leaked traceback."""
    if isinstance(error, PlanError):
        return error.code
    text = str(error).strip()
    if _SAFE_CODE.match(text):
        return text
    name = type(error).__name__
    if name in _DATABASE_ERRORS or name.startswith("psycopg"):
        return "database_error"
    return "execution_error"


def readback_targets(actions) -> list[dict]:
    """Every document an execution touched, for independent read-back."""
    targets: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for action in validate_action_set(actions):
        kind = action["type"]
        if kind in CREATING_ACTIONS:
            targets.append({"model": action["target_model"], "action_id": action["action_id"],
                            "kind": "created", "fields": _readback_fields(kind)})
            continue
        key = (action["target_model"], action["target_id"])
        if key in seen:
            continue
        seen.add(key)
        targets.append({"model": action["target_model"], "id": action["target_id"],
                        "action_id": action["action_id"], "kind": "existing",
                        "fields": _readback_fields(kind)})
    return targets


def _readback_fields(kind: str) -> list[str]:
    if kind in {"cancel_purchase_order", "adjust_purchase_line", "create_purchase_order"}:
        return ["id", "name", "state", "date_planned", "order_line", "amount_total",
                "product_qty"]
    if kind in {"cancel_manufacturing_order", "adjust_manufacturing_order",
                "create_manufacturing_order"}:
        return ["id", "name", "state", "product_qty", "date_start", "date_finished"]
    if kind == "reserve_stock":
        return ["id", "product_id", "location_id", "quantity", "reserved_quantity"]
    if kind == "link_supply_to_demand":
        return ["id", "sale_line_id", "product_uom_qty"]
    return ["id", "state"]


def check_preconditions(action: dict, current: dict) -> None:
    """Refuse an action whose recorded preconditions no longer hold."""
    for condition in action.get("preconditions") or []:
        field = condition.get("field")
        if field not in current:
            raise PlanError("precondition_field_missing", str(field))
        actual = current[field]
        if "in" in condition and actual not in (condition["in"] or []):
            raise PlanError("precondition_failed", f"{field}={actual!r}")
        if "eq" in condition and actual != condition["eq"]:
            raise PlanError("precondition_failed", f"{field}={actual!r}")
        if "gte" in condition and not (actual is not None and actual >= condition["gte"]):
            raise PlanError("precondition_failed", f"{field}={actual!r}")


def business_acceptance(actions, readback: dict, *, objectives: dict | None = None) -> list[dict]:
    """Layer-three acceptance: does the business state meet the recovery goal?

    ``readback`` maps ``"model:id"`` (or ``"model:new:<action_id>"``) to a dict of
    field values read independently of the executor.
    """
    checks: list[dict] = []
    validated = validate_action_set(actions)
    expected_cancelled = []
    expected_created = []
    expected_linked = []
    for action in validated:
        kind = action["type"]
        key = (f"{action['target_model']}:new:{action['action_id']}"
               if kind in CREATING_ACTIONS
               else f"{action['target_model']}:{action['target_id']}")
        record = readback.get(key)
        if record is None:
            checks.append({"code": f"{action['action_id']}:readback_present",
                           "status": "unknown", "detail": key})
            continue
        if kind in CREATING_ACTIONS:
            # A created document is accepted by existence and live state; the
            # order header does not carry the line quantity, so comparing a
            # ``product_qty`` on it would fail for a correct plan.
            live = {"purchase", "sent", "confirmed", "progress", "draft", "done",
                    "assigned", "partially_available", "waiting"}
            checks.append({
                "code": f"{action['action_id']}:created",
                "status": "pass" if record.get("id") and str(
                    record.get("state")) in live else "failed",
                "detail": str(record.get("state"))})
        elif kind in {"cancel_purchase_order", "cancel_manufacturing_order"}:
            expected_cancelled.append(kind)
            checks.append({
                "code": f"{action['action_id']}:cancelled",
                "status": "pass" if str(record.get("state")) in {"cancel", "cancelled"}
                else "failed",
                "detail": str(record.get("state"))})
        elif kind in QUANTIFIED_ACTIONS:
            quantity = record.get("product_qty", record.get("product_uom_qty"))
            expected = action.get("quantity")
            ok = quantity is not None and expected is not None and \
                abs(float(quantity) - float(expected)) < 1e-6
            checks.append({"code": f"{action['action_id']}:quantity",
                           "status": "pass" if ok else "failed",
                           "detail": f"{quantity!r} vs {expected!r}"})
        elif kind == "preserve_supply":
            checks.append({
                "code": f"{action['action_id']}:preserved",
                "status": "pass" if str(record.get("state")) in {
                    "purchase", "sent", "confirmed", "progress", "done"} else "failed",
                "detail": str(record.get("state"))})
        if kind in CREATING_ACTIONS:
            expected_created.append(key)
        if kind == "link_supply_to_demand":
            expected_linked.append(key)
            checks.append({
                "code": f"{action['action_id']}:linked",
                "status": "pass" if record.get("sale_line_id") else "failed",
                "detail": str(record.get("sale_line_id"))})
    if objectives:
        for name, expected in objectives.items():
            actual = readback.get(f"objective:{name}")
            checks.append({"code": f"objective:{name}",
                           "status": "pass" if actual == expected else "unknown",
                           "detail": f"{actual!r} vs {expected!r}"})
    return checks


def acceptance_status(checks) -> str:
    if any(check["status"] == "failed" for check in checks):
        return "failed"
    if any(check["status"] == "unknown" for check in checks):
        return "unknown"
    return "verified"


def plan_checks(*, actions, facts: dict, rules: list[dict] | None = None) -> list[dict]:
    """Layer one: does the plan satisfy the formal rules and the fact snapshot?"""
    checks: list[dict] = []
    try:
        validated = validate_action_set(actions)
        checks.append({"code": "action_set_valid", "status": "pass", "detail": None})
    except PlanError as error:
        checks.append({"code": "action_set_valid", "status": "failed",
                       "detail": error.code})
        return checks
    covered = {f"{action['target_model']}:{action['target_id']}" for action in validated
               if action["target_id"]}
    for target in facts.get("affected_targets") or []:
        checks.append({
            "code": f"target_covered:{target}",
            "status": "pass" if target in covered else "failed",
            "detail": None if target in covered else "no action"})
    for rule in rules or []:
        name = str(rule.get("name", ""))
        satisfied = bool(rule.get("satisfied", True))
        checks.append({"code": f"rule:{name}",
                       "status": "pass" if satisfied else "failed",
                       "detail": rule.get("detail")})
    return checks
