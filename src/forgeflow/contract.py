"""A versioned task policy contract that a recovery request must satisfy.

The evolved agent used to derive its own parameters from whatever it could read,
and one successful run still took the sales-order untaxed amount as the budget
instead of the budget stated in the task book. This module turns the intended
policy into an explicit, reviewable object and checks a request against it
before any solve or write happens.

Rules of construction:
* Values come from the task instruction, the validated task configuration and
  business reads only; never from ``solution/`` or verifier internals.
* Every field records where it came from, so a reviewer can audit a contract
  without re-reading the whole task.
* The contract is compared literally. A value that merely happens to be
  tighter (the B run's smaller budget) is reported as a conflict, not accepted.
* A contract is bound to one task, one product, one anchor date and one
  disruption identity. Reusing it for another task or range is an error.

Physical actions are out of scope by construction: the executor only accepts
the ten planning-document operations in ``recovery.journal.KINDS``, so no
contract can authorize a goods receipt, a production completion or an
inventory adjustment. The contract therefore bans operation *kinds* that the
executor could actually perform, not invented physical action names.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = 1
KIND = "forgeflow.task_contract"
CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
KINDS = ("supplier_cancellation", "workcenter_outage")
ACTIONS = ("keep", "update", "cancel", "create")
GRANULARITIES = ("day",)


class ContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskContract:
    """The authorized policy for one task, task version and anchor date."""

    contract_id: str
    task_id: str
    task_type: str
    product_code: str
    anchor_date: str
    margin: float
    margin_basis: str
    time_granularity: str
    budgets: MappingProxyType
    sales_order_ids: tuple[int, ...]
    disruption: MappingProxyType
    forbidden_workcenters: tuple[str, ...]
    forbidden_operation_kinds: tuple[str, ...]
    original_commitment_policy: str
    sources: MappingProxyType
    expires_at_utc: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "contract_id": self.contract_id,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "product_code": self.product_code,
            "anchor_date": self.anchor_date,
            "margin": self.margin,
            "margin_basis": self.margin_basis,
            "time_granularity": self.time_granularity,
            "budgets": dict(sorted(self.budgets.items())),
            "sales_order_ids": list(self.sales_order_ids),
            "disruption": dict(sorted(self.disruption.items())),
            "forbidden_workcenters": list(self.forbidden_workcenters),
            "forbidden_operation_kinds": list(self.forbidden_operation_kinds),
            "original_commitment_policy": self.original_commitment_policy,
            "sources": dict(sorted(self.sources.items())),
            "expires_at_utc": self.expires_at_utc,
        }

    def digest(self) -> str:
        # Same canonical form as recovery.storage.digest, so a policy.json
        # written by the executor hashes to the same value the approval binds.
        encoded = json.dumps(self.payload(), sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        return hashlib.sha256(encoded.encode()).hexdigest()


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _budget(value: object, field: str) -> float:
    _need(
        not isinstance(value, bool) and isinstance(value, (int, float))
        and math.isfinite(value) and value > 0,
        f"{field} must be a positive finite number",
    )
    return float(value)


def build_contract(
    *,
    contract_id: str,
    task_id: str,
    task_type: str,
    product_code: str,
    anchor_date: str,
    margin: float,
    margin_basis: str,
    budgets: dict[str, float],
    sales_order_ids: tuple[int, ...] | list[int],
    disruption: dict[str, str],
    forbidden_workcenters: tuple[str, ...] | list[str] = (),
    forbidden_operation_kinds: tuple[str, ...] | list[str] = (),
    original_commitment_policy: str,
    sources: MappingProxyType,
    time_granularity: str = "day",
    expires_at_utc: str | None = None,
) -> TaskContract:
    """Validate every field once and return an immutable contract."""
    _need(bool(CODE.fullmatch(contract_id)), "invalid contract_id")
    _need(bool(CODE.fullmatch(task_id)), "invalid task_id")
    _need(task_type in KINDS, "invalid task_type")
    _need(isinstance(product_code, str) and bool(product_code), "invalid product_code")
    _need(time_granularity in GRANULARITIES, "invalid time_granularity")
    try:
        parsed_anchor = date.fromisoformat(anchor_date)
    except (TypeError, ValueError) as error:
        raise ContractError("invalid anchor_date") from error
    _need(parsed_anchor.isoformat() == anchor_date, "anchor_date must be YYYY-MM-DD")
    _need(
        not isinstance(margin, bool) and isinstance(margin, (int, float))
        and math.isfinite(margin) and 0 < margin < 1,
        "margin must be a finite fraction in (0, 1)",
    )
    _need(isinstance(margin_basis, str) and bool(margin_basis), "invalid margin_basis")
    ids = tuple(sales_order_ids)
    _need(bool(ids), "sales_order_ids must not be empty")
    _need(all(type(item) is int and item > 0 for item in ids), "invalid sales_order_ids")
    _need(len(set(ids)) == len(ids), "duplicate sales_order_ids")
    expected_budget_keys = {str(item) for item in ids}
    _need(set(budgets) == expected_budget_keys, "budgets must cover exactly the order ids")
    clean_budgets = {key: _budget(value, f"budget {key}") for key, value in budgets.items()}
    _need(isinstance(disruption, dict) and disruption.get("kind") == task_type,
          "disruption kind must match task_type")
    if task_type == "supplier_cancellation":
        _need(bool(disruption.get("vendor_ref")), "supplier contract needs vendor_ref")
        _need(bool(disruption.get("vendor_name")), "supplier contract needs vendor_name")
    else:
        _need(bool(disruption.get("workcenter_code")), "outage contract needs workcenter_code")
        _need(bool(disruption.get("workcenter_name")), "outage contract needs workcenter_name")
    known_kinds = {"purchase_order", "manufacturing_order"}
    _need(
        all(isinstance(item, str) and item in known_kinds for item in forbidden_operation_kinds),
        "invalid forbidden_operation_kinds; expected planning document kinds",
    )
    _need(
        isinstance(original_commitment_policy, str) and bool(original_commitment_policy),
        "invalid original_commitment_policy",
    )
    _need(isinstance(sources, dict) and bool(sources), "contract needs sources")
    _need(all(isinstance(key, str) and isinstance(value, str) for key, value in sources.items()),
          "sources must map field names to references")
    if expires_at_utc is not None:
        try:
            expires = datetime.fromisoformat(expires_at_utc)
        except (TypeError, ValueError) as error:
            raise ContractError("invalid expires_at_utc") from error
        _need(expires.tzinfo is not None, "expires_at_utc must include a timezone")
    return TaskContract(
        contract_id=contract_id,
        task_id=task_id,
        task_type=task_type,
        product_code=product_code,
        anchor_date=anchor_date,
        margin=float(margin),
        margin_basis=margin_basis,
        time_granularity=time_granularity,
        budgets=MappingProxyType(clean_budgets),
        sales_order_ids=tuple(sorted(ids)),
        disruption=MappingProxyType(dict(disruption)),
        forbidden_workcenters=tuple(sorted(forbidden_workcenters)),
        forbidden_operation_kinds=tuple(sorted(forbidden_operation_kinds)),
        original_commitment_policy=original_commitment_policy,
        sources=MappingProxyType(dict(sources)),
        expires_at_utc=expires_at_utc,
    )


def contract_from_payload(payload: dict[str, Any]) -> TaskContract:
    """Rebuild a contract from stored JSON, rejecting a different schema."""
    _need(isinstance(payload, dict), "contract payload must be an object")
    if "forbidden_actions" in payload and "forbidden_operation_kinds" not in payload:
        raise ContractError(
            "unsupported_legacy_field: forbidden_actions was renamed to "
            "forbidden_operation_kinds and must be migrated, not ignored."
        )
    _need(payload.get("schema_version") == SCHEMA_VERSION, "unsupported contract schema")
    _need(payload.get("kind") == KIND, "not a task contract")
    fields = {
        key: payload.get(key) for key in (
            "contract_id", "task_id", "task_type", "product_code", "anchor_date", "margin",
            "margin_basis", "time_granularity", "budgets", "sales_order_ids", "disruption",
            "forbidden_workcenters", "forbidden_operation_kinds", "original_commitment_policy",
            "sources", "expires_at_utc",
        )
    }
    return build_contract(**fields)


def _budget_order(key: object) -> tuple[int, int, str]:
    """Total order over budget keys of any type, so sorting never raises."""
    if isinstance(key, str) and key.isdigit():
        return (0, int(key), "")
    return (1, 0, str(key))


def check_request(
    request: dict[str, Any],
    contract: TaskContract,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compare a validated request with the contract.

    Returns a report instead of raising: the caller can show every conflict at
    once, which matters when the model proposes a budget and a margin that are
    both wrong. ``ok`` is true only when there are no issues.
    """
    issues: list[dict[str, Any]] = []

    def conflict(code: str, field: str, expected: object, observed: object) -> None:
        issues.append({
            "code": code, "field": field,
            "expected": expected, "observed": observed,
        })

    if not isinstance(request, dict):
        return {
            "ok": False,
            "contract_id": contract.contract_id,
            "contract_sha256": contract.digest(),
            "issues": [{
                "code": "request_unusable", "field": "request",
                "expected": "object", "observed": type(request).__name__,
            }],
        }
    if contract.expires_at_utc is not None:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            conflict("invalid_reference_time", "now", "timezone-aware", str(now))
        elif moment.astimezone(UTC) >= datetime.fromisoformat(contract.expires_at_utc).astimezone(UTC):
            conflict("contract_expired", "contract", contract.expires_at_utc, moment.isoformat())

    observed_product = request.get("target_product_code")
    if observed_product != contract.product_code:
        conflict("product_mismatch", "target_product_code",
                 contract.product_code, observed_product)

    observed_anchor = request.get("anchor_date")
    if observed_anchor != contract.anchor_date:
        conflict("anchor_date_mismatch", "anchor_date", contract.anchor_date, observed_anchor)

    observed_ids = request.get("sales_order_ids")
    scope_usable = isinstance(observed_ids, list) and all(type(item) is int for item in observed_ids)
    observed_scope: set[int] = set()
    if not scope_usable:
        conflict("order_scope_unusable", "sales_order_ids", list(contract.sales_order_ids), observed_ids)
    else:
        expected = set(contract.sales_order_ids)
        observed_scope = set(observed_ids)
        missing = sorted(expected - observed_scope)
        extra = sorted(observed_scope - expected)
        if missing:
            conflict("order_scope_missing", "sales_order_ids", sorted(expected), missing)
        if extra:
            conflict("order_scope_extra", "sales_order_ids", sorted(expected), extra)
        if len(observed_scope) != len(observed_ids):
            conflict("order_scope_duplicate", "sales_order_ids", sorted(observed_scope),
                     sorted(observed_ids))

    observed_margin = request.get("margin")
    if (not isinstance(observed_margin, (int, float)) or isinstance(observed_margin, bool)
            or not math.isfinite(observed_margin)
            or not math.isclose(observed_margin, contract.margin, rel_tol=0, abs_tol=1e-9)):
        conflict("margin_mismatch", "margin", contract.margin, observed_margin)

    caps = request.get("sales_order_budget_caps")
    if not isinstance(caps, dict):
        conflict("budget_unusable", "sales_order_budget_caps", contract.budgets, caps)
    else:
        # Budget for an order outside the contract scope is a scope error, not a
        # second budget error; one root cause, one issue.
        for key in sorted(contract.budgets, key=_budget_order):
            if scope_usable and int(key) not in observed_scope:
                continue
            observed = caps.get(key)
            if observed is None:
                conflict("budget_missing", f"sales_order_budget_caps.{key}",
                         contract.budgets[key], None)
            elif (not isinstance(observed, (int, float)) or isinstance(observed, bool)
                  or not math.isfinite(observed)
                  or not math.isclose(observed, contract.budgets[key], rel_tol=0, abs_tol=1e-6)):
                conflict("budget_mismatch", f"sales_order_budget_caps.{key}",
                         contract.budgets[key], observed)
        for key in sorted(set(caps) - set(contract.budgets), key=_budget_order):
            if (scope_usable and isinstance(key, str) and key.isdigit()
                    and key == str(int(key)) and int(key) in observed_scope):
                continue
            conflict("budget_unknown_order", f"sales_order_budget_caps.{key}", None, caps[key])

    disruption = request.get("disruption")
    if not isinstance(disruption, dict):
        conflict("disruption_unusable", "disruption", contract.disruption, disruption)
    else:
        observed_kind = disruption.get("kind")
        kind_matches = observed_kind == contract.task_type
        if not kind_matches:
            conflict("disruption_type_mismatch", "disruption.kind",
                     contract.task_type, observed_kind)
        elif contract.task_type == "supplier_cancellation":
            observed_ref = disruption.get("vendor_ref")
            expected_ref = contract.disruption["vendor_ref"]
            if observed_ref != expected_ref:
                code = (
                    "identifier_is_display_name"
                    if observed_ref == contract.disruption.get("vendor_name")
                    else "vendor_ref_mismatch"
                )
                conflict(code, "disruption.vendor_ref", expected_ref, observed_ref)
        else:
            observed_code = disruption.get("workcenter_code")
            expected_code = contract.disruption["workcenter_code"]
            if observed_code != expected_code:
                code = (
                    "identifier_is_display_name"
                    if observed_code == contract.disruption.get("workcenter_name")
                    else "workcenter_ref_mismatch"
                )
                conflict(code, "disruption.workcenter_code", expected_code, observed_code)

    return {
        "ok": not issues,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.digest(),
        "issues": issues,
    }


