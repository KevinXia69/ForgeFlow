"""Interpret recorded budget checks without upgrading their historical basis."""
from __future__ import annotations

from typing import Any

from ..product.budget_checks import project_budget_check

LEGACY_AGGREGATE_SOURCE = (
    "solver planned cost compared with summed sales-order customer budget caps; "
    "per-order caps remain policy checks, not purchase authorization"
)
BUDGET_CONTEXT_RULE = (
    "Budget check counts are recorded statuses, not interchangeable compliance proofs. "
    "Only a valid sales_order_value_per_customer_cap_v1 basis bound to its parent "
    "plan and snapshot checks each sales-order value against its own customer cap. "
    "An unmarked budget check is historical: never infer per-order compliance from "
    "its pass, label, total amounts, decision_metrics or verifier result."
)


def budget_fact(plan: Any, check: Any = None) -> dict | None:
    if not isinstance(plan, dict):
        return None
    if check is None:
        checks = plan.get("checks")
        check = next((row for row in checks if isinstance(row, dict) and row.get("code") == "budget"), None) if isinstance(checks, list) else None
    if not isinstance(check, dict) or check.get("code") != "budget":
        return None
    status = check.get("status")
    result = {"recorded_status": status if isinstance(status, str) and status in {"pass", "warn", "block", "unknown", "planned"} else "unknown"}
    if "basis" not in check:
        result["basis"] = "legacy_aggregate" if check.get("source") == LEGACY_AGGREGATE_SOURCE else "legacy_unrecorded"
        return result
    try:
        validated = project_budget_check(check, plan_id=plan.get("plan_id"),
                                         snapshot_sha256=plan.get("snapshot_sha256"))
    except (ValueError, TypeError, KeyError):
        result["basis"] = "invalid"
        return result
    result.update(basis="per_order", compliant_orders=validated["actual"],
                  required_orders=validated["threshold"])
    return result


def budget_statement(plan: Any, lang: str, check: Any = None) -> str:
    fact = budget_fact(plan, check)
    if fact is None:
        return "逐单预算检查结论未记录。" if lang == "zh" else "No per-order budget-check conclusion was recorded."
    basis = fact["basis"]
    if basis == "invalid":
        return "预算检查的口径或计划绑定无效，不能据此判断逐单预算。" if lang == "zh" else "The budget check has an invalid basis or plan binding; it cannot establish per-order compliance."
    status = fact["recorded_status"]
    status_zh = {"pass": "通过", "warn": "警告", "block": "阻断", "unknown": "未知", "planned": "计划中"}.get(status, "未知")
    if basis == "legacy_aggregate":
        return (f"历史预算总额对照记录为{status_zh}，其口径是规划总成本对客户预算合计，不代表逐单预算检查。" if lang == "zh" else
                f"The historical aggregate comparison was recorded as {status}: planned total cost versus summed customer caps, not a per-order budget check.")
    if basis == "legacy_unrecorded":
        return (f"历史预算检查记录为{status_zh}，计算口径未记录，不能据此确认逐单预算。" if lang == "zh" else
                f"The historical budget check was recorded as {status}; its calculation basis is unrecorded and does not establish per-order compliance.")
    actual, total = fact["compliant_orders"], fact["required_orders"]
    counts = f"{actual}/{total}" if actual is not None and total is not None else None
    if lang == "zh":
        return f"逐单客户销售金额预算检查：{status_zh}；" + (f"{counts} 单在各自客户上限内。" if counts else "订单金额或预算资料不完整。")
    return f"Per-order customer sales-value budget check: {status}; " + (f"{counts} orders within their own customer caps." if counts else "order-value or budget evidence is incomplete.")
