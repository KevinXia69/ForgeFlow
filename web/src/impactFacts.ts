import type { MessageKey, Translate } from "./i18n";
import type { ImpactEdge, ImpactFact, ImpactNode, ImpactOrder, ImpactSourceRef, ProductImpactChain, ProductPlan } from "./types";

/** The API validates source projections; the view also refuses a chain from another plan. */
export function boundImpactChain(plan: ProductPlan | null | undefined): ProductImpactChain | null {
  const chain = plan?.impact_chain;
  if (!plan || !chain || chain.schema_version !== 1 || chain.basis?.plan_id !== plan.plan_id
    || chain.basis.snapshot_sha256 !== plan.snapshot_sha256 || chain.basis.quantity_basis !== "product_units"
    || chain.basis.time_basis !== "planned_supply_availability_days" || !Array.isArray(chain.nodes)
    || !Array.isArray(chain.edges) || !Array.isArray(chain.orders) || !Array.isArray(chain.missing)) return null;
  const nodes = new Set(chain.nodes.map(row => row.key)), edges = new Set(chain.edges.map(row => row.key));
  if (nodes.size !== chain.nodes.length || edges.size !== chain.edges.length
    || chain.edges.some(row => !nodes.has(row.from) || !nodes.has(row.to))) return null;
  if (chain.orders.some(row => !nodes.has(row.node_key)
    || [...row.baseline_node_keys, ...row.planned_node_keys].some(key => !nodes.has(key))
    || [...row.baseline_edge_keys, ...row.planned_edge_keys].some(key => !edges.has(key)))) return null;
  return chain;
}

/** Follow only recorded baseline relationships, never use the whole case as disruption scope. */
export function disruptionScope(plan: ProductPlan | null | undefined) {
  const chain = boundImpactChain(plan);
  if (!chain) return null;
  const roots = new Set(chain.nodes.filter(node => node.kind === "disruption").map(node => node.key));
  const brokenKeys = new Set(chain.edges.filter(edge => edge.phase === "baseline"
    && edge.relation === "disruption_scope" && roots.has(edge.from)).map(edge => edge.to));
  if (!brokenKeys.size) return null;
  const reached = new Set(brokenKeys);
  for (let changed = true; changed;) {
    changed = false;
    for (const edge of chain.edges) if (edge.phase === "baseline" && reached.has(edge.from) && !reached.has(edge.to)) {
      reached.add(edge.to); changed = true;
    }
  }
  const orders = chain.orders.filter(order => reached.has(order.node_key));
  const demands = [...new Set(orders.map(order => chain.nodes.find(node => node.key === order.node_key)?.product_code))].map(product => {
    const rows = orders.filter(order => chain.nodes.find(node => node.key === order.node_key)?.product_code === product);
    const quantities = rows.map(order => order.readiness?.demand_units);
    return { product: product ?? null, orders: rows.length, units: quantities.every(value => typeof value === "number" && Number.isFinite(value))
      ? quantities.reduce((total, value) => total + value, 0) : null };
  });
  const supplies = chain.nodes.filter(node => brokenKeys.has(node.key));
  const products = [...new Set(supplies.map(node => node.product_code))];
  return { orders: orders.length || null, units: demands.length === 1 ? demands[0].units : null, demands,
    supplies: products.map(product => {
      const rows = supplies.filter(node => node.product_code === product);
      return { product, documents: rows.length, units: rows.every(row => typeof row.quantity === "number" && Number.isFinite(row.quantity))
        ? rows.reduce((total, row) => total + row.quantity!, 0) : null };
    }) };
}
export function disruptionScopeLines(plan: ProductPlan | null | undefined, t: Translate): string[] {
  const scope = disruptionScope(plan), unknown = t("v9.unknown");
  if (!scope) return [t("scope.unknown")];
  return [...(scope.demands.length ? scope.demands.map(row => t("scope.linked", { orders: row.orders, units: row.units ?? unknown, product: row.product ?? unknown })) : [t("scope.unknown")]),
    ...scope.supplies.map(row => t("scope.supply", { documents: row.documents, units: row.units ?? unknown, product: row.product ?? unknown })),
    t("scope.note")];
}