def _workcenter_codes(value: object) -> tuple[list[str], int]:
    """Collect workcenter_code strings anywhere in a projection.

    The change-set nests workcenter choices under workorder plans, so a check
    that only reads ``after.workcenter_code`` can be bypassed by a nested one.
    Returns the codes plus the count of unreadable values found under that key.
    """
    codes: list[str] = []
    unreadable = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "workcenter_code":
                if isinstance(item, str):
                    codes.append(item)
                elif item is not None:
                    unreadable += 1
            elif isinstance(item, (dict, list)):
                nested, bad = _workcenter_codes(item)
                codes.extend(nested)
                unreadable += bad
    elif isinstance(value, list):
        for item in value:
            nested, bad = _workcenter_codes(item)
            codes.extend(nested)
            unreadable += bad
    return codes, unreadable


def check_changes(changes: dict[str, Any], contract: TaskContract) -> dict[str, Any]:
    """Reject a change set that contradicts the contract's physical constraints.

    Only claims the artifacts can support are checked: a downed work center must
    not be scheduled by any non-cancel operation, and a forbidden planning
    document kind must not appear at all. Physical actions are already
    impossible at the executor layer (see the module docstring).
    """
    _need(isinstance(changes, dict), "change set must be an object")
    operations = changes.get("operations")
    _need(isinstance(operations, list), "change set must contain operations")
    issues: list[dict[str, Any]] = []
    forbidden = set(contract.forbidden_workcenters)
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            issues.append({"code": "operation_unreadable", "field": f"operations.{index}"})
            continue
        action = operation.get("action")
        projected = operation.get("after")
        if action != "cancel" and not isinstance(projected, dict):
            issues.append({
                "code": "operation_missing_projection",
                "field": f"operations.{index}.after",
            })
        after = projected if isinstance(projected, dict) else {}
        codes, unreadable = _workcenter_codes(after)
        if unreadable:
            issues.append({
                "code": "workcenter_code_unreadable",
                "field": f"operations.{index}.after",
            })
        if action != "cancel":
            for code in codes:
                if code in forbidden:
                    issues.append({
                        "code": "forbidden_workcenter_in_plan",
                        "field": f"operations.{index}.after.workcenter_code",
                        "expected": sorted(forbidden), "observed": code,
                    })
        kind = operation.get("kind")
        if not isinstance(kind, str):
            issues.append({
                "code": "operation_kind_unreadable",
                "field": f"operations.{index}.kind",
            })
        elif kind in contract.forbidden_operation_kinds:
            issues.append({
                "code": "forbidden_operation_kind",
                "field": f"operations.{index}.kind",
                "expected": list(contract.forbidden_operation_kinds), "observed": kind,
            })
        if action not in ACTIONS:
            issues.append({
                "code": "unknown_operation_action",
                "field": f"operations.{index}.action",
                "expected": list(ACTIONS), "observed": action,
            })
    return {
        "ok": not issues,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.digest(),
        "issues": issues,
    }
