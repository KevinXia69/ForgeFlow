"""Read-only explanations of the selected plan's bounded business relationships."""
from __future__ import annotations

import re

from forgeflow.product.impact import project_impact_chain

IMPACT_CONTEXT_RULE = (
    "Impact-chain baseline edges establish recorded association scope, not allocated quantities or certain delay. "
    "Planned edges describe product-specific allocations, not physical receipts or delivery. Component quantities "
    "belong to the entire manufacturing order, not its allocation to an individual sales order. "
    "Missing relationships are unknown, not absent. Actual execution and original verification remain separate. "
)


def chain_intent(question):
    text = question.casefold()
    return any(term in text for term in (
        "impact chain", "supply path", "supply chain", "upstream", "dependency", "dependencies",
        "which purchase", "which manufacturing", "which supplier", "supplies this", "supplies order",
        "影响链", "供应路径", "供应链", "来源链", "上游", "依赖关系", "哪张采购", "哪个采购",
        "哪张制造", "哪个制造", "哪个供应商", "由谁供应", "来自哪", "采购和制造",
    )) or (any(term in text for term in ("affect", "suppl", "relate", "depend", "影响", "供应", "关联", "依赖"))
           and any(term in text for term in ("purchase order", "manufacturing order", "采购单", "制造单")))


def checked_chain(plan):
    if not isinstance(plan, dict):
        return None
    try:
        return project_impact_chain(plan.get("impact_chain"), plan_id=plan.get("plan_id"),
            snapshot_sha256=plan.get("snapshot_sha256"), readiness=plan.get("order_readiness"))
    except (ValueError, TypeError, KeyError):
        return None


def impact_summary(plan):
    if not isinstance(plan, dict) or plan.get("impact_chain") is None:
        return None
    data = checked_chain(plan)
    if data is None:
        return {"status": "invalid; no relationship claims supported"}
    return {"status": "recorded", "order_count": len(data["orders"]),
            "orders_with_baseline_relationship": sum(bool(row["baseline_edge_keys"]) for row in data["orders"]),
            "detail": "Detailed paths omitted. Counts do not establish individual relationships, allocations or actual delivery."}


def _matches(question, label):
    return re.search(r"(?<![\w])" + re.escape(label) + r"(?![\w])", question, re.I) is not None


