"""Regression tests for the third review round (commit dd9ff25).

Each test fails on dd9ff25 and passes after its fix, or states plainly which
behaviour it pins.  The four findings were:

1. the dispatcher read ``self.resolver`` that its constructor never set, and the
   ambiguous branch passed ``options=`` to ``open_attention``, whose interface has
   no such argument;
2. reference extraction rewrote Odoo's ``P00046`` to ``PO46``;
3. the rule epoch hashed only name/version/hash/model, so a body edit, a scope
   change or an expiring rule left old approvals valid;
4. one unknown budget removed every other order's cap in the same currency.
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from forgeflow.planning.multi_bom import BomNode
from forgeflow.planning.recovery import (
    DemandLine,
    Period,
    ProductPlanInput,
    RecoveryPlanner,
    SupplierOffer,
    Workcenter,
)
from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Dispatcher,
    Role,
    Router,
    TenantContext,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not DATABASE_URL,
                                 reason="FORGEFLOW_CONTROL_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parents[1]
RULE_EPOCH_PATH = ROOT / "odoo-addons/forgeflow_general/models/rule_epoch.py"


def _load_rule_epoch():
    spec = importlib.util.spec_from_file_location("forgeflow_rule_epoch", RULE_EPOCH_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


NOW = "2026-11-08T12:00:00"


# ------------------------------------------------------- 1. object references
def test_odoo_default_purchase_name_survives_extraction() -> None:
    from forgeflow.superagent.workers import _object_ref_in

    assert _object_ref_in("P00046 供应商取消") == "purchase.order:P00046"
    assert _object_ref_in("采购单 46 延期") == "purchase.order:46"
    # A self-describing prefix is still returned exactly as written.
    assert _object_ref_in("PO-12 is late") == "PO-12"


# ------------------------------------------------------- 2. dispatcher resolver
class _StubResolver:
    def __init__(self, resolution) -> None:
        self.resolution = resolution
        self.references: list[str] = []

    def resolve(self, reference: str):
        self.references.append(reference)
        return self.resolution


def _candidate(identifier: int, name: str, company: int = 1):
    from forgeflow.integrations.odoo20.resolver import Candidate

    return Candidate("purchase.order", identifier, name, name, company, "name")


def _tenant(prefix: str):
    tenant = f"{prefix}-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, prefix, 1, (("planner-a", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    return tenant, store, ctx, ControlEngine(store)


def _ticket_with_route_effect(store, engine, ctx, tenant, message: str) -> str:
    admitted = engine.admit_trigger(ctx, source_id="test", external_event_id=f"r-{uuid.uuid4()}",
                                    payload_hash="b" * 64, object_ref="purchase.order:1",
                                    goal="route")
    first = engine.claim_effect("setup", tenant_id=tenant)
    engine.complete_effect(effect_id=first["id"], tenant_id=tenant,
                           fencing_token=first["fencing_token"])
    with store.transaction() as cursor:
        cursor.execute(
            "INSERT INTO control_effects(id, tenant_id, ticket_id, kind, payload, "
            "side_effect_class) VALUES (%s, %s, %s, 'route_decision', %s, "
            "'model_inference')",
            (str(uuid.uuid4()), tenant, admitted.ticket_id, Jsonb({"message": message})))
    return admitted.ticket_id


@requires_db
def test_route_decision_uses_the_configured_resolver() -> None:
    """dd9ff25 raised AttributeError: Dispatcher never set ``self.resolver``."""
    from forgeflow.integrations.odoo20.resolver import Resolution

    tenant, store, ctx, engine = _tenant("resolver")
    try:
        ticket_id = _ticket_with_route_effect(store, engine, ctx, tenant,
                                              "P00046 is late")
        resolution = Resolution(reference="P00046", model_hint="purchase.order",
                                candidates=[_candidate(900, "P00046")])
        resolver = _StubResolver(resolution)
        result = Dispatcher(store, Router(), resolver=resolver).tick(
            "dispatcher-r", tenant_id=tenant)
        assert result is not None and result["status"] == "completed"
        assert result["decision"] == "start_investigation"
        assert result["resolution"]["status"] == "resolved"
        assert result["resolution"]["resolved"]["id"] == 900
        # The hint is carried to the resolver, which splits it from the name.
        assert resolver.references == ["purchase.order:P00046"]
        events = engine.ticket_events(ctx, ticket_id)
        decision = next(row for row in events if row["kind"] == "router_decision")
        assert decision["payload"]["resolution"]["status"] == "resolved"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


@requires_db
def test_ambiguous_reference_opens_attention_with_options_in_detail() -> None:
    """The ambiguous branch called open_attention(options=...), which it does not take."""
    from forgeflow.integrations.odoo20.resolver import Resolution

    tenant, store, ctx, engine = _tenant("ambiguity")
    try:
        ticket_id = _ticket_with_route_effect(store, engine, ctx, tenant, "PO-500 is late")
        resolution = Resolution(reference="PO-500", model_hint="purchase.order",
                                candidates=[_candidate(901, "PO-500"),
                                            _candidate(902, "PO-500")])
        result = Dispatcher(store, Router(), resolver=_StubResolver(resolution)).tick(
            "dispatcher-a", tenant_id=tenant)
        assert result is not None and result["resolution"]["status"] == "ambiguous"
        attentions = engine.list_attentions(ctx, state="open")
        assert len(attentions) == 1
        assert str(attentions[0]["ticket_id"]) == ticket_id
        detail = attentions[0]["detail"]
        assert [item["id"] for item in detail["options"]] == [901, 902]
        assert detail["reference"] == "PO-500"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


# ---------------------------------------------------------------- 3. rule epoch
def test_editing_a_confirmed_body_moves_the_epoch_without_a_version_bump() -> None:
    module = _load_rule_epoch()
    base = {"name": "cap", "scope_model": "purchase.order", "version": 1,
            "condition_json": '{"lte":{"field":"spend","value":100}}'}
    before = module.epoch([base], now=NOW)
    edited = dict(base, condition_json='{"lte":{"field":"spend","value":200}}')
    assert module.epoch([edited], now=NOW) != before


def test_formatting_alone_is_not_a_rule_change() -> None:
    module = _load_rule_epoch()
    first = {"name": "cap", "scope_model": "purchase.order", "version": 1,
             "condition_json": '{"lte": {"field": "spend", "value": 100}}'}
    second = dict(first, condition_json='{"lte":{"field":"spend","value":100}}')
    assert module.epoch([first], now=NOW) == module.epoch([second], now=NOW)


@pytest.mark.parametrize("field,value", [
    ("scope_ref", "PO-9"),
    ("unit", "kg"),
    ("currency", "CNY"),
    ("valid_from", "2026-11-01 00:00:00"),
    ("valid_to", "2026-12-31 00:00:00"),
    ("source_ref", "contract-7"),
])
def test_scope_unit_currency_and_validity_are_part_of_the_epoch(field, value) -> None:
    module = _load_rule_epoch()
    base = {"name": "cap", "scope_model": "purchase.order", "version": 1,
            "condition_json": '{"lte":{"field":"spend","value":100}}'}
    assert module.epoch([dict(base, **{field: value})], now=NOW) != module.epoch(
        [base], now=NOW)


def test_an_expiring_rule_leaves_the_epoch_when_its_window_closes() -> None:
    module = _load_rule_epoch()
    rule = {"name": "cap", "scope_model": "purchase.order", "version": 1,
            "condition_json": '{"lte":{"field":"spend","value":100}}',
            "valid_from": "2026-11-01 00:00:00", "valid_to": "2026-11-08 12:00:00"}
    assert module.epoch([rule], now="2026-11-08T11:59:59") != module.epoch(
        [rule], now="2026-11-08T12:00:00")
    # And coming back is not the same epoch as never having left.
    assert module.epoch([rule], now="2026-11-08T12:00:00") == module.epoch(
        [], now="2026-11-08T12:00:00")


def test_a_rule_becoming_effective_changes_the_epoch() -> None:
    module = _load_rule_epoch()
    rule = {"name": "cap", "scope_model": "purchase.order", "version": 1,
            "condition_json": '{"lte":{"field":"spend","value":100}}',
            "valid_from": "2026-12-01 00:00:00"}
    assert module.epoch([rule], now="2026-11-08T12:00:00") != module.epoch(
        [rule], now="2026-12-01T00:00:00")


def test_a_malformed_validity_is_open_ended_not_silently_dropped() -> None:
    module = _load_rule_epoch()
    rule = {"name": "cap", "scope_model": "purchase.order", "version": 1,
            "condition_json": '{"lte":{"field":"spend","value":100}}',
            "valid_to": "not-a-date"}
    assert module.is_effective(rule["valid_to"], None, NOW) is True
    assert module.epoch([rule], now=NOW) != module.epoch([], now=NOW)


def test_a_stale_cached_hash_does_not_hide_a_body_change() -> None:
    module = _load_rule_epoch()
    base = {"name": "cap", "scope_model": "purchase.order", "version": 1,
            "condition_json": '{"lte":{"field":"spend","value":100}}',
            "rule_hash": "abc"}
    edited = dict(base, condition_json='{"lte":{"field":"spend","value":999}}')
    assert module.epoch([edited], now=NOW) != module.epoch([base], now=NOW)


# ------------------------------------------------------------ 4. mixed budgets
def _budget_planner(demands, *, price=100.0, hours=0.0):
    return RecoveryPlanner(
        periods=[Period(0, "w1")],
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=demands,
        workcenters=[Workcenter("press", {0: hours})],
        offers=[SupplierOffer("sup", "FG", price, lead_periods=0)])


def test_unknown_budget_does_not_release_another_orders_cap() -> None:
    plan = _budget_planner([
        DemandLine("SO1", "FG", 10, 0, 1, budget_cap=500.0),
        DemandLine("SO2", "FG", 10, 0, 1, budget_cap=None),
    ]).solve()
    assert plan.solved is True
    # SO1 can buy five units (5 x 100 = 500); SO2 is uncapped and buys ten.
    assert plan.cost_by_currency == {"USD": 1500.0}
    assert plan.unmet["SO1"] == 5
    assert plan.unmet["SO2"] == 0
    assert "budget_unknown:SO2" in plan.assumptions


def test_two_known_caps_in_one_currency_are_each_enforced() -> None:
    plan = _budget_planner([
        DemandLine("SO1", "FG", 10, 0, 1, budget_cap=300.0),
        DemandLine("SO2", "FG", 10, 0, 1, budget_cap=700.0),
    ]).solve()
    assert plan.cost_by_currency == {"USD": 1000.0}
    assert plan.unmet["SO1"] == 7
    assert plan.unmet["SO2"] == 3


def test_a_cap_in_another_currency_is_not_compared_without_a_rate() -> None:
    planner = RecoveryPlanner(
        periods=[Period(0, "w1")],
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=[DemandLine("SO1", "FG", 10, 0, 1, budget_cap=500.0, currency="USD")],
        workcenters=[Workcenter("press", {0: 0.0})],
        offers=[SupplierOffer("sup", "FG", 100.0, currency="EUR", lead_periods=0)])
    plan = planner.solve()
    # The purchase is needed, but a EUR price cannot be held to a USD cap.
    assert plan.unmet["SO1"] == 0
    assert any(note.startswith("budget_currency_mismatch:SO1:EUR")
               for note in plan.assumptions)
    assert plan.trustworthy is False


def test_a_minimum_order_surplus_does_not_force_a_stockout() -> None:
    """Demand 3, MOQ 5, enough budget: buy 5, keep 2 as stock, charge all 5."""
    planner = RecoveryPlanner(
        periods=[Period(0, "w1")],
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=[DemandLine("SO1", "FG", 3, 0, 1, budget_cap=50.0)],
        workcenters=[Workcenter("press", {0: 0.0})],
        offers=[SupplierOffer("sup", "FG", 10.0, min_order_quantity=5,
                              lead_periods=0)])
    plan = planner.solve()
    assert plan.unmet["SO1"] == 0
    assert sum(plan.buy["FG"]["sup"].values()) == 5
    # The whole purchase is charged, not only the three allocated units.
    assert plan.cost_by_currency == {"USD": 50.0}


def test_a_budget_below_the_minimum_order_buys_nothing() -> None:
    planner = RecoveryPlanner(
        periods=[Period(0, "w1")],
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=[DemandLine("SO1", "FG", 3, 0, 1, budget_cap=40.0)],
        workcenters=[Workcenter("press", {0: 0.0})],
        offers=[SupplierOffer("sup", "FG", 10.0, min_order_quantity=5,
                              lead_periods=0)])
    plan = planner.solve()
    assert plan.unmet["SO1"] == 3
    assert sum(sum(values.values()) for values in plan.buy["FG"].values()) == 0
    assert plan.total_cost() == 0.0
