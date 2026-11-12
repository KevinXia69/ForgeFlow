"""Resolve what a user actually means by "PO-123", and what it really affects.

Two rules drive this module:

* A number inside a document name is **not** a database id.  ``PO-123`` names the
  record whose ``name`` is ``PO-123``; the record may have any id.  The previous
  behaviour turned the digits straight into ``purchase.order:123``, which silently
  pointed at whichever unrelated order happened to own that id.
* A shared supplier is not a customer.  Impact is derived from supply/demand
  relations (stock moves, procurements, BOM demand), never from a partner id, and
  the result says how strong each relation is instead of declaring everything
  directly affected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

#: Prefix -> model.  Order matters: the longest prefixes are tried first.
REFERENCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("PICK", "stock.picking"),
    ("INV", "account.move"),
    ("PO", "purchase.order"),
    ("SO", "sale.order"),
    ("MO", "mrp.production"),
    ("GL", "account.move.line"),
)

#: Models that can carry an ``object_ref`` in a ticket.
SUPPORTED_MODELS = frozenset(model for _, model in REFERENCE_PREFIXES)

#: How strong a relation is.  ``direct`` may drive a plan; the other two may not
#: without further evidence.
DIRECT = "direct"
POTENTIAL = "potential"
UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Candidate:
    model: str
    id: int
    name: str
    display_name: str
    company_id: int | None
    matched_by: str
    in_company_scope: bool = True
    #: A partner's own document number (vendor reference / customer PO).  It is
    #: not the record name and never the id.
    external_ref: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.model}:{self.id}"

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "id": self.id, "name": self.name,
                "display_name": self.display_name, "company_id": self.company_id,
                "matched_by": self.matched_by,
                "in_company_scope": self.in_company_scope,
                "external_ref": self.external_ref}


@dataclass
class Resolution:
    """Outcome of resolving one reference string."""

    reference: str
    model_hint: str | None
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    truncated: bool = False

    @property
    def resolved(self) -> Candidate | None:
        in_scope = [item for item in self.candidates if item.in_company_scope]
        return in_scope[0] if len(in_scope) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len([item for item in self.candidates
                    if item.in_company_scope]) > 1

    def as_dict(self) -> dict[str, Any]:
        return {"reference": self.reference, "model_hint": self.model_hint,
                "resolved": self.resolved.as_dict() if self.resolved else None,
                "ambiguous": self.ambiguous, "truncated": self.truncated,
                "candidates": [item.as_dict() for item in self.candidates],
                "rejected": [item.as_dict() for item in self.rejected]}


def split_reference(text: str) -> tuple[str | None, str]:
    """``"PO-123"`` -> ``("purchase.order", "PO-123")``; unknown prefix -> (None, text)."""
    cleaned = text.strip()
    if ":" in cleaned:
        model, _, name = cleaned.partition(":")
        if model in SUPPORTED_MODELS:
            return model, name.strip()
    upper = cleaned.upper()
    for prefix, model in REFERENCE_PREFIXES:
        if not upper.startswith(prefix):
            continue
        rest = upper[len(prefix):]
        # "PO-123" and "PO123" are references; "SOP-1" is not a sale order.
        if rest and (rest[0].isdigit() or rest[0] in "-_/"):
            return model, cleaned
    return None, cleaned


class BusinessObjectResolver:
    """Resolve references and analyse impact against the live business records.

    Every read applies the tenant's company scope in the *query*, before any
    candidate reaches a model or a page.  A read failure is recorded as an
    unknown relation, never as "no rows": a paginated or failed query must not
    turn into a claim that nothing is affected.
    """

    #: Models that carry ``company_id`` and can therefore be scoped in the domain.
    COMPANY_SCOPED = frozenset({
        "purchase.order", "purchase.order.line", "sale.order", "sale.order.line",
        "mrp.production", "mrp.bom", "mrp.bom.line", "mrp.workorder",
        "mrp.workcenter", "mrp.workcenter.capacity", "stock.move", "stock.quant",
        "account.move",
        "account.move.line", "stock.picking",
    })

    def __init__(self, adapter, *, company_ids: Iterable[int] | None = None,
                 limit: int = 20, max_bom_depth: int = 5) -> None:
        self.adapter = adapter
        self.company_ids = tuple(company_ids) if company_ids else None
        self.limit = limit
        self.max_bom_depth = max(1, int(max_bom_depth))
        self._failures: list[str] = []

    # ---------------------------------------------------------------- resolve
    def resolve(self, text: str) -> Resolution:
        model, name = split_reference(text)
        reference = name
        resolution = Resolution(reference=reference, model_hint=model)
        models = [model] if model else list(SUPPORTED_MODELS)
        for candidate_model in models:
            resolution.candidates.extend(self._match(candidate_model, reference))
        # An integer is *also* a possible id, but only as a weaker candidate: it
        # never replaces a name match and it is labelled as what it is.
        if model and reference.isdigit():
            resolution.candidates.extend(self._by_id(model, int(reference)))
        resolution.truncated = len(resolution.candidates) > self.limit
        resolution.candidates = resolution.candidates[: self.limit]
        resolution.rejected = [item for item in resolution.candidates
                               if not item.in_company_scope]
        return resolution

    def _match(self, model: str, reference: str) -> list[Candidate]:
        fields = self._fields(model)
        exact = self._search(model, [("name", "=", reference)], fields)
        matched_by = "name"
        if not exact:
            exact = self._search(model, [("name", "=ilike", reference)], fields)
            matched_by = "name_case_insensitive"
        return [self._candidate(model, row, matched_by) for row in exact]

    def _by_id(self, model: str, identifier: int) -> list[Candidate]:
        rows = self._search(model, [("id", "=", identifier)], self._fields(model))
        return [self._candidate(model, row, "numeric_id") for row in rows]

    def _fields(self, model: str) -> list[str]:
        base = ["id", "name", "display_name", "state", "company_id"]
        if model == "purchase.order":
            base.append("partner_ref")
        return base

    def _scope_domain(self, model: str, domain: list) -> list:
        if not self.company_ids or model not in self.COMPANY_SCOPED:
            return domain
        return [*domain, ("company_id", "in", list(self.company_ids))]

    def _search(self, model: str, domain: list, fields: list[str],
                limit: int | None = None) -> list[dict]:
        try:
            return self.adapter.search_read(
                model, self._scope_domain(model, domain), fields,
                limit=limit or self.limit)
        except Exception:  # noqa: BLE001 - an absent model is recorded, not guessed
            self._failures.append(model)
            return []

    def _read(self, model: str, domain: list, fields: list[str],
              limit: int = 500) -> list[dict]:
        try:
            return self.adapter.search_read(
                model, self._scope_domain(model, domain), fields, limit=limit)
        except Exception:  # noqa: BLE001 - unknown is not empty
            self._failures.append(model)
            return []

    def _candidate(self, model: str, row: dict, matched_by: str) -> Candidate:
        company = row.get("company_id")
        company_id = company[0] if isinstance(company, (list, tuple)) and company else (
            company if isinstance(company, int) else None)
        in_scope = (self.company_ids is None or company_id is None
                    or company_id in self.company_ids)
        display = row.get("display_name") or row.get("name") or ""
        external = row.get("partner_ref") or None
        return Candidate(model=model, id=int(row["id"]), name=str(row.get("name") or ""),
                         display_name=str(display), company_id=company_id,
                         matched_by=matched_by, in_company_scope=in_scope,
                         external_ref=str(external) if external else None)

    # ----------------------------------------------------------------- impact
    def impact(self, ref: str) -> dict[str, Any]:
        """Supply/demand impact of one purchase or manufacturing record."""
        self._failures = []
        observed_at = _now()
        source = {"database": self._database(), "resolver": RESOLVER_VERSION,
                  "observed_at": observed_at}
        resolution = self.resolve(ref)
        target = resolution.resolved
        if target is None:
            return {"resolved": None, "ambiguous": resolution.ambiguous,
                    "candidates": [item.as_dict() for item in resolution.candidates],
                    "direct": [], "potential": [], "uncertain": [],
                    "manufacturing": [], "workcenters": [],
                    "truncated": resolution.truncated, "observed_at": observed_at,
                    "source": source,
                    "completeness": self._completeness(
                        truncated=resolution.truncated,
                        reason=("ambiguous_reference" if resolution.ambiguous
                                else "not_found"))}
        if target.model != "purchase.order":
            return {"resolved": target.as_dict(), "direct": [], "potential": [],
                    "uncertain": [], "manufacturing": [], "workcenters": [],
                    "truncated": False, "observed_at": observed_at, "source": source,
                    "note": "impact_analysis_not_implemented_for_model",
                    "completeness": self._completeness(
                        truncated=False, reason="unsupported_model")}
        return self._purchase_impact(target, source)

    def snapshot(self, ref: str) -> dict[str, Any]:
        """A traceable fact snapshot: source, business version, units, completeness."""
        self._failures = []
        observed_at = _now()
        source = {"database": self._database(), "resolver": RESOLVER_VERSION,
                  "observed_at": observed_at}
        resolution = self.resolve(ref)
        target = resolution.resolved
        if target is None:
            return {
                "object_ref": ref, "resolved": None,
                "ambiguous": resolution.ambiguous,
                "candidates": [item.as_dict() for item in resolution.candidates],
                "observed_at": observed_at, "source": source,
                "record": None, "lines": [],
                "relations": {"direct": [], "potential": [], "uncertain": [],
                              "manufacturing": [], "workcenters": []},
                "completeness": self._completeness(
                    truncated=resolution.truncated,
                    reason=("ambiguous_reference" if resolution.ambiguous
                            else "not_found")),
            }
        if target.model != "purchase.order":
            return {
                "object_ref": target.ref, "resolved": target.as_dict(),
                "observed_at": observed_at, "source": source,
                "record": self._provenance(target.model, self._read(
                    target.model, [("id", "=", target.id)],
                    ["id", "name", "state", "company_id", "write_date"], limit=1)),
                "lines": [],
                "relations": {"direct": [], "potential": [], "uncertain": [],
                              "manufacturing": [], "workcenters": []},
                "completeness": self._completeness(
                    truncated=False, reason="unsupported_model"),
            }
        order = self._read("purchase.order", [("id", "=", target.id)],
                           ORDER_FIELDS, limit=1)
        lines = self._read("purchase.order.line", [("order_id", "=", target.id)],
                           LINE_FIELDS, limit=500)
        impact = self._purchase_impact(target, source)
        return {
            "object_ref": target.ref,
            "resolved": target.as_dict(),
            "observed_at": observed_at,
            "source": source,
            "record": self._provenance("purchase.order", order[0]) if order else None,
            "lines": [self._provenance("purchase.order.line", line) for line in lines],
            "currency": _display(order[0].get("currency_id")) if order else "",
            "relations": {"direct": impact["direct"], "potential": impact["potential"],
                          "uncertain": impact["uncertain"],
                          "manufacturing": impact["manufacturing"],
                          "workcenters": impact["workcenters"]},
            "completeness": impact["completeness"],
        }

    def _purchase_impact(self, target: Candidate, source: dict) -> dict[str, Any]:
        lines = self._read("purchase.order.line", [("order_id", "=", target.id)],
                           ["id", "product_id", "product_qty", "uom_id",
                            "price_unit", "qty_received", "date_planned"], limit=200)
        line_ids = [int(row["id"]) for row in lines]
        products = {int(row["product_id"][0]) for row in lines
                    if isinstance(row.get("product_id"), (list, tuple))}
        moves = []
        if line_ids:
            moves = self._read(
                "stock.move", [("purchase_line_id", "in", line_ids)],
                ["id", "sale_line_id", "picking_id", "product_id", "state",
                 "quantity", "location_id", "location_dest_id"], limit=200)
        direct: dict[str, dict] = {}
        for move in moves:
            sale_line = move.get("sale_line_id")
            if not isinstance(sale_line, (list, tuple)) or not sale_line:
                continue
            sale_orders = self._read(
                "sale.order.line", [("id", "=", int(sale_line[0]))],
                ["id", "order_id", "product_uom_qty", "product_uom_id"], limit=1)
            if not sale_orders:
                continue
            order = sale_orders[0].get("order_id")
            if not isinstance(order, (list, tuple)) or not order:
                continue
            key = f"sale.order:{int(order[0])}"
            direct.setdefault(key, {
                "ref": key, "name": str(order[1]), "strength": DIRECT,
                "path": [f"purchase.order:{target.id}",
                         f"stock.move:{int(move['id'])}",
                         f"sale.order.line:{int(sale_line[0])}", key],
                "unit": _display(sale_orders[0].get("product_uom_id")),
                "location": _display(move.get("location_id")),
                "reserved": float(move.get("quantity") or 0),
                "move_state": str(move.get("state") or ""),
                "observed_at": _now()})
        potential, uncertain = self._sale_demand(products, set(direct))
        manufacturing, workcenters = self._manufacturing_demand(products)
        truncated = (len(lines) >= 200 or len(moves) >= 200
                     or len(manufacturing) >= 200)
        return {
            "resolved": target.as_dict(),
            "direct": list(direct.values()),
            "potential": potential,
            "uncertain": uncertain,
            "manufacturing": manufacturing,
            "workcenters": workcenters,
            "truncated": truncated,
            "observed_at": _now(),
            "source": source,
            "completeness": self._completeness(
                truncated=truncated,
                reason=(None if products else "no_lines")),
        }

    def _sale_demand(self, product_ids: set[int], direct: set[str]
                     ) -> tuple[list[dict], list[dict]]:
        """Sales demand for the same products, split by the evidence we have."""
        if not product_ids:
            return [], []
        lines = self._read(
            "sale.order.line", [("product_id", "in", sorted(product_ids))],
            ["id", "order_id", "product_id", "product_uom_qty", "product_uom_id",
             "price_unit"], limit=200)
        potential: list[dict] = []
        uncertain: list[dict] = []
        for line in lines:
            order = line.get("order_id")
            if not isinstance(order, (list, tuple)) or not order:
                continue
            key = f"sale.order:{int(order[0])}"
            if key in direct:
                continue
            state = self._stock_state(int(line["product_id"][0]))
            available = state["available"] if state else None
            demanded = float(line.get("product_uom_qty") or 0)
            entry = {
                "ref": key, "name": str(order[1]),
                "path": [f"purchase.order.line->product:{int(line['product_id'][0])}",
                         f"sale.order.line:{int(line['id'])}", key],
                "quantity": demanded,
                "unit": _display(line.get("product_uom_id")),
                "price": line.get("price_unit"),
                "available": available,
                "reserved": state["reserved"] if state else None,
                "locations": state["locations"] if state else [],
                "observed_at": _now(),
            }
            if available is not None and available >= demanded:
                # Stock covers it: no supply conflict is demonstrated.
                uncertain.append({**entry, "strength": UNCERTAIN,
                                  "reason": "stock_covers_demand"})
            else:
                potential.append({**entry, "strength": POTENTIAL,
                                  "reason": ("stock_unknown" if available is None
                                             else "shared_product_stock_short")})
        return potential, uncertain

    def _manufacturing_demand(self, product_ids: set[int]
                              ) -> tuple[list[dict], list[dict]]:
        """Multi-level manufacturing demand and the workcenters it needs."""
        if not product_ids:
            return [], []
        demand_products = set(product_ids)
        frontier = set(product_ids)
        via = {product: "purchased_product" for product in product_ids}
        for _ in range(self.max_bom_depth):
            if not frontier:
                break
            bom_lines = self._read(
                "mrp.bom.line", [("product_id", "in", sorted(frontier))],
                ["id", "bom_id", "product_id"], limit=200)
            bom_ids = [int(line["bom_id"][0]) for line in bom_lines
                       if isinstance(line.get("bom_id"), (list, tuple))
                       and line.get("bom_id")]
            if not bom_ids:
                break
            boms = self._read("mrp.bom", [("id", "in", bom_ids)],
                              ["id", "product_id", "product_tmpl_id"], limit=200)
            parents = {int(bom["product_id"][0]) for bom in boms
                       if isinstance(bom.get("product_id"), (list, tuple))
                       and bom.get("product_id")}
            new = parents - demand_products
            for product in new:
                via[product] = "bom_component_parent"
            demand_products |= parents
            frontier = new
        orders = self._read(
            "mrp.production", [("product_id", "in", sorted(demand_products)),
                               ("state", "not in", ["done", "cancel"])],
            ["id", "name", "product_id", "product_qty", "state", "date_start",
             "date_finished"], limit=200)
        demand: list[dict] = []
        for order in orders:
            product = order.get("product_id")
            product_id = int(product[0]) if isinstance(product, (list, tuple)) and product else 0
            demand.append({
                "ref": f"mrp.production:{int(order['id'])}",
                "name": str(order.get("name") or ""),
                "product_id": product_id,
                "product": _display(product),
                "quantity": order.get("product_qty"),
                "state": str(order.get("state") or ""),
                "date_start": str(order.get("date_start") or ""),
                "date_finished": str(order.get("date_finished") or ""),
                "via": via.get(product_id, "unknown"),
                "observed_at": _now(),
            })
        workcenters: list[dict] = []
        mo_ids = [int(order["id"]) for order in orders]
        if mo_ids:
            workorders = self._read(
                "mrp.workorder", [("production_id", "in", mo_ids)],
                ["id", "production_id", "workcenter_id", "duration_expected",
                 "state"], limit=200)
            center_ids = sorted({int(row["workcenter_id"][0])
                                 for row in workorders
                                 if isinstance(row.get("workcenter_id"), (list, tuple))
                                 and row.get("workcenter_id")})
            centers = self._read(
                "mrp.workcenter", [("id", "in", center_ids)],
                ["id", "name", "time_efficiency"], limit=200) if center_ids else []
            capacities = self._read(
                "mrp.workcenter.capacity", [("workcenter_id", "in", center_ids)],
                ["id", "workcenter_id", "product_id", "capacity"],
                limit=200) if center_ids else []
            by_id = {int(center["id"]): center for center in centers}
            capacity_by_center: dict[int, list[float]] = {}
            for row in capacities:
                center_id = _relation_id(row.get("workcenter_id")) or 0
                value = row.get("capacity")
                if center_id and isinstance(value, (int, float)):
                    capacity_by_center.setdefault(center_id, []).append(float(value))
            for workorder in workorders:
                center = workorder.get("workcenter_id")
                center_id = (int(center[0]) if isinstance(center, (list, tuple))
                             and center else 0)
                record = by_id.get(center_id, {})
                declared = capacity_by_center.get(center_id) or []
                workcenters.append({
                    "ref": f"mrp.workorder:{int(workorder['id'])}",
                    "workcenter": _display(center) or str(record.get("name") or ""),
                    "production_id": _relation_id(workorder.get("production_id")),
                    "duration_expected": workorder.get("duration_expected"),
                    "state": str(workorder.get("state") or ""),
                    "capacity": max(declared) if declared else None,
                    "capacities": declared,
                    "time_efficiency": record.get("time_efficiency"),
                    "observed_at": _now(),
                })
        return demand, workcenters

    def _stock_state(self, product_id: int) -> dict[str, Any] | None:
        try:
            rows = self.adapter.search_read(
                "stock.quant",
                self._scope_domain("stock.quant",
                                   [("product_id", "=", product_id),
                                    ("location_id.usage", "=", "internal")]),
                ["quantity", "reserved_quantity", "location_id"], limit=500)
        except Exception:  # noqa: BLE001 - unknown is not zero
            self._failures.append("stock.quant")
            return None
        if not rows:
            return None
        return {
            "available": sum(float(row.get("quantity") or 0)
                             - float(row.get("reserved_quantity") or 0)
                             for row in rows),
            "reserved": sum(float(row.get("reserved_quantity") or 0) for row in rows),
            "locations": sorted({_display(row.get("location_id")) for row in rows
                                 if row.get("location_id")}),
        }

    # -- provenance and completeness ---------------------------------------
    def _database(self) -> str:
        config = getattr(self.adapter, "config", None)
        return str(getattr(config, "database", "") or "")

    def _provenance(self, model: str, record: dict | None) -> dict[str, Any] | None:
        if not record:
            return None
        return {
            "model": model,
            "id": int(record.get("id") or 0),
            "business_version": str(record.get("write_date")
                                    or record.get("state") or ""),
            "observed_at": _now(),
            "company_id": _relation_id(record.get("company_id")),
            "state": record.get("state"),
            "record": record,
        }

    def _completeness(self, *, truncated: bool, reason: str | None) -> dict[str, Any]:
        failures = sorted(dict.fromkeys(self._failures))
        complete = reason is None and not truncated and not failures
        return {
            "complete": complete,
            "truncated": bool(truncated),
            "missing_relations": failures,
            "query_failures": failures,
            "reason": reason if reason is not None else ("" if complete else "partial"),
        }


RESOLVER_VERSION = "business-object-resolver-v2"

ORDER_FIELDS = ["id", "name", "state", "partner_id", "date_planned", "amount_total",
                "currency_id", "company_id", "write_date", "partner_ref"]

LINE_FIELDS = ["id", "product_id", "product_qty", "uom_id", "price_unit",
               "qty_received", "date_planned", "company_id", "write_date"]


def _display(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1])
    return "" if value in (None, False) else str(value)


def _relation_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