export function impactScope(chain: ProductImpactChain, order: ImpactOrder, phase: "baseline" | "planned") {
  const nodeKeys = new Set(phase === "baseline" ? order.baseline_node_keys : order.planned_node_keys);
  const edgeKeys = new Set(phase === "baseline" ? order.baseline_edge_keys : order.planned_edge_keys);
  return { nodes: chain.nodes.filter(node => nodeKeys.has(node.key)),
    edges: chain.edges.filter(edge => edgeKeys.has(edge.key) && edge.phase === phase) };
}

export function impactNodeLabel(node: ImpactNode, t: Translate): string {
  if (node.kind === "stock") return t("impact.stockName", { product: node.product_code ?? t("v9.unknown") });
  if (node.kind === "unallocated_supply") return t("impact.unallocatedName", { product: node.product_code ?? t("v9.unknown") });
  if (node.action === "create" && node.phase === "planned" && node.facts.document_ref == null) {
    const number = /:([0-9]+)$/.exec(node.key)?.[1];
    if (number) return t("impact.newDocument", { number, product: node.product_code ?? t("v9.unknown") });
  }
  return node.label;
}

export function impactQuantity(edge: ImpactEdge, t: Translate): string {
  if (edge.phase === "baseline" || edge.quantity == null) return t("impact.scopeQuantity");
  return t(edge.quantity_basis === "unallocated_supply" ? "impact.unusedQuantity" : "impact.exactQuantity",
    { quantity: edge.quantity, product: edge.product_code });
}

const missingKeys: Record<string, MessageKey> = {
  actual_receipt_unmeasured: "impact.missing.actual_receipt_unmeasured",
  shipping_unmeasured: "impact.missing.shipping_unmeasured",
  actual_delivery_unmeasured: "impact.missing.actual_delivery_unmeasured",
  component_order_allocation_unrecorded: "impact.missing.component_order_allocation_unrecorded",
  baseline_allocation_quantity_unrecorded: "impact.missing.baseline_allocation_quantity_unrecorded",
  baseline_relationship_unrecorded: "impact.missing.baseline_relationship_unrecorded",
};
export function impactMissing(code: string, t: Translate): string { return t(missingKeys[code] ?? "impact.otherMissing"); }

/** Only named, published fields are rendered. Source records are data, never links or file paths. */
export const impactFactKeys = ["kind", "vendor_ref", "offer_key", "workcenter_code", "broken_tokens", "entity_ref",
  "order_ref", "product_code", "quantity", "customer_due_day", "customer_due_date", "product_uom_id", "supply_role",
  "start_day", "available_day", "origin_tokens", "model", "record_id", "document_ref", "operation_id", "used_units",
  "remaining_units", "source_token", "action", "order_token", "plan_ref", "manufacturing_token", "required_day",
  "source_plan_ref", "per_unit"] as const;
export function impactFieldLabel(key: string, t: Translate): string {
  return impactFactKeys.includes(key as typeof impactFactKeys[number]) ? t(`impact.field.${key}` as MessageKey) : t("v9.unknown");
}
export function impactFactValue(value: ImpactFact | undefined, t: Translate): string {
  if (value == null) return t("impact.noReference");
  if (typeof value === "boolean") return t(value ? "impact.true" : "impact.false");
  if (Array.isArray(value)) return value.length ? value.map(item => impactFactValue(item, t)).join(" · ") : t("impact.noReference");
  if (typeof value === "object") return t("impact.noReference");
  const known: Record<string, MessageKey> = { supplier_cancellation: "impact.supplier_cancellation", workcenter_outage: "impact.workcenter_outage",
    keep: "impact.keep", create: "impact.create", update: "impact.update", cancel: "impact.cancel", finished: "impact.finished", component: "impact.component",
    "purchase.order": "impact.kind.purchase_order", "mrp.production": "impact.kind.manufacturing_order", "sale.order": "impact.kind.sales_order" };
  return typeof value === "string" && known[value] ? t(known[value]) : String(value);
}
export function impactSourceFields(chain: ProductImpactChain, ref: ImpactSourceRef): [string, ImpactFact | undefined][] {
  // This closed record grammar intentionally cannot navigate prototypes, paths or arbitrary URLs.
  if (ref.projection !== "source_facts" || !/^(?:disruption|(?:orders|operations|stock|components|sales_order_flows|component_flows|unused_purchase_units)\.[0-9]+(?:\.(?:before|after))?)$/.test(ref.record)) return [];
  let record: ImpactFact | undefined = chain.source_facts;
  for (const segment of ref.record.split(".")) {
    if (record == null || typeof record !== "object" || !Object.hasOwn(record, segment)) return [];
    record = (record as Record<string, ImpactFact>)[segment];
  }
  if (record == null || typeof record !== "object" || Array.isArray(record)) return [];
  return ref.fields.filter(key => impactFactKeys.includes(key as typeof impactFactKeys[number]))
    .map(key => [key, Object.hasOwn(record, key) ? record[key] : undefined]);
}

