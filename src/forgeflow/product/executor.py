"""Executor protocol and deterministic fake for the product approval slice.

A real executor will eventually talk to Odoo and a worker.  This module keeps
the contract narrow: ``prepare(case) -> plan`` and ``apply(case, plan) ->
execution``.  ``FakeProductExecutor`` is intentionally local: it never opens a
network connection or touches the filesystem, so it is safe for unit tests.
"""

from __future__ import annotations

import copy
from typing import Any, Protocol, runtime_checkable

from forgeflow.recovery.failure import failure_projection

from .state import canonical_digest, utc_stamp


class ProductExecutionError(RuntimeError):
    """A stable, client-safe executor failure carrying a machine code."""

    def __init__(self, code: str, *, outcome_unknown: bool = False, failure: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.failure = failure_projection(failure)


@runtime_checkable
class ProductExecutor(Protocol):
    """The one abstraction the API needs to become ERP-independent."""

    live: bool

    def prepare(self, case: dict[str, Any]) -> dict[str, Any]:
        """Read the synthetic case and return a complete, immutable plan."""

    def apply(self, case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        """Apply one approved plan and return the readback/verifier record."""


def _settled_timestamp(case: dict[str, Any]) -> str:
    """Use the case stamp so repeated fake prepares are byte-for-byte equal."""
    stamp = case.get("updated_at") or case.get("created_at")
    return stamp if isinstance(stamp, str) and stamp else utc_stamp()


def _quantity(order: dict[str, Any]) -> int:
    units = order.get("units")
    return units if isinstance(units, int) and not isinstance(units, bool) else 0


def build_fake_plan(case: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic scenario-A plan used by the fake executor."""
    order = case.get("order") if isinstance(case.get("order"), dict) else {}
    disruption = (
        case.get("disruption") if isinstance(case.get("disruption"), dict) else {}
    )
    units = _quantity(order)
    due_days = order.get("due_days") if isinstance(order.get("due_days"), int) else 0
    vendor = disruption.get("vendor") if isinstance(disruption.get("vendor"), str) else ""
    plan_id = canonical_digest(
        {
            "case_id": case.get("case_id"),
            "scenario": case.get("scenario"),
            "order": order,
            "disruption": disruption,
            "revision": case.get("revision"),
        }
    )
    policy_sha256 = canonical_digest(
        {"scenario": case.get("scenario"), "order": order, "policy": "synthetic-v1"}
    )
    snapshot_sha256 = canonical_digest({"order": order, "disruption": disruption})
    # Every number below is synthetic and labeled as such: the fake executor
    # has no real policy, budget, solver or ERP readback.
    checks = [
        {
            "code": "margin",
            "label": "Margin floor",
            "status": "pass",
            "actual": "31.4%",
            "threshold": "27.5%",
            "source": "synthetic scenario A",
        },
        {
            "code": "demand",
            "label": "Demand coverage",
            "status": "pass",
            "actual": f"{units}/{units} units",
            "threshold": f"{units} units",
            "source": "synthetic scenario A",
        },
        {
            "code": "budget_policy",
            "label": "Budget & policy",
            "status": "pass",
            "actual": "synthetic budget and policy check",
            "threshold": "synthetic budget and policy",
            "source": "synthetic executor",
        },
        {
            "code": "on_time",
            "label": "On-time delivery",
            "status": "pass",
            "actual": "planned",
            "threshold": "customer due date",
            "source": "synthetic scenario A",
        },
    ]
    return {
        "plan_id": plan_id,
        "actions": [
            {
                "kind": "cancel_po",
                "document": "PO-0007",
                "summary": f"Cancel the delayed purchase order from {vendor or 'the vendor'}",
                "quantity": units,
                "before": None,
                "after": None,
            },
            {
                "kind": "create_po",
                "document": "PO-0012",
                "summary": "Create a replacement purchase order for the same quantity",
                "quantity": units,
                "before": None,
                "after": None,
            },
            {
                "kind": "adjust_mo",
                "document": "MO-0003",
                "summary": (
                    f"Advance the manufacturing order start to protect the "
                    f"{due_days}-day customer due date"
                ),
                "quantity": units,
                "before": None,
                "after": None,
            },
        ],
        "margin": 31.4,
        "on_time": True,
        "new_spend": 420.0,
        "checks": checks,
        "cost_delta": None,
        "policy_sha256": policy_sha256,
        "snapshot_sha256": snapshot_sha256,
        "prepared_at": _settled_timestamp(case),
    }


def _readback_state(kind: Any) -> str:
    return {
        "cancel_po": "cancel",
        "create_po": "purchase",
        "adjust_mo": "confirmed",
    }.get(kind, "reflected")


class FakeProductExecutor:
    """Deterministic executor used by tests and local demos.

    ``fail_prepare`` and ``fail_apply`` are mutable attributes so a test can
    inject a failure after wiring the fake into the API.
    """

    live = False
    supported_scenarios = ("A",)

    def __init__(
        self,
        *,
        fail_prepare: bool = False,
        fail_apply: bool = False,
    ) -> None:
        self.fail_prepare = fail_prepare
        self.fail_apply = fail_apply
        self.prepare_calls = 0
        self.apply_calls = 0
        self.last_prepare_case: dict[str, Any] | None = None
        self.last_apply_case: dict[str, Any] | None = None
        self.last_apply_plan: dict[str, Any] | None = None
        self.applied_plan_ids: list[str] = []

    def prepare(self, case: dict[str, Any]) -> dict[str, Any]:
        self.prepare_calls += 1
        self.last_prepare_case = copy.deepcopy(case)
        if self.fail_prepare:
            raise ProductExecutionError("prepare_failed")
        return build_fake_plan(case)

    def validate_revision(self, case: dict[str, Any], changes: Any) -> dict[str, Any]:
        """Only the synthetic executor supports this bounded demo revision."""
        if not isinstance(changes, dict) or set(changes) != {"minimum_margin_pct"}:
            raise ProductExecutionError("revision_unsupported")
        value = changes["minimum_margin_pct"]
        previous = (case.get("revision") or {}).get("changes", {}).get("minimum_margin_pct", 27.5)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not previous < value < 100:
            raise ProductExecutionError("revision_must_tighten_constraints")
        return {"minimum_margin_pct": float(value)}

    def prepare_revision(self, case: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        plan = self.prepare(case)
        check = next(row for row in plan["checks"] if row["code"] == "margin")
        check.update(threshold=f"{changes['minimum_margin_pct']:g}%",
                     status="pass" if plan["margin"] >= changes["minimum_margin_pct"] else "block")
        return plan

    def apply(self, case: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        self.apply_calls += 1
        self.last_apply_case = copy.deepcopy(case)
        self.last_apply_plan = copy.deepcopy(plan)
        plan_id = plan.get("plan_id")
        if isinstance(plan_id, str):
            self.applied_plan_ids.append(plan_id)
        if self.fail_apply:
            raise ProductExecutionError("apply_failed")

        actions = plan.get("actions")
        if not isinstance(actions, list):
            actions = []
        writes = []
        records = []
        checks = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = action.get("kind")
            document = action.get("document")
            quantity = action.get("quantity")
            writes.append(
                {"kind": kind, "document": document, "quantity": quantity}
            )
            records.append(
                {
                    "kind": kind,
                    "document": document,
                    "state": _readback_state(kind),
                    "quantity": quantity,
                }
            )
            checks.append(
                {"kind": kind, "document": document, "passed": True}
            )
        return {
            "status": "succeeded",
            "readback": {"records": records, "execution_validated": True},
            "verifier": {"status": "passed", "checks": checks, "strict_success": True,
                         "evaluation_valid": True, "source": "synthetic_executor"},
            "writes": writes,
        }
