"""Versioned contract for the Odoo 20 business adapter."""
from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_MAJOR = 20

#: Every recovery action the adapter may ask the addon to execute.  The addon is
#: the authority on what an action means; this list is the transport allowlist.
ACTION_TYPES: tuple[str, ...] = (
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

#: Actions that create a new ERP document.  They are the ones whose identity is
#: only known after execution, so they are read back by operation id.
CREATING_ACTIONS = frozenset({"create_purchase_order", "create_manufacturing_order"})

REQUIRED_MODELS: dict[str, tuple[str, ...]] = {
    "res.partner": ("name", "supplier_rank", "customer_rank"),
    "product.product": ("name", "default_code", "uom_id", "qty_available",
                        "virtual_available"),
    "purchase.order": ("name", "partner_id", "state", "date_planned", "order_line"),
    "purchase.order.line": ("order_id", "product_id", "product_qty", "date_planned",
                            "qty_received"),
    "sale.order": ("name", "partner_id", "state", "commitment_date"),
    "sale.order.line": ("order_id", "product_id", "product_uom_qty"),
    "stock.quant": ("product_id", "location_id", "quantity", "reserved_quantity"),
    "stock.move": ("product_id", "product_uom_qty", "state", "sale_line_id"),
    "stock.move.line": ("move_id", "product_id", "quantity", "state"),
    "mrp.production": ("name", "product_id", "product_qty", "state", "bom_id",
                       "date_start", "date_finished"),
    "mrp.bom": ("product_tmpl_id", "product_qty", "type"),
    "mrp.bom.line": ("bom_id", "product_id", "product_qty", "uom_id"),
    "mrp.workcenter": ("name", "resource_calendar_id", "code",
                       "time_efficiency"),
    "resource.calendar": ("name",),
    "resource.calendar.attendance": ("calendar_id", "dayofweek", "hour_from",
                                     "hour_to", "duration_hours"),
    "resource.calendar.leaves": ("calendar_id", "date_from", "date_to",
                                 "resource_id"),
    "stock.warehouse": ("name", "code"),
    "uom.uom": ("name", "factor"),
    "mrp.routing.workcenter": ("bom_id", "workcenter_id", "time_cycle",
                               "time_cycle_manual"),
    "mrp.workcenter.capacity": ("workcenter_id", "product_id", "capacity"),
    "mrp.workorder": ("production_id", "workcenter_id", "duration_expected", "state"),
    "forgeflow.exception_event": ("name", "event_type", "occurred_at", "state"),
    "forgeflow.business_rule": ("name", "scope_model", "condition_json", "version",
                                "active", "state", "unit", "currency_id", "valid_from",
                                "valid_to", "confirmed_by_id"),
    "forgeflow.recovery_plan": ("name", "exception_id", "plan_hash", "state", "version",
                                "action_digest", "action_count", "environment_generation",
                                "fact_snapshot_hash", "rule_version"),
    "forgeflow.decision_record": ("plan_id", "decision", "approver_id", "plan_hash",
                                  "plan_version", "plan_digest", "action_digest",
                                  "environment_generation", "execution_identity",
                                  "expires_at"),
    "forgeflow.execution_operation": ("plan_id", "idempotency_key", "state",
                                      "action_id", "fingerprint", "plan_version"),
    "forgeflow.execution_receipt": ("operation_id", "state", "readback_json", "verified"),
    "forgeflow.event_outbox": ("event_type", "external_event_id", "state",
                               "aggregate_model", "aggregate_id", "payload_json"),
}

#: Models an enterprise tenant may read through the planning adapter.  Kept
#: separate from REQUIRED_MODELS because ``ir.module.module`` is infrastructure,
#: not business data, and must not become a general read surface.
ENVIRONMENT_MODELS: dict[str, tuple[str, ...]] = {
    "ir.module.module": ("name", "state", "latest_version"),
}

#: Read-only metadata needed to *decide* a purchase, never to write one.  These
#: models are readable through the adapter but stay out of the write allowlist:
#: a supplier pricelist is an observation, not a recovery action.
PROCUREMENT_READ_MODELS: dict[str, tuple[str, ...]] = {
    "product.supplierinfo": ("id", "partner_id", "product_tmpl_id", "product_id", "min_qty",
                             "price", "delay", "company_id", "currency_id",
                             "date_start", "date_end"),
    "res.currency": ("id", "name"),
    "res.company": ("id", "name", "currency_id"),
    "stock.location": ("id", "usage", "company_id", "name"),
}


@dataclass(frozen=True, slots=True)
class OdooVersion:
    major: int
    raw: str


@dataclass(frozen=True, slots=True)
class ContractCheck:
    ok: bool
    version: OdooVersion | None
    missing_models: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnvironmentGeneration:
    """An identity for the ERP environment an approval is bound to."""

    generation: str
    database: str
    version: str
    module_count: int