def impact_text(plan, question, lang):
    """Return plain explanation and scalar citation specs, with explicit bounds."""
    zh = lang == "zh"
    chain = checked_chain(plan)
    specs = [("plan.plan_id", "Selected plan", "当前方案")]
    if chain is None:
        return (("当前方案没有可核对的供应关系记录；无法确定哪张采购或制造单关联这张订单，也不能据此判断已到货或交付。"
                 if zh else "The current plan has no verifiable supply-relationship record. Which purchases or manufacturing "
                 "orders relate to this order is unknown; this cannot establish receipt or delivery."), tuple(specs))
    nodes = {node["key"]: (index, node) for index, node in enumerate(chain["nodes"])}
    edges = {edge["key"]: (index, edge) for index, edge in enumerate(chain["edges"])}
    orders = list(enumerate(chain["orders"]))
    named = [(index, row) for index, row in orders if _matches(question, row["order_ref"])]
    if not named:
        requested_orders = re.findall(r"(?<![A-Za-z0-9])(?:S[0-9]{4,}|SO(?:[-_][A-Za-z0-9]+|[0-9]+))(?![A-Za-z0-9])", question, re.I)
        if requested_orders:
            return (("当前选定方案没有找到所询问订单的供应关系；不能拿其他订单代替。" if zh else
                     "No supply relationship for the requested order was found in the selected plan; other orders cannot substitute for it."), tuple(specs))
        mentioned = {key for key, (_, node) in nodes.items() if _matches(question, node["label"])}
        if mentioned:
            named = [(index, row) for index, row in orders
                     if mentioned.intersection(row["baseline_node_keys"] + row["planned_node_keys"])]
            if not named:
                return (("该节点没有记录通往销售订单的分配关系；未分配供应不能计入订单覆盖。" if zh else
                         "This node has no recorded allocation path to a sales order; unallocated supply cannot count as order coverage."), tuple(specs))
    selected = named or orders
    lines = ["以下是当前选定方案的原异常关联与计划供应安排。" if zh else
             "These are the original disruption relationships and planned supplies for the currently selected plan."]
    if len(selected) > 4:
        lines.append("先列前4张订单；可按订单号询问其完整路径，全部关系见案例详情。" if zh else
                     "Showing the first 4 orders; ask by order reference for its path, or see case details for all relationships.")

    def cite(path, en="Relationship evidence", cn="关系依据"):
        specs.append(("plan.impact_chain." + path, en, cn))

    def label(key):
        index, node = nodes[key]
        cite(f"nodes.{index}.label", "Business document", "业务单据")
        cite(f"nodes.{index}.kind")
        if zh and node["kind"] == "stock":
            return "库存 " + node["product_code"]
        if zh and node["kind"] == "unallocated_supply":
            return "未分配供应 " + node["product_code"]
        if zh and node["kind"] in {"purchase_order", "manufacturing_order"} and node["facts"].get("record_id") is None:
            return ("计划新建采购单 " if node["kind"] == "purchase_order" else "计划新建制造单 ") + node["facts"]["entity_ref"].rsplit(":", 1)[1]
        return node["label"]

    for order_index, order in selected[:4]:
        cite(f"orders.{order_index}.order_ref", "Sales order", "销售订单")
        lines.append(order["order_ref"] + ("：" if zh else ":"))
        for phase, title in (("baseline", "原异常关联" if zh else "Original disruption scope"),
                             ("planned", "计划供应" if zh else "Planned supplies")):
            keys = order[phase + "_edge_keys"]
            if not keys:
                lines.append(title + ("：关系未记录，不能判定没有影响。" if zh else
                             ": relationship not recorded; this does not establish no impact."))
            for key in keys[:12]:
                index, edge = edges[key]
                quantity = ("关联范围，分配量未记录" if zh else "association scope; allocated quantity not recorded") if phase == "baseline" else (
                    f"{edge['quantity']} 件 {edge['product_code']}" if zh else f"{edge['quantity']} units of {edge['product_code']}")
                lines.append(f"- {title}: {label(edge['from'])} → {label(edge['to'])}; {quantity}.")
                for field in ("from", "to", "relation", "product_code", "quantity", "quantity_basis", "available_day", "required_day"):
                    cite(f"edges.{index}.{field}")
                for ref_index, _ in enumerate(edge["source_refs"]):
                    cite(f"edges.{index}.source_refs.{ref_index}.artifact", "Source artifact", "来源资料")
                    cite(f"edges.{index}.source_refs.{ref_index}.record", "Public source record", "公开来源记录")
            if len(keys) > 12:
                lines.append("此处仅列前12条关系，完整路径见案例详情。" if zh else
                             "Only the first 12 relationships are listed here; see case details for the full path.")
        ready = order["readiness"]
        unknown = "未知" if zh else "unknown"
        lines.append((f"计划齐货日 {ready['planned_ready_date'] or unknown}；客户截止 {ready['customer_due_date']}；"
                      f"自然日余量 {ready['buffer_days'] if ready['buffer_days'] is not None else unknown}。" if zh else
                      f"Planned supply-ready date {ready['planned_ready_date'] or unknown}; customer deadline {ready['customer_due_date']}; "
                      f"calendar-day buffer {ready['buffer_days'] if ready['buffer_days'] is not None else unknown}."))
        for field in ("planned_ready_date", "customer_due_date", "buffer_days"):
            cite(f"orders.{order_index}.readiness.{field}", "Planned readiness", "计划备货")
    lines.append("原关联不提供逐边数量；组件投入量属于整张制造单，不能当作此销售订单的分摊量。以上均为计划，不证明已收货、生产或交付；实际结果须查看独立执行回读与原始验收。" if zh else
                 "Original associations have no per-edge quantity. Component inputs belong to the whole manufacturing order, "
                 "not an allocation to this sales order. These are plans, not proof of receipt, production or delivery; "
                 "actual results require the separate execution readback and original verification.")
    return "\n\n".join(lines), tuple(specs)
