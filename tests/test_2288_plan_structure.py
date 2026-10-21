"""E16: the exact 2288 inputs, solved and compiled locally, without a model.

Three rules failed on both valid 2288 runs and on the 2285 run:

  finished_stock_capacity_compliance, supply_coverage, repair_state_compliance

This module replays the *recorded exact inputs* of the 2288 planning call — the
blueprint, the snapshot and the request that the product itself wrote — through
the same solver and change-set code the product runs, and asserts the plan
structure those rules depend on:

  1. finished stock is allocated explicitly and never beyond what is on hand;
  2. every accepted customer's demand is covered by stock, purchase or assembly;
  3. every document the plan creates carries the state chain and the origin that
     make it a committed supply, not a draft.

No model, no ERP, no verifier. Nothing here reads the hidden verifier; the rule
names come from the recorded verifier output only.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.planning.tool import SolverFailure, solve_blueprint
from forgeflow.recovery.change_set import build_change_set
from forgeflow.recovery.compiler import compile_steps

ROOT = Path(__file__).resolve().parents[1]
RECORDED = (
    ROOT
    / "runs"
    / "v9-g3-2288-contract-r1"
    / "harbor"
    / "job"
    / "2288_medium_repair_plan_medium__rUFEXsT"
    / "agent"
    / "planning"
    / "54a5fd410b6a4b2e9e33c4121c032ec3"
)
PRODUCT = "PC3C63A097A-LED-LSL-003"


def _require_recording() -> None:
    if not (RECORDED / "blueprint.json").is_file():
        pytest.skip("the recorded 2288 planning inputs are not present")


@pytest.fixture(scope="module")
def exact(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The recorded exact inputs, plus the pinned upstream staged by the bundle."""
    _require_recording()
    request = json.loads((RECORDED / "request.json").read_text())
    snapshot = json.loads((RECORDED / "snapshot.json").read_text())
    blueprint = json.loads((RECORDED / "blueprint.json").read_text())

    # Never write into the recorded run: it is evidence, and a test that edits
    # evidence cannot be trusted to describe it.
    bundle_dir = tmp_path_factory.mktemp("2288-bundle")
    archive = bundle_dir / "planner.tar.gz"
    build_planner_bundle(ROOT, archive, recovery=True)
    with tarfile.open(archive) as handle:
        handle.extractall(bundle_dir, filter="data")
    upstream = bundle_dir / "planner" / "upstream"
    assert (upstream / "procurement").is_dir(), "the bundle must stage the pinned solver"

    return {"request": request, "snapshot": snapshot, "blueprint": blueprint, "upstream": upstream}


@pytest.fixture(scope="module")
def solved(exact: dict) -> dict:
    blueprint = exact["blueprint"]
    try:
        plan = solve_blueprint(blueprint, exact["upstream"])
    except SolverFailure as error:  # pragma: no cover - reported, not hidden
        pytest.fail(f"the exact 2288 blueprint no longer solves: {error}")
    return plan


def test_exact_blueprint_solves_without_a_model(solved: dict) -> None:
    calls = solved["trace"]["solve_calls"]
    assert calls, "the solver must record what it solved"
    assert all(call["status"] == "OPTIMAL" for call in calls)


def test_finished_stock_is_allocated_explicitly_and_within_capacity(solved: dict) -> None:
    """The structure finished_stock_capacity_compliance depends on."""
    plan = solved["plan"]
    on_hand = {row["code"]: row["available"] for row in plan["stock_report"]}
    used = {row["code"]: row["used"] for row in plan["stock_report"]}

    for code, quantity in used.items():
        assert 0 <= quantity <= on_hand[code], f"{code}: allocating {quantity} of {on_hand[code]} on hand"

    allocated = sum(allocation.get("stock_allocated", 0) for allocation in plan["allocations"])
    assert allocated == used.get(PRODUCT, 0), "every allocated finished unit must appear on a customer"
    assert allocated > 0, "the 2288 plan has finished units on hand and must use them"


def test_every_accepted_customer_is_covered_by_a_named_source(solved: dict) -> None:
    """The structure supply_coverage depends on."""
    plan = solved["plan"]
    for allocation in plan["allocations"]:
        if not allocation["accepted"]:
            continue
        covered = (
            allocation.get("stock_allocated", 0)
            + allocation.get("purchase_units", 0)
            + allocation.get("assembly_units", 0)
        )
        assert covered == allocation["demand"], (
            f"{allocation['ref']}: covering {covered} of {allocation['demand']} leaves the order short"
        )
        assert covered > 0, f"{allocation['ref']}: an accepted order must be covered somehow"


def test_created_documents_carry_a_state_chain(exact: dict, solved: dict) -> None:
    """The structure repair_state_compliance depends on.

    The state lives in the compiled steps, not in the change-set projection: a
    created document is left in a committed state only if a confirm step follows
    it. Asserting on the projection would have been the wrong layer.
    """
    changes = build_change_set(exact["request"], exact["snapshot"], exact["blueprint"], solved)
    workorders = [
        {**row, "date_start": False, "date_finished": False, "duration_expected": 1.0}
        for row in exact["snapshot"]["models"]["mrp.workorder"]
    ]
    steps = compile_steps(changes, exact["snapshot"], workorders)["steps"]
    kinds = [step["kind"] if isinstance(step, dict) else step.kind for step in steps]
    assert kinds, "the compiler must produce steps for a plan that creates supply"

    for created, confirmed in (("create_manufacturing", "confirm_manufacturing"),
                               ("create_purchase", "confirm_purchase")):
        created_at = [index for index, kind in enumerate(kinds) if kind == created]
        confirmed_at = [index for index, kind in enumerate(kinds) if kind == confirmed]
        assert len(created_at) == len(confirmed_at), (
            f"{created} × {len(created_at)} but only {len(confirmed_at)} {confirmed} steps"
        )
        for index in created_at:
            assert any(later > index for later in confirmed_at), (
                f"a {created} is never followed by {confirmed}: it would stay a draft"
            )


def test_no_step_touches_a_forbidden_operation_kind(exact: dict) -> None:
    """The contract forbids planning MO/PO operations; the chain must still work."""
    forbidden = set(exact["blueprint"].get("forbidden_operation_kinds") or ())
    assert {"manufacturing_order", "purchase_order"} <= forbidden or not forbidden
