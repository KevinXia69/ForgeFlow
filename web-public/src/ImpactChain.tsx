import { useEffect, useId, useRef, useState } from "react";
import { SelectMenu } from "./SelectMenu";
import { useI18n, type MessageKey } from "./i18n";
import { boundImpactChain, impactFactKeys, impactFactValue, impactFieldLabel, impactMissing, impactNodeLabel,
  impactQuantity, impactScope, impactSourceFields } from "./impactFacts";
import type { ImpactEdge, ImpactNode, ImpactSourceRef, ProductImpactChain, ProductPlan } from "./types";
import styles from "./ImpactChain.module.css";

function Sources({ chain, refs }: { chain: ProductImpactChain; refs: ImpactSourceRef[] }) {
  const { t } = useI18n();
  return <details className={styles.sources}>
    <summary>{t("impact.source")}</summary>
    {refs.map((ref, index) => {
      const fields = impactSourceFields(chain, ref);
      return <div key={`${ref.artifact}:${ref.record}:${index}`}>
        <p>{t(`impact.source.${ref.artifact}` as MessageKey)}</p>
        <p className="decision-note">{t("impact.sourceRecord", { record: ref.record })}</p>
        {fields.length ? <dl className={styles.facts}>{fields.map(([key, value]) => <div key={key}>
          <dt>{impactFieldLabel(key, t)}</dt><dd>{impactFactValue(value, t)}</dd>
        </div>)}</dl> : <p>{t("impact.sourceUnavailable")}</p>}
      </div>;
    })}
  </details>;
}

/** Every instance owns its selection; a different plan or confirmed choice remounts it. */
export function ImpactChain({ plan, selectionKey = "" }: { plan: ProductPlan; selectionKey?: string }) {
  const chain = boundImpactChain(plan);
  const { t } = useI18n();
  if (!chain) return <section className="case-section" data-impact-chain={plan.plan_id}>
    <h2>{t("impact.title")}</h2><p className="decision-note">{t(plan.impact_chain ? "impact.invalid" : "impact.unknown")}</p>
  </section>;
  return <BoundImpactChain key={`${plan.plan_id}:${plan.snapshot_sha256}:${selectionKey}`} chain={chain} />;
}