export function impactIntent(question: string): boolean {
  const q = question.toLowerCase();
  return /impact chain|supply (?:chain|path|allocation)|document (?:link|relationship)|origin link|why.{0,50}affected|affected.{0,50}why|影响链|供应(?:路径|关系|分配)|单据(?:关系|关联)|(?:为何|为什么).{0,30}受影响/.test(q)
    || /(?:which|what).{0,45}(?:suppl(?:y|ies)|feeds?).{0,45}(?:order|so\b)|(?:purchase|manufacturing) (?:order|document).{0,30}(?:feeds?|supplies|linked|affect)|(?:哪个|哪些).{0,20}(?:采购单|制造单)|(?:采购单|制造单).{0,20}(?:供应|关联|影响)/.test(q);
}

export function impactAnswer(plan: ProductPlan | null | undefined, question: string, t: Translate): string {
  const chain = boundImpactChain(plan);
  if (!chain) return t(plan?.impact_chain ? "impact.invalid" : "impact.unknown");
  const q = question.toLowerCase();
  const names = (label: string) => {
    const safe = label.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(`(?:^|[^a-z0-9])${safe}(?=$|[^a-z0-9])`).test(q);
  };
  const namedOrders = chain.orders.filter(row => names(row.order_ref));
  if (!namedOrders.length && /(?:^|[^a-z0-9])(?:s[0-9]{4,}|so(?:[-_][a-z0-9]+|[0-9]+))(?=$|[^a-z0-9])/i.test(question)) return t("impact.orderNotFound");
  const namedNodes = chain.nodes.filter(node => node.label.length > 2 && names(node.label));
  const selectedOrders = namedOrders.length ? namedOrders : namedNodes.length ? chain.orders.filter(row =>
    namedNodes.some(node => [...row.baseline_node_keys, ...row.planned_node_keys].includes(node.key))) : chain.orders;
  if (!selectedOrders.length) return t("impact.nodeNoOrder");
  const nodes = new Map(chain.nodes.map(node => [node.key, node]));
  const lines = [t("impact.qaTitle")];
  let shown = 0, omitted = false;
  for (const order of selectedOrders) {
    if (shown >= 12) { omitted = true; break; }
    const row = order.readiness;
    lines.push(t("impact.orderFacts", { order: row.order_ref, covered: row.covered_units, demand: row.demand_units,
      ready: row.planned_ready_date ?? t("v9.unknown"), due: row.customer_due_date }));
    for (const phase of ["baseline", "planned"] as const) {
      const scope = impactScope(chain, order, phase);
      lines.push(t(phase === "baseline" ? "impact.baseline" : "impact.planned"));
      if (!scope.edges.length) lines.push(t(phase === "baseline" ? "impact.noBaseline" : "impact.noPlanned"));
      for (const edge of scope.edges) {
        if (shown >= 12) { omitted = true; break; }
        lines.push(`- ${impactNodeLabel(nodes.get(edge.from)!, t)} → ${impactNodeLabel(nodes.get(edge.to)!, t)}: ${edge.product_code}; ${impactQuantity(edge, t)}.${edge.relation === "component_allocation" ? " " + t("impact.componentNote") : ""}`);
        shown++;
      }
    }
  }
  if (omitted) lines.push(t("impact.qaMore", { count: 12 }));
  lines.push(t("impact.baselineNote"), t("impact.plannedNote"), t("impact.actualNote"));
  return lines.join("\n\n");
}
