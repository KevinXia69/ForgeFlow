"""Deterministic, citation-only Q&A for one product CaseFile.

The answer is derived only from the public ``ProductCase.view()`` projection:
no model, network, filesystem or owner identity is involved.  Citations name
fields that exist in that view and carry only scalar values, so ``uid`` and
filesystem paths can never enter a response.
"""

from __future__ import annotations

import math
import re
from typing import Any

from ..product.state import execution_result
from .budget_context import BUDGET_CONTEXT_RULE, budget_statement
from .impact_context import IMPACT_CONTEXT_RULE, chain_intent, impact_text

MODE = "case"

PLANNED_COST_CONTEXT = (
    "For ForgeFlow case facts, plan.new_spend is a legacy field for planned cost: "
    "the proposal's purchasing and manufacturing cost, including retained existing "
    "documents, with existing stock treated as sunk cost. It is not newly created "
    "purchase value or a cost increase. plan.margin uses that planning-cost basis; "
    "it is not measured post-execution accounting margin. Customer sales-order "
    "budget caps are not a new-purchase authorization limit. Keep cost_delta "
    "unknown without a measured baseline, and do not infer an increment from action kinds. "
    "plan.costs.new_purchase_value is the value of newly created purchase orders; "
    "plan.costs.purchase_increase is only the value increase on existing purchase orders, "
    "so a zero purchase_increase does not mean zero newly created purchasing. "
    "A new:purchase.order identifier is a planned identifier, not a confirmed ERP order number. "
) + BUDGET_CONTEXT_RULE + IMPACT_CONTEXT_RULE

_IMPACT_WORDS = (
    "impact",
    "affect",
    "risk",
    "exposure",
    "consequence",
    "delay",
    "late",
    "due",
    "order",
    "unit",
    "delivery",
    "why",
    "what happens",
    "how bad",
    "影响",
    "风险",
    "后果",
    "延迟",
    "延期",
    "受影响",
    "严重",
    "损失",
    "会怎样",
    "怎么办",
    "订单",
    "断供",
    "为什么",
)
_PLAN_WORDS = (
    "plan",
    "proposal",
    "policy",
    "polic",
    "budget",
    "spend",
    "cost",
    "margin",
    "price",
    "approve",
    "approval",
    "change",
    "changed",
    "check",
    "cancel",
    "create",
    "buy",
    "purchase",
    "计划",
    "方案",
    "政策",
    "策略",
    "预算",
    "支出",
    "成本",
    "毛利",
    "利润",
    "审批",
    "批准",
    "变更",
    "改什么",
    "改了什么",
    "改动",
    "修改",
    "采购",
    "取消",
    "新建",
    "花费",
    "金额",
)
_EXECUTION_WORDS = (
    "execut",
    "verif",
    "validat",
    "readback",
    "write",
    "writ",
    "wrote",
    "receipt",
    "proof",
    "evidence",
    "score",
    "odoo",
    "succeed",
    "success",
    "failed",
    "failure",
    "执行",
    "验收",
    "验证",
    "校验",
    "回读",
    "写入",
    "证据",
    "收据",
    "成功",
    "失败",
    "运行",
)

_STATE_ZH = {
    "preparing": "准备中",
    "awaiting_selection": "等待选择方案",
    "awaiting_approval": "等待审批",
    "executing": "执行中",
    "succeeded": "已完成",
    "failed": "失败",
    "rejected": "已拒绝",
    "expired": "已过期",
    "execution_unknown": "执行结果待核对",
    "revision_requested": "已要求修改",
}
_STATUS_ZH = {"pass": "通过", "warn": "警告", "block": "阻断", "unknown": "未知"}
_CHECK_ZH = {
    "margin": "规划毛利率",
    "budget": "历史预算检查",
    "budget_policy": "预算/政策",
    "demand": "需求覆盖",
    "policy": "策略绑定",
    "on_time": "按时交付",
}
_EXEC_ZH = {
    "succeeded": "成功",
    "success": "成功",
    "applied": "已应用",
    "ok": "成功",
    "completed": "完成",
    "failed": "失败",
}
_VERIFIER_ZH = {"passed": "通过", "failed": "失败", "unknown": "未知", "not_run": "未运行"}

_IMPACT_CITATIONS = (
    ("order.ref", "Order", "订单"),
    ("order.product", "Product", "产品"),
    ("order.units", "Units", "数量"),
    ("order.customer", "Customer", "客户"),
    ("order.due_days", "Due in (days)", "剩余天数"),
    ("order.due_days_max", "Latest due (days)", "最晚交期（天）"),
    ("disruption.vendor", "Vendor", "供应商"),
    ("disruption.workcenter", "Workcenter", "工作中心"),
)
_PLAN_CITATIONS = (
    ("plan.objective", "Planning preference", "规划偏好"),
    ("plan.new_spend", "Planned cost", "方案规划成本"),
    ("plan.margin", "Planned margin (%)", "规划毛利率（%）"),
    ("plan.actions", "Planned changes", "计划变更数"),
    ("plan.policy_sha256", "Policy binding", "策略绑定"),
    ("plan.costs.baseline.total", "Original arrangement planned cost", "原安排规划成本"),
    ("plan.costs.known_delta", "Known planning-cost difference", "已知规划成本差额"),
    ("plan.costs.new_purchase_value", "New purchase order value", "新建采购单金额"),
    ("plan.costs.purchase_increase", "Increases to existing purchases", "既有采购增加额"),
    ("plan.costs.new_manufacturing_standard", "New manufacturing standard cost", "新建制造单标准成本"),
    ("plan.costs.basis.as_of", "Cost snapshot time", "成本快照时间"),
)
_EXECUTION_CITATIONS = (
    ("execution.status", "Execution status", "执行状态"),
    ("execution.verifier.status", "Verifier", "验收"),
    ("execution.verifier.score", "Verifier score", "验收分数"),
    ("execution.writes", "Document change records", "单据变更记录数"),
    ("state", "Case state", "案件状态"),
    ("reason", "Reason", "原因"),
)
_STATE_CITATIONS = (
    ("state", "Case state", "案件状态"),
    ("reason", "Reason", "原因"),
)
_COUNT_FIELDS = frozenset({"plan.actions", "execution.writes", "comparison.plans",
                         "comparison.plans.0.actions", "comparison.plans.1.actions", "comparison.plans.2.actions"})
_COMPARISON_WORDS = ("compare", "candidate", "alternative", "option", "selection", "cheaper",
                     "比较", "候选", "取舍", "选择", "对比", "更便宜")
_OBJECTIVE_LABELS = {
    "preserve_quantities": ("Preserve existing quantities", "尽量保留既有数量"),
    "min_planned_cost": ("Prefer lower planning cost", "优先降低规划成本"),
    "preserve_capacity": ("Preserve manufacturing capacity", "优先保留产能"),
}


def _language(question: str) -> str:
    return "zh" if any("\u4e00" <= char <= "\u9fff" for char in question) else "en"


def _intent(question: str) -> str:
    text = question.casefold()
    # The most specific state question wins: execution > plan > impact.  The
    # fallback stays available for questions outside the three fixed intents.
    for intent, words in (
        ("execution", _EXECUTION_WORDS),
        ("readiness", ("order readiness", "ready date", "date buffer", "per-order", "逐单", "每单", "齐货", "交期余量", "每张订单")),
        ("comparison", _COMPARISON_WORDS),
        ("plan", _PLAN_WORDS),
        ("impact", _IMPACT_WORDS),
    ):
        if any(word in text for word in words):
            return intent
    return "unknown"


