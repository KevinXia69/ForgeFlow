"""The evidence driver recomputes money from finished flows and all documents."""
import importlib.util
from pathlib import Path

import pytest
from test_decision_metrics import facts
from test_recovery_change_set import stock_margin_fixture

from forgeflow.product.decision_metrics import build_decision_metrics
from forgeflow.recovery.change_set import build_change_set
from forgeflow.recovery.margin import MARGIN_BASIS
from forgeflow.recovery.storage import atomic_json, digest


@pytest.mark.parametrize("stock,unit_cost,raw_price,expected", [(5, 76, 2, "20.0"), (10, 0, 0, None)])
def test_independent_acceptance_calculation_matches_stock_boundaries(tmp_path, stock, unit_cost, raw_price, expected):
    path = Path(__file__).resolve().parents[1] / "scripts/verify_next_phase.py"
    spec = importlib.util.spec_from_file_location("next_phase_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = stock_margin_fixture(stock=stock, unit_cost=unit_cost, raw_price=raw_price)
    inputs, keys = facts(inputs=fixture)
    request, snapshot, blueprint, plan = fixture
    plan["plan"]["objective_value"] = 6  # explicit synthetic solver metric
    changes = build_change_set(request, snapshot, blueprint, plan)
    records = {"snapshot": snapshot, "blueprint": blueprint, "plan": plan, "change-set": changes}
    manifest = {"margin_basis": MARGIN_BASIS, "artifacts": {k: digest(v) for k, v in records.items()},
                "product_costs_sha256": digest(inputs[4])}
    records.update(manifest=manifest, **{"product-costs": inputs[4]})
    for name, value in records.items():
        atomic_json(tmp_path / (name + ".json"), value)
    metrics = build_decision_metrics(*inputs, **keys)
    public = {"plan_id": digest(manifest), "margin_basis": MARGIN_BASIS, "decision_metrics": metrics,
              "margin": None if expected is None else float(expected)}
    result = module.independent_margin(tmp_path, public)
    assert result["stock_units"] == stock and result["new_spend_units"] == 10 - stock
    assert result["margin_pct"] == expected