function BoundImpactChain({ chain }: { chain: ProductImpactChain }) {
  const { t } = useI18n();
  const id = useId();
  const [orderRef, setOrderRef] = useState(chain.orders[0]?.order_ref ?? "");
  const [nodeKey, setNodeKey] = useState<string | null>(null);
  const panel = useRef<HTMLHeadingElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const order = chain.orders.find(row => row.order_ref === orderRef) ?? chain.orders[0];
  const selectedNode = chain.nodes.find(node => node.key === nodeKey);
  const nodeMap = new Map(chain.nodes.map(node => [node.key, node]));
  useEffect(() => { if (nodeKey) panel.current?.focus(); }, [nodeKey]);

  const closeNode = () => {
    setNodeKey(null);
    if (trigger.current?.isConnected) trigger.current.focus();
  };
  const nodeButton = (node: ImpactNode) => <button type="button" className={`secondary ${styles.node}`}
    data-impact-node={node.key} aria-expanded={nodeKey === node.key} aria-controls={`${id}-facts`}
    aria-label={t("impact.openNode", { name: impactNodeLabel(node, t) })}
    onClick={event => { trigger.current = event.currentTarget; setNodeKey(node.key); panel.current?.focus(); }}>
    <span>{impactNodeLabel(node, t)}</span>
  </button>;
  const edgeTable = (edges: ImpactEdge[], title: string) => <div className={`table-wrap ${styles.tableWrap}`}
    role="region" tabIndex={0} aria-label={title}>
    <table><thead><tr><th scope="col">{t("impact.from")}</th><th scope="col">{t("impact.to")}</th>
      <th scope="col">{t("impact.quantity")}</th><th scope="col">{t("impact.dates")}</th></tr></thead>
      <tbody>{edges.map(edge => <tr key={edge.key} data-impact-edge={edge.key}>
        <td>{nodeButton(nodeMap.get(edge.from)!)}</td><td>{nodeButton(nodeMap.get(edge.to)!)}</td>
        <td><p>{edge.product_code}</p><p>{impactQuantity(edge, t)}</p>
          <p className="decision-note">{t(`impact.relation.${edge.relation}` as MessageKey)}</p>
          {edge.relation === "component_allocation" && <p>{t("impact.componentNote")}</p>}
          <Sources chain={chain} refs={edge.source_refs} /></td>
        <td>{edge.available_day == null ? t("v9.unknown") : t("impact.day", { day: edge.available_day })}
          <br />{edge.required_day == null ? t("v9.unknown") : t("impact.day", { day: edge.required_day })}</td>
      </tr>)}</tbody></table>
  </div>;

  if (!order) return <section className="case-section" data-impact-chain={chain.basis.plan_id}>
    <h2>{t("impact.title")}</h2><p>{t("impact.noOrders")}</p>
  </section>;
  const baseline = impactScope(chain, order, "baseline"), planned = impactScope(chain, order, "planned");
  const visibleNodeKeys = new Set([...baseline.nodes, ...planned.nodes].map(node => node.key));
  const visibleEdgeKeys = new Set([...baseline.edges, ...planned.edges].map(edge => edge.key));
  const otherNodes = chain.nodes.filter(node => !visibleNodeKeys.has(node.key));
  const otherEdges = chain.edges.filter(edge => !visibleEdgeKeys.has(edge.key));
  const readiness = order.readiness;
  return <section className={`case-section ${styles.chain}`} data-impact-chain={chain.basis.plan_id}>
    <h2>{t("impact.title")}</h2><p>{t("impact.selectHelp")}</p>
    <p className="decision-note">{t("impact.basis", { date: chain.basis.anchor_date })}</p>
    <label className={styles.select} htmlFor={`${id}-order`}>{t("impact.order")}
      <SelectMenu
        id={`${id}-order`}
        value={orderRef}
        ariaLabel={t("impact.order")}
        options={chain.orders.map(row => ({ value: row.order_ref, label: row.order_ref }))}
        onChange={value => { setNodeKey(null); setOrderRef(value); trigger.current = null; }}
      />
    </label>
    <p data-impact-readiness={order.order_ref}>{t("impact.orderFacts", { order: readiness.order_ref,
      covered: readiness.covered_units, demand: readiness.demand_units, ready: readiness.planned_ready_date ?? t("v9.unknown"), due: readiness.customer_due_date })}</p>
    <div className={styles.summary} data-impact-summary="true">
      <h3>{t("impact.summaryTitle")}</h3>
      <p>{t("impact.summaryBody", { order: order.order_ref, baseline: baseline.edges.length,
        planned: planned.edges.length, missing: new Set([...chain.missing, ...order.missing]).size })}</p>
      <p className="decision-note">{t("impact.summaryHelp")}</p>
    </div>
    <p className="decision-note">{t("readiness.note")}</p>
    <details className={styles.details} data-impact-details="true">
      <summary>{t("impact.showDetails")}</summary>
      {([{ phase: "baseline", scope: baseline }, { phase: "planned", scope: planned }] as const).map(({ phase, scope }) => <div
        className={styles.phase} key={phase} data-impact-phase={phase}>
        <h3>{t(phase === "baseline" ? "impact.baseline" : "impact.planned")}</h3>
        <p>{t(phase === "baseline" ? "impact.baselineNote" : "impact.plannedNote")}</p>
        {scope.edges.length ? edgeTable(scope.edges, t(phase === "baseline" ? "impact.baseline" : "impact.planned"))
          : <p role="status">{t(phase === "baseline" ? "impact.noBaseline" : "impact.noPlanned")}</p>}
        <div className={styles.nodes} aria-label={t("impact.nodes")}>{scope.nodes.map(node => <span key={node.key}>{nodeButton(node)}</span>)}</div>
      </div>)}
    </details>
    {(otherNodes.length > 0 || otherEdges.length > 0) && <details className={styles.other}>
      <summary>{t("impact.other")}</summary><p>{t("impact.otherNote")}</p>
      {(["baseline", "planned"] as const).map(phase => {
        const edges = otherEdges.filter(edge => edge.phase === phase);
        return edges.length ? <div key={phase}><h3>{t(phase === "baseline" ? "impact.baseline" : "impact.planned")}</h3>
          {edgeTable(edges, t("impact.other") + ": " + t(phase === "baseline" ? "impact.baseline" : "impact.planned"))}</div> : null;
      })}
      <div className={styles.nodes}>{otherNodes.map(node => <span key={node.key}>{nodeButton(node)}</span>)}</div>
    </details>}
    {selectedNode && <section id={`${id}-facts`} className={styles.panel} aria-labelledby={`${id}-facts-title`}
      data-impact-node-facts={selectedNode.key} onKeyDown={event => {
        if (event.key === "Escape") { event.stopPropagation(); closeNode(); }
      }}>
      <h3 ref={panel} tabIndex={-1} id={`${id}-facts-title`}>{t("impact.facts", { name: impactNodeLabel(selectedNode, t) })}</h3>
      <button type="button" className="secondary" data-close-impact-node="true" onClick={closeNode}>{t("impact.close")}</button>
      <dl className={styles.facts}>
        <div><dt>{t("impact.nodeKind")}</dt><dd>{t(`impact.kind.${selectedNode.kind}` as MessageKey)}</dd></div>
        <div><dt>{t("impact.phase")}</dt><dd>{t(`impact.${selectedNode.phase}Phase` as MessageKey)}</dd></div>
        <div><dt>{t("impact.product")}</dt><dd>{selectedNode.product_code ?? t("v9.unknown")}</dd></div>
        <div><dt>{t("impact.nodeTotal")}</dt><dd>{selectedNode.quantity ?? t("v9.unknown")}</dd></div>
        {selectedNode.action && <div><dt>{t("impact.action")}</dt><dd>{t(`impact.${selectedNode.action}` as MessageKey)}</dd></div>}
        {impactFactKeys.filter(key => key in selectedNode.facts && !["quantity", "product_code"].includes(key)).map(key => <div key={key}>
          <dt>{impactFieldLabel(key, t)}</dt><dd>{impactFactValue(selectedNode.facts[key], t)}</dd></div>)}
      </dl>
      <p>{t(selectedNode.phase === "baseline" ? "impact.baselineNote" : "impact.plannedNote")}</p>
      <Sources chain={chain} refs={selectedNode.source_refs} />
    </section>}
    <details className={styles.missing}>
      <summary>{t("impact.missing")}</summary>
      <ul>{[...new Set([...chain.missing, ...order.missing])].map(code => <li key={code}>{impactMissing(code, t)}</li>)}</ul>
    </details>
    <p>{t("impact.actualNote")}</p>
  </section>;
}