def _get(case: dict[str, Any], path: str) -> Any:
    current: Any = case
    array_limit = 8192 if path.startswith("plan.impact_chain.") else 128
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif (isinstance(current, list) and part.isascii() and part.isdecimal()
              and len(part) <= 4 and str(int(part)) == part and int(part) < min(len(current), array_limit)):
            current = current[int(part)]
        else:
            return None
    return current


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _number_text(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _money(value: Any) -> str | None:
    number = _number(value)
    return None if number is None else f"${number:,.2f}"


def _percent(value: Any) -> str | None:
    number = _number(value)
    return None if number is None else f"{_number_text(number)}%"


def _units(value: Any) -> str | None:
    number = _number(value)
    return None if number is None else f"{_number_text(number)} units"


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number_text(float(value))
    return "—"


def _state_label(state: Any, lang: str) -> str:
    raw = _text(state)
    if lang == "zh":
        return _STATE_ZH.get(raw, raw)
    return raw.replace("_", " ")


def _execution_label(status: Any, lang: str) -> str:
    raw = _text(status)
    if lang == "zh":
        return _EXEC_ZH.get(raw, raw)
    return raw


def _verifier_label(status: Any, lang: str) -> str:
    raw = _text(status)
    if lang == "zh":
        return _VERIFIER_ZH.get(raw, raw)
    return raw


def _check_label(check: dict[str, Any], lang: str) -> str:
    code = check.get("code")
    if lang == "zh" and isinstance(code, str) and code in _CHECK_ZH:
        return _CHECK_ZH[code]
    label = check.get("label")
    return label if isinstance(label, str) and label else _text(code)


def _checks(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    raw = plan.get("checks")
    if not isinstance(raw, list):
        return []
    return [check for check in raw if isinstance(check, dict)]


def _checks_summary(checks: list[dict[str, Any]], lang: str, plan: dict[str, Any]) -> str:
    parts: list[str] = []
    for check in checks:
        code = check.get("code")
        if code == "budget":
            parts.append(budget_statement(plan, lang, check))
            continue
        status = check.get("status")
        status_text = _STATUS_ZH.get(status, _text(status)) if lang == "zh" else _text(status)
        separator = "：" if lang == "zh" else ": "
        part = f"{_check_label(check, lang)}{separator}{status_text}"
        if code == "on_time" and check.get("actual") == "planned":
            part += "（计划）" if lang == "zh" else " (planned)"
        parts.append(part)
    return " · ".join(parts)


def _budget_specs(plan: dict[str, Any], base: str = "plan") -> tuple[tuple[str, str, str], ...]:
    checks = _checks(plan)
    for index, check in enumerate(checks):
        if check.get("code") == "budget":
            return tuple((f"{base}.checks.{index}.{field}", en, zh) for field, en, zh in (
                ("status", "Recorded budget-check status", "记录的预算检查状态"),
                ("actual", "Recorded budget-check measured value", "预算检查原记录值"),
                ("threshold", "Recorded budget-check comparison value", "预算检查原对照值"),
                ("basis.kind", "Budget calculation basis", "预算计算口径"),
                ("basis.plan_id", "Budget-check bound plan", "预算检查绑定方案"),
                ("basis.snapshot_sha256", "Budget-check bound snapshot", "预算检查绑定快照"),
            ))
    return ()


def _citation_value(case: dict[str, Any], field: str) -> Any:
    if field in _COUNT_FIELDS:
        value = _get(case, field)
        return len(value) if isinstance(value, list) else None
    return _get(case, field)


def _citations(
    case: dict[str, Any],
    specs: tuple[tuple[str, str, str], ...],
    lang: str,
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field, label_en, label_zh in specs:
        if field in seen:
            continue
        value = _citation_value(case, field)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if not isinstance(value, (str, int, float)):
            continue
        if isinstance(value, str) and not value:
            continue
        citations.append({
            "field": field,
            "label": label_zh if lang == "zh" else label_en,
            "value": value,
        })
        seen.add(field)
    return citations


def _missing_plan(case: dict[str, Any], lang: str) -> dict[str, Any]:
    if _get(case, "decision.preparation_pending") is True:
        answer = (
            "准备请求已记录，但工作台尚未收到确认的方案。请在案例页面继续这次准备："
            "执行器会取回同一次准备的原方案；若请求尚未送达，则完成该次准备。不会批准或执行。"
            if lang == "zh" else
            "The preparation request is recorded, but the workbench has not received a confirmed plan. "
            "Continue this preparation on the Cases page to retrieve the original plan. If the request "
            "was never received, the executor will finish that preparation. This does not approve or execute it."
        )
        return {"answer": answer, "mode": MODE,
                "citations": _citations(case, _STATE_CITATIONS, lang)}
    state = _text(case.get("state"))
    reason = case.get("reason")
    if lang == "zh":
        lead = {
            "preparing": "尚未生成计划：案件仍在准备中。",
            "failed": "尚未生成计划：计划准备失败。",
            "rejected": "尚未生成计划：案件已被拒绝。",
            "expired": "尚未生成计划：审批已过期。",
        }.get(state, "尚未生成计划。")
        answer = lead
        if isinstance(reason, str) and reason:
            answer += f"（原因：{reason}）"
        answer += f" 当前状态：{_state_label(state, lang)}。"
    else:
        lead = {
            "preparing": "No plan is available yet: the case is still being prepared.",
            "failed": "No plan is available: plan preparation failed.",
            "rejected": "No plan is available: the case was rejected.",
            "expired": "No plan is available: the approval expired.",
        }.get(state, "No plan is available.")
        answer = lead
        if isinstance(reason, str) and reason:
            answer += f" (reason: {reason})"
        answer += f" Current state: {_state_label(state, lang)}."
    return {
        "answer": answer,
        "mode": MODE,
        "citations": _citations(case, _STATE_CITATIONS, lang),
    }


def _impact_answer(case: dict[str, Any], lang: str, question: str = "") -> dict[str, Any]:
    if chain_intent(question) or isinstance(case.get("plan"), dict) and case["plan"].get("impact_chain") is not None:
        answer, specs = impact_text(case.get("plan"), question, lang)
        return {"answer": answer, "mode": MODE, "citations": _citations(case, specs + _STATE_CITATIONS, lang)}
    order = case.get("order") if isinstance(case.get("order"), dict) else {}
    disruption = (
        case.get("disruption") if isinstance(case.get("disruption"), dict) else {}
    )
    ref = _text(order.get("ref"))
    product = _text(order.get("product"))
    customer = _text(order.get("customer"))
    vendor = _text(disruption.get("vendor"))
    due_days = _text(order.get("due_days"))
    latest_due = order.get("due_days_max")
    if _number(latest_due) is not None and latest_due != order.get("due_days"):
        due_days += "–" + _text(latest_due)
    outage = disruption.get("kind") == "workcenter_outage"
    workcenter = _text(disruption.get("workcenter"))
    if lang == "zh":
        units = _number(order.get("units"))
        units_text = f"{_number_text(units)} 件" if units is not None else "—"
        disruption_text = f"停机工作中心为 {workcenter}" if outage else f"受影响供应商为 {vendor}"
        answer = (
            f"订单 {ref} 包含 {units_text} {product}，客户为 {customer}，"
            f"距交付还有 {due_days} 天。{disruption_text}；"
            "若不采取恢复计划，该客户订单可能延迟。"
        )
    else:
        units = _units(order.get("units")) or _text(order.get("units"))
        disruption_text = f"The unavailable workcenter is {workcenter}" if outage else f"The disrupted supplier is {vendor}"
        answer = (
            f"Order {ref} covers {units} of {product} for {customer}, "
            f"due in {due_days} days. {disruption_text}; "
            "without a recovery plan the customer order may ship late."
        )
    return {
        "answer": answer,
        "mode": MODE,
        "citations": _citations(case, _IMPACT_CITATIONS, lang),
    }


def _cost_answer(costs, lang):
    from decimal import Decimal

    from forgeflow.recovery.product_costs import project_cost_report

    costs = project_cost_report(costs)
    if costs is None:
        return ""
    def money(value):
        # Multiple dollar delimiters in one Markdown paragraph become inline
        # math in the workbench renderer. Currency codes keep prose readable.
        return ("未知" if lang == "zh" else "unknown") if value is None else f"USD {Decimal(value):,.2f}"

    original = money(costs["baseline"]["total"])
    delta = money(costs["known_delta"])
    new, increase = money(costs["new_purchase_value"]), money(costs["purchase_increase"])
    manufacturing = money(costs["new_manufacturing_standard"])
    if lang == "zh":
        return (f"原安排规划成本为 {original}，已知规划成本差额为 {delta}（方案减原安排）；"
                "原安排可能已不可行。采购按快照记录单价、制造按公开工作中心标准计算。"
                f"新建采购单金额为 {new}，既有采购增加额为 {increase}；"
                f"新建制造单标准成本为 {manufacturing}，不属于采购承诺。"
                "取消费用（如涉及）、税务调整和实际财务值未确认，完整费用差额未知，不能据此声称实际节省。")
    return (f"Original arrangement planned cost is {original}; the known planning-cost difference "
            f"is {delta} (proposal minus original). The original arrangement may be infeasible. "
            "Purchases use snapshot unit prices and manufacturing uses public workcenter standards. "
            f"New purchase order value is {new}; increases to existing purchases are {increase}. "
            f"New manufacturing standard cost is {manufacturing}, not a purchase commitment. "
            "Cancellation fees where applicable, tax adjustments and actual financial values are "
            "unconfirmed. The full cost difference is unknown; this does not establish actual savings.")


def _comparison_answer(case: dict[str, Any], lang: str) -> dict[str, Any]:
    """Summarize only the bounded public candidate set; never choose or rank it."""
    from decimal import Decimal

    from forgeflow.recovery.product_costs import project_cost_report

    comparison = case.get("comparison")
    plans = comparison.get("plans") if isinstance(comparison, dict) else None
    ready = (isinstance(comparison, dict) and comparison.get("status") == "ready"
             and isinstance(plans, list) and 1 <= len(plans) <= 3
             and all(isinstance(plan, dict) and plan.get("objective") in _OBJECTIVE_LABELS for plan in plans))
    if not ready:
        answer = (
            "当前没有可核对的候选比较记录，不能据此排名或确认可批准的方案。已有执行结果请以执行回执为准。"
            if lang == "zh" else
            "No candidate comparison can currently be checked. It cannot establish a ranking or an "
            "approvable choice. Consult the execution receipt for any recorded execution result."
        )
        return {"answer": answer, "mode": MODE, "citations": _citations(case, _STATE_CITATIONS + (
            ("comparison.status", "Comparison status", "比较记录状态"),), lang)}

    def label(plan):
        return _OBJECTIVE_LABELS[plan["objective"]][lang == "zh"]

    selected = next((plan for plan in plans if plan.get("plan_id") == comparison.get("selected_plan_id")), None)
    live_choice = (case.get("state") in {"awaiting_selection", "awaiting_approval"}
                   and not case.get("approval") and not case.get("execution"))
    if lang == "zh":
        lead = f"当前版本记录了 {len(plans)} 个同一事实快照下的不同候选。"
        lead += (f"记录中的当前方案是“{label(selected)}”。" if selected else "尚未选定方案。")
        if live_choice:
            lead += "在案例页面选择后，再单独批准才会执行。" if selected is None else "尚未执行，批准仍是独立操作。"
        else:
            lead += "这些是已记录的规划事实，不表示当前仍可选择或执行。"
    else:
        lead = f"This version records {len(plans)} distinct candidate(s) from the same fact snapshot. "
        lead += f'The recorded current plan is “{label(selected)}”. ' if selected else "No plan has been selected. "
        if live_choice:
            lead += ("Choose on the Cases page; execution requires a separate approval." if selected is None
                     else "Nothing has been executed; approval remains a separate action.")
        else:
            lead += "These are recorded planning facts, not evidence that a new choice or execution is available."
    lines = []
    specs = _STATE_CITATIONS + (("comparison.plans", "Distinct candidates", "不同候选数"),
        ("comparison.version", "Candidate version", "候选版本"),
        ("comparison.selected_plan_id", "Recorded current plan", "记录中的当前方案"))
    for index, plan in enumerate(plans):
        costs = project_cost_report(plan.get("costs"))
        amount = costs["planned"]["total"] if costs is not None else None
        money = ("未知" if lang == "zh" else "unknown") if amount is None else f"USD {Decimal(amount):,.2f}"
        actions = plan.get("actions")
        count = str(len(actions)) if isinstance(actions, list) else ("未知" if lang == "zh" else "unknown")
        lines.append(f"- {label(plan)}：规划成本 {money}，计划变更 {count} 项。" if lang == "zh"
                     else f"- {label(plan)}: planning cost {money}; proposed changes: {count}.")
        capacity = plan.get("capacity")
        if isinstance(capacity, dict):
            minutes = capacity.get("standard_minutes")
            time_text = ("未知" if lang == "zh" else "unknown") if minutes is None else f"{minutes:,.2f}"
            lines.append(f"  计划制造 {capacity["manufactured_units"]:g} 台、外购成品 {capacity["finished_purchase_units"]:g} 台，标准产线占用 {time_text} 分钟。" if lang == "zh" else
                f"  Planned finished units: {capacity["manufactured_units"]:g} manufactured, {capacity["finished_purchase_units"]:g} purchased; standard workcenter load: {time_text} minutes.")
        base = f"comparison.plans.{index}"
        lines.append(f"  {budget_statement(plan, lang)}")
        specs += tuple((field, f"{label(plan)}: {en}", f"{label(plan)}：{zh}")
                       for field, en, zh in _budget_specs(plan, base))
        headroom, headroom_specs = _headroom(plan, lang, base)
        lines.extend(headroom)
        specs += headroom_specs
        specs += tuple((f"{base}.{field}", f"{label(plan)}: {en}", f"{label(plan)}：{zh}") for field, en, zh in (
            ("objective", "preference", "规划偏好"), ("actions", "proposed changes", "计划变更数"),
            ("costs.planned.total", "planning cost", "规划成本"),
            ("costs.basis.as_of", "snapshot time", "快照时间"),
            ("policy_sha256", "policy binding", "策略绑定"),
            ("capacity.standard_minutes", "standard workcenter minutes", "标准产线占用分钟"),
            ("capacity.manufactured_units", "planned finished units manufactured", "计划制造成品数量"),
            ("capacity.finished_purchase_units", "planned finished units purchased", "计划外购成品数量")))
    for index, attempt in enumerate(comparison.get("attempts", [])[:3]):
        if not isinstance(attempt, dict) or attempt.get("objective") not in _OBJECTIVE_LABELS:
            continue
        reason = {
            "duplicate": ("produced the same business changes, so no extra candidate is shown", "业务变更相同，未重复展示"),
            "infeasible": ("did not produce a feasible candidate", "未生成可行候选"),
            "rejected": ("did not pass independent validation", "未通过独立校验"),
        }.get(attempt.get("status"))
        if reason:
            lines.append(f"{label(attempt)}：{reason[1]}。" if lang == "zh" else f"{label(attempt)}: {reason[0]}.")
        specs += ((f"comparison.attempts.{index}.status", f"{label(attempt)}: attempt outcome", f"{label(attempt)}：尝试结果"),)
    caveat = (
        "这些名称表示规划偏好，不证明全局最优或 ERP 改动最少。规划成本包含保留的单据；"
        "缺项和实际财务成本仍未知，不据此声称实际节省或已交付。标准产线占用不是实际工时或日历排程。"
        if lang == "zh" else
        "These labels describe planning preferences, not proof of a global optimum or the fewest ERP changes. "
        "Planning cost includes retained documents. Missing amounts and actual financial costs remain unknown; "
        "this does not establish actual savings or completed delivery. Standard workcenter load is not actual work time or a calendar schedule."
    )
    return {"answer": lead + "\n\n" + "\n".join(lines) + "\n\n" + caveat, "mode": MODE,
            "citations": _citations(case, specs, lang)}


def _headroom(plan, lang, base="plan"):
    """The same bound values shown in comparison, details and approval."""
    from decimal import Decimal

    from forgeflow.product.decision_metrics import project_decision_metrics

    try:
        data = project_decision_metrics(plan.get("decision_metrics"), plan_id=plan.get("plan_id"),
            snapshot_sha256=plan.get("snapshot_sha256"), costs=plan.get("costs"),
            capacity=plan.get("capacity"), readiness=plan.get("order_readiness"))
    except ValueError:
        data = None
    if data is None:
        return (["约束余量记录不足，风险高低未知。" if lang == "zh" else
                 "Constraint headroom is unrecorded; risk level is unknown."], ())
    unknown = "未知" if lang == "zh" else "unknown"
    def money(value):
        return unknown if value is None else f"USD {Decimal(value):,.2f}"
    margin, dates = data["margin"], data["readiness"]
    if lang == "zh":
        lines = [f"规划毛利率 {margin['planned_margin_pct'] or unknown}%，最低要求 {margin['effective_floor_pct']}%，余量 {margin['headroom_pp'] or unknown} 个百分点；毛利约束下的规划成本余量为 {money(margin['cost_headroom'])}。",
            f"{dates['order_count']} 单中，{dates['zero_buffer_orders']} 单无日期缓冲、{dates['late_orders']} 单供应晚于截止、{dates['uncovered_orders']} 单未覆盖需求、{dates['unknown_ready_orders']} 单齐货日期未知。"]
    else:
        lines = [f"Planned margin {margin['planned_margin_pct'] or unknown}%; required floor {margin['effective_floor_pct']}%; headroom {margin['headroom_pp'] or unknown} percentage points. Planning-cost headroom under that floor: {money(margin['cost_headroom'])}.",
            f"Of {dates['order_count']} orders, {dates['zero_buffer_orders']} have no date buffer, {dates['late_orders']} have late supply, {dates['uncovered_orders']} have uncovered demand and {dates['unknown_ready_orders']} have unknown readiness dates."]
    specs = tuple((f"{base}.decision_metrics.{field}", en, zh) for field, en, zh in (
        ("margin.planned_margin_pct", "Planned margin (%)", "规划毛利率（%）"),
        ("margin.effective_floor_pct", "Effective margin floor (%)", "有效毛利率门槛（%）"),
        ("margin.headroom_pp", "Margin headroom (percentage points)", "毛利率余量（百分点）"),
        ("margin.cost_headroom", "Planning-cost headroom", "规划成本余量"),
        ("readiness.order_count", "Recorded orders", "记录订单数"),
        ("readiness.zero_buffer_orders", "Orders without date buffer", "无日期缓冲订单数"),
        ("readiness.late_orders", "Orders with late supply", "供应晚于截止订单数"),
        ("readiness.uncovered_orders", "Orders with uncovered demand", "未覆盖需求订单数"),
        ("readiness.unknown_ready_orders", "Orders with unknown readiness", "齐货日期未知订单数")))
    for i, center in enumerate(data["workcenters"][:12]):
        used, limit, spare = (center[key] if center[key] is not None else unknown
                              for key in ("used_standard_minutes", "horizon_limit_minutes", "headroom_minutes"))
        lines.append(f"{center['code']}：规划期标准占用 {used} 分钟，上限 {limit}，余量 {spare}。" if lang == "zh" else
                     f"{center['code']}: horizon standard load {used} minutes; limit {limit}; headroom {spare}.")
        specs += tuple((f"{base}.decision_metrics.workcenters.{i}.{key}", key, key)
                       for key in ("code", "used_standard_minutes", "horizon_limit_minutes", "headroom_minutes"))
    if len(data["workcenters"]) > 12:
        lines.append("仅显示前12个工作中心；其余见详情。" if lang == "zh" else "Only the first 12 workcenters are shown; see details for the rest.")
    lines.append("金额不是实际可用资金；日期未计运输；产能余量不等于某一天空闲时间。这些是规划事实，不是风险概率或交付承诺。" if lang == "zh" else
                 "Amounts are not available funds; dates exclude transport; horizon capacity headroom is not free time on a particular day. These are planning facts, not risk probabilities or delivery promises.")
    return lines, specs


def _decision_details(case, lang, *, headroom=True, choice=False, actions=False, budgets=False):
    plan = case.get("plan")
    if not isinstance(plan, dict):
        return _missing_plan(case, lang)
    lines, specs = _headroom(plan, lang) if headroom else ([], ())
    if budgets:
        prefix = "当前方案的预算事实：" if lang == "zh" else "Budget facts for the recorded current plan: "
        lines.append(prefix + budget_statement(plan, lang))
        specs += _budget_specs(plan)
        from forgeflow.product.decision_metrics import project_decision_metrics
        try:
            metrics = project_decision_metrics(plan.get("decision_metrics"), plan_id=plan.get("plan_id"),
                snapshot_sha256=plan.get("snapshot_sha256"), costs=plan.get("costs"),
                capacity=plan.get("capacity"), readiness=plan.get("order_readiness"))
        except ValueError:
            metrics = None
        if metrics is not None:
            for i, row in enumerate(metrics["order_budgets"][:12]):
                lines.append(f"{row['order_ref']}：订单销售金额 USD {row['sales_value']}，客户预算上限 USD {row['customer_cap']}，余量 USD {row['headroom']}。" if lang == "zh" else
                             f"{row['order_ref']}: sales order value USD {row['sales_value']}; customer budget cap USD {row['customer_cap']}; headroom USD {row['headroom']}.")
                specs += tuple((f"plan.decision_metrics.order_budgets.{i}.{key}", key, key)
                               for key in ("order_ref", "sales_value", "customer_cap", "headroom"))
            if len(metrics["order_budgets"]) > 12:
                lines.append("这里只显示前12单；完整预算边界见案例详情。" if lang == "zh" else "Only the first 12 orders are shown; see case details for all budget boundaries.")
        else:
            lines.append("逐单客户预算余量未记录。" if lang == "zh" else "Per-order customer budget headroom is unrecorded.")
        lines.append("客户预算约束销售金额；余量不是采购授权，也不是逐单成本分摊。" if lang == "zh" else
                     "Customer budgets constrain sales value. Headroom is neither purchase authorization nor per-order cost allocation.")
    if choice:
        approval = case.get("approval") or {}
        reason = approval.get("comment") if approval.get("plan_id") == plan.get("plan_id") else None
        if isinstance(reason, str) and reason.strip():
            lines.append(("已记录的选择理由 / 批准备注：" if lang == "zh" else "Recorded selection reason / approval comment: ") + reason)
            specs += (("approval.comment", "Recorded approval comment", "已记录批准备注"),
                      ("approval.plan_id", "Approved plan", "批准方案"))
        else:
            lines.append("没有记录与此方案绑定的选择理由；规划目标不能代替操作者的理由。" if lang == "zh" else
                         "No selection reason bound to this plan was recorded. Its planning objective does not establish the operator's reason.")
    if actions:
        rows = plan.get("actions", [])
        lines.append("以下是方案拟议的单据变更，实际执行以回执为准；这些字段不能单独证明每项操作的原因。" if lang == "zh" else
                     "These are proposed document changes; the receipt establishes actual execution. These fields alone do not establish the reason for each action.")
        for i, row in enumerate(rows[:12]):
            lines.append(f"- {row.get('document', '')}: {row.get('summary', '')}")
            specs += tuple((f"plan.actions.{i}.{field}", field, field) for field in ("document", "summary", "kind", "quantity"))
        if len(rows) > 12:
            lines.append("这里只显示前12项；完整变更见案例详情。" if lang == "zh" else "Only the first 12 changes are shown; see case details for all changes.")
    return {"answer": "\n\n".join(lines), "mode": MODE, "citations": _citations(case, specs + _STATE_CITATIONS, lang)}


def _mixed_answer(question, case, lang):
    """Compose requested facts instead of silently discarding all but one intent."""
    text = question.casefold()
    def has(words):
        return any(word in text for word in words)
    compare = has(_COMPARISON_WORDS)
    readiness = has(("order readiness", "ready date", "date buffer", "per-order", "逐单", "每单", "齐货", "交期余量", "每张订单"))
    headroom = has(("headroom", "slack", "risk", "余量", "缓冲", "风险"))
    choice = has(("selection reason", "approval comment", "选择理由", "批准备注", "备注")) or (
        has(("why", "reason", "为什么", "为何", "理由")) and has(_COMPARISON_WORDS + ("choose", "chosen", "selected", "选")))
    actions = has(("changes", "changed", "cancel", "create", "变更", "取消", "新建", "改了"))
    budgets = has(("budget", "预算"))
    relationships = chain_intent(question)
    plan = has(_PLAN_WORDS)
    execution = has(_EXECUTION_WORDS)
    budget_only = budgets and not (compare or choice or actions or execution
        or has(tuple(word for word in _PLAN_WORDS if word not in {"budget", "预算"}))
        or has(("date", "ready", "delivery", "capacity", "risk", "齐货", "交期", "交付", "产能", "风险")))
    if budget_only:
        plan = readiness = headroom = False
    if not (relationships or headroom or choice or actions or budgets or sum((compare, readiness, plan, execution)) > 1):
        return None
    replies = []
    if execution:
        replies.append(_execution_answer(case, lang))
    if compare:
        replies.append(_comparison_answer(case, lang))
    elif plan and not relationships:
        replies.append(_plan_answer(case, lang))
    if relationships or headroom and not (compare or plan):
        replies.append(_impact_answer(case, lang, question))
    if readiness:
        replies.append(_readiness_answer(case, lang))
    if headroom and not compare or choice or actions or budgets:
        replies.append(_decision_details(case, lang, headroom=headroom and not compare, choice=choice, actions=actions, budgets=budgets))
    seen, citations = set(), []
    for reply in replies:
        for citation in reply["citations"]:
            if citation["field"] not in seen:
                citations.append(citation)
                seen.add(citation["field"])
    return {"answer": "\n\n".join(reply["answer"] for reply in replies), "mode": MODE, "citations": citations}


def _plan_answer(case: dict[str, Any], lang: str) -> dict[str, Any]:
    plan = case.get("plan")
    if case.get("comparison") is not None and (plan is None or _get(case, "comparison.status") != "ready"):
        return _comparison_answer(case, lang)
    if not isinstance(plan, dict):
        return _missing_plan(case, lang)

    actions = plan.get("actions")
    count = len(actions) if isinstance(actions, list) else 0
    new_spend = _money(plan.get("new_spend"))
    margin = _percent(plan.get("margin"))
    policy = plan.get("policy_sha256")
    policy_text = (
        f"{policy[:12]}…" if isinstance(policy, str) and len(policy) > 12 else policy
    )
    checks = _checks(plan)
    margin_check = next(
        (check for check in checks if check.get("code") == "margin"), None
    )
    threshold = None
    if isinstance(margin_check, dict):
        raw_threshold = margin_check.get("threshold")
        threshold = _percent(raw_threshold) or (
            raw_threshold if isinstance(raw_threshold, str) else None
        )
    summary = _checks_summary(checks, lang, plan)

    if lang == "zh":
        parts = [f"该计划包含 {count} 项变更。"]
        if new_spend is not None:
            parts.append(
                f"方案规划成本为 {new_spend}，包含规划采购与制造成本以及保留的既有单据；"
                "这不是相对原方案的成本增量。"
            )
        if margin is not None:
            if threshold is not None:
                parts.append(f"同一规划成本口径的规划毛利率为 {margin}，门槛为 {threshold}；不是执行后实际毛利率。")
            else:
                parts.append(f"同一规划成本口径的规划毛利率为 {margin}；不是执行后实际毛利率。")
        if isinstance(policy_text, str) and policy_text:
            parts.append(f"计划绑定策略 {policy_text}。")
        if any(check.get("code") in ("budget", "budget_policy") for check in checks):
            parts.append("客户预算边界不是新增采购授权额度。")
        if summary:
            parts.append(f"检查结果：{summary}。")
        answer = "".join(parts)
    else:
        parts = [f"The plan proposes {count} changes."]
        if new_spend is not None:
            parts.append(
                f"Planned cost is {new_spend}, covering planned purchasing and manufacturing "
                "including retained existing documents; it is not an increase over the original plan."
            )
        if margin is not None:
            if threshold is not None:
                parts.append(
                    f"Planned margin is {margin} against a {threshold} threshold on the same "
                    "planning-cost basis, not measured post-execution accounting margin."
                )
            else:
                parts.append(
                    f"Planned margin is {margin} on the same planning-cost basis, "
                    "not measured post-execution accounting margin."
                )
        if isinstance(policy_text, str) and policy_text:
            parts.append(f"The plan is bound to policy {policy_text}.")
        if any(check.get("code") in ("budget", "budget_policy") for check in checks):
            parts.append("Customer budget caps are not a new-purchase authorization limit.")
        if summary:
            parts.append(f"Checks: {summary}.")
        answer = " ".join(parts)
    cost_answer = _cost_answer(plan.get("costs"), lang)
    objective = plan.get("objective")
    if objective in _OBJECTIVE_LABELS:
        label = _OBJECTIVE_LABELS[objective][lang == "zh"]
        answer = (f"以下是记录中当前方案“{label}”的事实。" if lang == "zh"
                  else f'These are the facts for the recorded current plan, “{label}”. ') + answer
    return {
        "answer": answer + ("\n\n" + cost_answer if cost_answer else ""),
        "mode": MODE,
        "citations": _citations(case, _PLAN_CITATIONS + _budget_specs(plan), lang),
    }


def _missing_execution(case: dict[str, Any], lang: str) -> dict[str, Any]:
    state = _text(case.get("state"))
    reason = case.get("reason")
    if lang == "zh":
        lead = {
            "preparing": "案件仍在准备中，尚未执行，也没有验收结果。",
            "awaiting_selection": "候选已准备好，等待选择；尚未执行，也没有验收结果。",
            "awaiting_approval": "计划已准备好，正在等待你的决定；尚未执行，也没有验收结果。",
            "executing": "执行正在进行中；尚未产生验收结果。",
            "failed": "没有可用的执行记录：计划准备失败。",
            "rejected": "案件已被拒绝，没有执行。",
            "expired": "审批已过期，没有执行。",
        }.get(state, "尚无执行记录。")
        answer = lead
        if isinstance(reason, str) and reason:
            answer += f"（原因：{reason}）"
        answer += f" 当前状态：{_state_label(state, lang)}。"
    else:
        lead = {
            "preparing": "The case is still being prepared; nothing has been executed and no verifier result exists.",
            "awaiting_selection": "Candidates are prepared and awaiting a choice; nothing has been executed and no verifier result exists.",
            "awaiting_approval": "The plan is prepared and waiting for your decision; nothing has been executed and no verifier result exists.",
            "executing": "Execution is in progress; no verifier result exists yet.",
            "failed": "No execution record is available because plan preparation failed.",
            "rejected": "The case was rejected, so nothing was executed.",
            "expired": "The approval expired, so nothing was executed.",
        }.get(state, "No execution record is available.")
        answer = lead
        if isinstance(reason, str) and reason:
            answer += f" (reason: {reason})"
        answer += f" Current state: {_state_label(state, lang)}."
    return {
        "answer": answer,
        "mode": MODE,
        "citations": _citations(case, _STATE_CITATIONS, lang),
    }


def _execution_is_unknown(case: dict[str, Any]) -> bool:
    return (
        case.get("state") == "execution_unknown"
        or _get(case, "result.outcome") == "execution_unknown"
        or _get(case, "execution.status") in {"unknown", "execution_unknown"}
    )


def _unknown_execution(case: dict[str, Any], lang: str) -> dict[str, Any]:
    # A lost/failed postflight receipt can follow actual ERP writes. Neither an
    # empty write list nor missing readback establishes a zero-write outcome.
    if lang == "zh":
        answer = (
            "执行结果仍待核对，可能已经发生写入。缺失或不完整的回执无法确定写入范围，"
            "也不能确认完整回读与验收结果。请在案例页面核对原执行，勿重复批准。"
        )
    else:
        answer = (
            "The execution outcome remains unknown; writes may already have occurred. "
            "Missing or incomplete receipts do not establish the write scope or confirm "
            "complete readback and verification. Reconcile the original execution on "
            "the Cases page; do not approve it again."
        )
    reason = case.get("reason")
    if isinstance(reason, str) and reason:
        answer += f" 原因：{reason}。" if lang == "zh" else f" Reason: {reason}."
    return {
        "answer": answer,
        "mode": MODE,
        # Do not turn partial write receipts (including []) into a write count.
        "citations": _citations(
            case,
            (("execution.status", "Execution status", "执行状态"),) + _STATE_CITATIONS,
            lang,
        ),
    }


def _execution_answer(case: dict[str, Any], lang: str) -> dict[str, Any]:
    if _get(case, "execution.status") == "not_started":
        answer = (
            "此案例已关闭，尚未执行。执行器确认原计划没有派发记录，原计划及授权已失效。"
            "未执行 ERP 写入、回读或原始验收；这不代表业务恢复成功。"
            if lang == "zh" else
            "This case was closed without execution. The executor confirmed no dispatch records "
            "for the original plan and invalidated its plan and approval. No ERP write, readback "
            "or original verification was performed; this does not mean business recovery succeeded."
        )
        return {"answer": answer, "mode": MODE,
                "citations": _citations(case, (("execution.status", "Execution status", "执行状态"),
                                               ("state", "Case state", "案例状态")), lang)}
    if _execution_is_unknown(case):
        return _unknown_execution(case, lang)
    execution = case.get("execution")
    if not isinstance(execution, dict):
        return _missing_execution(case, lang)

    status = execution.get("status")
    verifier = (
        execution.get("verifier")
        if isinstance(execution.get("verifier"), dict)
        else {}
    )
    result = execution_result(execution, str(case.get("state", "")))
    synthetic = verifier.get("source") == "synthetic_executor"
    original = not synthetic and verifier.get("evaluation_scope") == "product_original_verifier"
    strict = result.get("strict_success")
    if strict is True:
        verifier_status = "passed"
    elif strict is False or verifier.get("status") == "failed":
        verifier_status = "failed"
    else:
        verifier_status = "not_run" if verifier.get("status") == "not_run" else "unknown"
    score = _number(verifier.get("score"))
    writes = execution.get("writes")
    write_count = len(writes) if isinstance(writes, list) else None
    readback = (
        execution.get("readback") if isinstance(execution.get("readback"), dict) else {}
    )
    records = readback.get("records")
    record_count = len(records) if isinstance(records, list) else None
    checks = verifier.get("checks")
    check_count = len(checks) if isinstance(checks, list) else 0
    passed = _number(verifier.get("passed_rule_count"))
    applicable = _number(verifier.get("applicable_rule_count"))
    total = None
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            applicable = applicable if applicable is not None else _number(check.get("applicable"))
            total = _number(check.get("total"))
            if applicable is not None or total is not None:
                break
    score_threshold_miss = (original and verifier.get("evaluation_valid") is True
        and verifier.get("strict_success") is False and verifier.get("original_passed") is False
        and score is not None and 0 <= score < 99
        and type(verifier.get("applicable_rule_count")) is int
        and verifier["applicable_rule_count"] > 0
        and type(verifier.get("passed_rule_count")) is int
        and verifier["passed_rule_count"] == verifier["applicable_rule_count"]
        and type(verifier.get("failed_rule_count")) is int
        and verifier.get("failed_rule_count") == 0)
    reason = case.get("reason")
    reason_text = reason if isinstance(reason, str) and reason else None

    if lang == "zh":
        status_text = _execution_label(status, lang)
        if reason_text is not None:
            parts = [f"执行结束，状态为{status_text}（原因：{reason_text}）。"]
        else:
            parts = [f"执行结束，状态为{status_text}。"]
        if synthetic:
            parts[0] = "模拟" + parts[0]
            parts.append(f"模拟检查结果为{_verifier_label(verifier_status, lang)}；未运行原始 ERP 验收。")
        elif not original:
            parts.append("验收证据来源尚未确认，原始 ERP 验收结果仍未知。")
        elif verifier_status is not None:
            score_text = f"，得分 {_number_text(score)}/100" if score is not None else ""
            if verifier_status == "unknown":
                parts.append("尚无有效的原始验收严格通过证据，不能判定已验证。")
            else:
                parts.append(f"原始验收结果为{_verifier_label(verifier_status, lang)}{score_text}。")
        else:
            parts.append("尚无可用的验收结果。")
        if score_threshold_miss:
            parts.append("记录的规则全部通过，但总分未达到原始验收要求的99/100，严格验收仍未通过。")
        prefix = "模拟" if synthetic else ""
        parts.append(
            f"记录了 {write_count} 条{prefix}单据变更；单据条目数不等于 ERP 事务数。"
            if write_count is not None else f"{prefix}写入明细未记录。"
        )
        parts.append(
            f"{prefix}回读 {record_count} 条记录。"
            if record_count is not None else f"{prefix}回读明细未记录，无法确定记录数量。"
        )
        if applicable is not None and total is not None:
            passed_text = f"通过 {_number_text(passed)} 项，" if passed is not None else ""
            parts.append(f"记录的检查：{passed_text}适用 {_number_text(applicable)} 项，共 {_number_text(total)} 项。")
        elif check_count:
            parts.append(f"记录了 {check_count} 项{'模拟' if synthetic else ''}检查。")
        if result.get("readback_status") != "passed":
            parts.append("ERP 回读尚未确认成功。")
        answer = "".join(parts)
    else:
        status_text = _text(status)
        if reason_text is not None:
            parts = [f"Execution finished with status {status_text} (reason: {reason_text})."]
        else:
            parts = [f"Execution finished with status {status_text}."]
        if synthetic:
            parts[0] = parts[0].replace("Execution", "Simulated execution", 1)
            parts.append(f"Synthetic checks report {_text(verifier_status)}; the original ERP verifier was not run.")
        elif not original:
            parts.append("The verifier evidence source is unconfirmed; original ERP verification remains unknown.")
        elif verifier_status is not None:
            score_text = f" ({_number_text(score)}/100)" if score is not None else ""
            if verifier_status == "unknown":
                parts.append("Strict original-verifier evidence is unavailable; verification remains unknown.")
            else:
                parts.append(f"The original verifier reports {_text(verifier_status)}{score_text}.")
        else:
            parts.append("No verifier result is available.")
        if score_threshold_miss:
            parts.append("All recorded rules passed, but the overall score is below the original passing threshold of 99/100. Strict verification did not pass.")
        prefix = "simulated " if synthetic else ""
        parts.append(
            f"{write_count} {prefix}document changes were recorded; document entries are not ERP transactions."
            if write_count is not None
            else f"{'Simulated write' if synthetic else 'Write'} details were not recorded."
        )
        parts.append(
            f"{record_count} {prefix}readback records were returned."
            if record_count is not None
            else f"{'Simulated readback' if synthetic else 'Readback'} record details were not recorded; the count is unknown."
        )
        if applicable is not None and total is not None:
            passed_text = f"{_number_text(passed)} passed, " if passed is not None else ""
            parts.append(f"Recorded checks: {passed_text}{_number_text(applicable)} applicable, {_number_text(total)} total.")
        elif check_count:
            parts.append(f"{check_count} {prefix}checks were recorded.")
        if result.get("readback_status") != "passed":
            parts.append("Successful ERP readback is not confirmed.")
        answer = " ".join(parts)
    return {
        "answer": answer,
        "mode": MODE,
        "citations": _citations(case, _EXECUTION_CITATIONS, lang),
    }


def _fallback_answer(case: dict[str, Any], lang: str) -> dict[str, Any]:
    state = _text(case.get("state"))
    if lang == "zh":
        answer = (
            "我可以基于该 CaseFile 确定性回答三类问题：影响、计划与政策、"
            f"执行与验收。当前状态：{_state_label(state, lang)}。"
        )
    else:
        answer = (
            "I can answer three deterministic questions from this CaseFile: "
            "impact, plan and policy, and execution and verification. "
            f"Current state: {_state_label(state, lang)}."
        )
    return {
        "answer": answer,
        "mode": MODE,
        "citations": _citations(case, _STATE_CITATIONS, lang),
    }


def _readiness_answer(case: dict[str, Any], lang: str) -> dict[str, Any]:
    from ..product.readiness import project_order_readiness

    plan = case.get("plan")
    if not isinstance(plan, dict):
        return _missing_plan(case, lang)
    try:
        data = project_order_readiness(plan.get("order_readiness"), plan_id=plan.get("plan_id"),
                                       snapshot_sha256=plan.get("snapshot_sha256"))
    except ValueError:
        data = None
    specs = _STATE_CITATIONS + (("plan.plan_id", "Recorded plan", "记录中的方案"),)
    if data is None:
        answer = ("这份方案没有可核对的逐单备货记录，交期余量未知；不能从整体按时标签推算，也不能确认已发货或送达。"
                  if lang == "zh" else "This plan has no verifiable per-order readiness record. Date buffers are unknown; "
                  "the overall on-time label cannot supply them or establish that orders shipped or arrived.")
        return {"answer": answer, "mode": MODE, "citations": _citations(case, specs, lang)}
    zh = lang == "zh"
    lead = ("以下是记录中当前方案的逐单备货安排，不是实际送达结果。" if zh else
            "These are the recorded current plan's per-order supply arrangements, not actual delivery results.")
    heading = ("| 订单 | 规划覆盖/需求 | 计划齐货日 | 客户截止日 | 规划余量 |\n| --- | --- | --- | --- | --- |" if zh else
               "| Order | Planned coverage/demand | Planned ready date | Customer deadline | Buffer |\n| --- | --- | --- | --- | --- |")
    rows = []
    for index, row in enumerate(data["orders"][:12]):
        name = row["order_ref"].replace("|", "\\|")
        ready = row["planned_ready_date"] or ("未知" if zh else "unknown")
        buffer = ("未知" if zh else "unknown") if row["buffer_days"] is None else f'{row["buffer_days"]} ' + ("天" if zh else "day" if abs(row["buffer_days"]) == 1 else "days")
        rows.append(f'| {name} | {row["covered_units"]}/{row["demand_units"]} | {ready} | {row["customer_due_date"]} | {buffer} |')
        specs += tuple((f"plan.order_readiness.orders.{index}.{field}", f"{row['order_ref']}: {en}", f"{row['order_ref']}：{cn}")
                       for field, en, cn in (("order_ref", "order", "订单"), ("covered_units", "planned coverage", "规划覆盖"),
                           ("demand_units", "demand", "需求"), ("planned_ready_date", "planned ready date", "计划齐货日"),
                           ("customer_due_date", "deadline", "客户截止日"), ("buffer_days", "planning buffer days", "规划余量天数")))
    note = ("齐货日取分配给该订单的供应中最晚的可用日；余量按自然日计算。未计运输时间，0天表示没有规划余量，负数表示供应晚于截止日；不是风险概率或已交付证明。"
            if zh else "Readiness uses the latest availability among supplies allocated to the order; buffers use calendar days. "
            "Transport is excluded. Zero means no planning buffer; a negative value means supply misses the deadline. "
            "This is not a risk probability or proof of completed delivery.")
    if len(data["orders"]) > 12:
        note += "这里只显示前12单，完整记录见案例详情。" if zh else " Only the first 12 orders are shown; see case details for the full record."
    return {"answer": lead + "\n\n" + heading + "\n" + "\n".join(rows) + "\n\n" + note,
            "mode": MODE, "citations": _citations(case, specs, lang)}


def _answer_case_impl(question: str, case: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    """Dispatch once; grounded replies can only fall back to a terminal route."""
    from .answer_evidence import _pending, grounded_answer, language, requested_topics, wants_document_detail

    lang = language(question, lang)
    scoped_cost = (isinstance(_get(case, "plan.costs"), dict) and requested_topics(question) == ["costs"]
                   and not any(word in question.casefold() for word in ("预算", "毛利", "利润", "budget", "margin", "profit", *_COMPARISON_WORDS))
                   and (not isinstance(case.get("comparison"), dict) or any(word in question.casefold() for word in (
                       "原安排", "基线", "差额", "新建采购", "增加", "节省", "三句", "baseline", "difference", "new purchase", "increase", "saving", "three sentences")))
                   and not chain_intent(question))
    if _pending(case) or wants_document_detail(question) or scoped_cost:
        return grounded_answer(question, case, lang)
    return _answer_case_fallback(question, case, lang)


def _answer_case_fallback(question: str, case: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    """Legacy/guarded answers; never redispatch into grounded answer routing."""
    from .answer_evidence import language
    lang = language(question, lang)
    if case.get("reason") == "product_selection_conflict" and not case.get("execution"):
        answer = (
            "这次选择与执行器当前版本冲突，未能确认。已有候选记录仍保留，但不能据此批准或继续原选择。"
            "请核对最新案例和已有执行回执；这条选择冲突记录不证明 ERP 是否已被其他请求修改，也不是计划准备失败。"
            if lang == "zh" else
            "This choice conflicts with the executor's current version and could not be confirmed. "
            "Candidate records remain, but cannot authorize approval or continuation of this choice. "
            "Check the latest case and any existing receipt; this conflict does not establish whether another "
            "request changed ERP, and does not mean plan preparation failed."
        )
        return {"answer": answer, "mode": MODE, "citations": _citations(case, _STATE_CITATIONS, lang)}
    if _get(case, "decision.selection_pending") is True:
        answer = (
            "选择请求已保存，但执行器的确认尚未收到。页面保留的是上一次确认的选择，不能据此批准或改选。"
            "请在案例页面继续确认这次选择；这不会批准或执行，也不重新求解。"
            if lang == "zh" else
            "The selection request is saved, but executor confirmation has not arrived. The page retains the last "
            "confirmed choice, which cannot be approved or replaced yet. Continue this selection on the Cases page; "
            "it does not approve, execute or solve again."
        )
        return {"answer": answer, "mode": MODE, "citations": _citations(case, _STATE_CITATIONS + (
            ("selection_request.plan_id", "Requested choice", "请求选择的方案"),
            ("selection_request.requested_at", "Selection requested at", "选择请求时间")), lang)}
    if _get(case, "decision.repreparation_pending") is True:
        answer = (
            "失败版本的记录仍保留，旧方案保持退役。正在确认按现有全部约束重新准备的后续版本，"
            "目前还没有可批准的新方案，也没有新的执行或验收结论。"
            if lang == "zh" else
            "The failed version remains recorded and earlier plans remain retired. A later version using "
            "all current constraints is awaiting confirmation. There is no new plan ready for approval "
            "and no new execution or verification conclusion."
        )
        return {"answer": answer, "mode": MODE,
                "citations": _citations(case, _STATE_CITATIONS + (
                    ("revision.version", "Failed version", "失败版本"),
                    ("repreparation_request.version", "Requested version", "请求版本")), lang)}
    if _get(case, "decision.revision_pending") is True:
        answer = (
            "这次修改的结果仍待确认。页面保留的是修改前的方案，尚不能确认旧版是否已退役、"
            "新版是否已准备完成；确认前不能批准、拒绝或另发一次修订。这里没有新的执行或验收结论。"
            if lang == "zh" else
            "The revision outcome is still awaiting confirmation. The displayed plan is the earlier version; "
            "its retirement and the new preparation have not yet been confirmed. Approval, rejection and "
            "another revision are locked during confirmation. There is no new execution or verification conclusion."
        )
        return {"answer": answer, "mode": MODE,
                "citations": _citations(case, _STATE_CITATIONS + (
                    ("revision.version", "Displayed version", "展示版本"),
                    ("revision_request.version", "Requested version", "请求版本")), lang)}
    mixed = _mixed_answer(question, case, lang)
    if mixed is not None:
        return mixed
    intent = _intent(question)
    if intent == "readiness":
        return _readiness_answer(case, lang)
    if intent == "comparison":
        return _comparison_answer(case, lang)
    if intent == "impact":
        return _impact_answer(case, lang, question)
    if intent == "plan":
        return _plan_answer(case, lang)
    if intent == "execution":
        return _execution_answer(case, lang)
    return _fallback_answer(case, lang)


_BROAD_EN = (
    "what happened", "which orders", "why", "what do i need", "what should i", "what will",
    "not known", "unknown", "executed", "verified", "readback", "what does it mean",
)
_BROAD_ZH = (
    "出了什么事", "发生什么", "哪些订单", "为什么", "需要决定", "我要做什么",
    "未知", "验证", "执行", "回读", "什么意思", "现在怎么样",
)


def _broad_question(question: str) -> bool:
    lowered = question.lower()
    return any(word in lowered for word in _BROAD_EN) or any(word in question for word in _BROAD_ZH)


def _plain_lead(case: dict[str, Any], lang: str) -> str:
    order = case.get("order") if isinstance(case.get("order"), dict) else {}
    disruption = case.get("disruption") if isinstance(case.get("disruption"), dict) else {}
    product = str(order.get("product") or "").strip()
    units = order.get("units")
    kind = disruption.get("kind")
    if lang == "zh":
        what = "供应商取消供货" if kind == "supplier_cancellation" else "产线停机" if kind == "workcenter_outage" else "订单异常"
        subject = f"这是一个{what}案例"
        if product and isinstance(units, (int, float)):
            subject += f"，涉及 {product} 的 {units:g} 台产品"
        state = str(case.get("state") or "")
        execution = case.get("execution") if isinstance(case.get("execution"), dict) else {}
        if state == "awaiting_approval":
            status = "方案已经准备好，等待你决定；还没有写入 ERP。"
        elif state == "executing":
            status = "系统正在执行这条方案。"
        elif state in {"execution_unknown", "failed"} or execution.get("status") == "unknown":
            status = "系统还不能确认是否已经写入 ERP，也还不能确认结果。"
        elif state == "succeeded":
            status = "系统已经执行并回读；原始验收结果请看记录。"
        elif state == "awaiting_selection":
            status = "有几条方案等你选择。"
        else:
            status = "当前状态以案例记录为准。"
        return f"{subject}。{status}"
    what = "a supplier cancellation" if kind == "supplier_cancellation" else "a workcenter outage" if kind == "workcenter_outage" else "an order exception"
    subject = f"This is {what} case"
    if product and isinstance(units, (int, float)):
        subject += f" affecting {units:g} units of {product}"
    state = str(case.get("state") or "")
    execution = case.get("execution") if isinstance(case.get("execution"), dict) else {}
    if state == "awaiting_approval":
        status = "A plan is ready and waiting for your decision; nothing has been written to ERP yet."
    elif state == "executing":
        status = "The system is applying this plan now."
    elif state in {"execution_unknown", "failed"} or execution.get("status") == "unknown":
        status = "The system cannot yet confirm whether anything was written to ERP, or what the result is."
    elif state == "succeeded":
        status = "The system applied it and read ERP back; the original verification result is in the record."
    elif state == "awaiting_selection":
        status = "Several plans are waiting for your choice."
    else:
        status = "The current state is whatever the case record shows."
    return f"{subject}. {status}"


def answer_case(question: str, case: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    """Answer one case question; broad questions get a plain-language lead."""
    from .answer_evidence import language
    resolved = language(question, lang)
    reply = _answer_case_impl(question, case, resolved)
    if _broad_question(question):
        lead = _plain_lead(case, resolved)
        reply["answer"] = lead + "\n\n" + reply["answer"]
    return reply


def strip_case_reference(text: str, case_id: str, lang: str) -> str:
    """Remove model-supplied case-ID reference lines from user-facing prose."""
    if not text:
        return text
    label = "本案例" if lang == "zh" else "this case"
    cleaned = re.sub(rf"(?im)^[ \t]*(?:Reference|参考|Case|案例)[ \t]*[:：][^\n]*{re.escape(case_id)}[^\n]*\n?", "", text)
    cleaned = cleaned.replace(case_id, label)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()
