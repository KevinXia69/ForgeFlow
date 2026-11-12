"""Render an inspectable local report from trusted exported investigation state."""
from __future__ import annotations

import argparse
import copy
import html
import json
import re
from collections import Counter
from pathlib import Path

from forgeflow.investigation.event import trusted_event
from forgeflow.recovery.storage import digest, read_json


def cell(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(value).replace("|", "&#124;").replace("\n", " ").replace("`", "&#96;")


PLAN_COST_LABEL = "计划采购与制造成本（含保留单据）"


def _verify_review(state: dict, review: dict | None) -> None:
    if trusted_event(state["event"])["sha256"] != state["event_sha256"]:
        raise ValueError("review_binding_mismatch")
    if state.get("status") == "prepared":
        if (not review or not re.fullmatch(r"[0-9a-f]{64}", review.get("plan_id", ""))
                or digest(review) != state["prepared"]["review_sha256"]
                or review["plan_id"] != state["prepared"]["plan_id"]
                or review["run_id"] != state["run_id"]
                or review["event_sha256"] != state["event_sha256"]
                or digest(review["request"]) != review["request_sha256"]
                or review["request_sha256"] != state["prepared"]["request_sha256"]
                or review["policy_sha256"] != state["policy_sha256"]
                or review["contract_sha256"] != state["contract_sha256"]
                or review["event"] != state["event"]
                or digest(review["verification_facts"]) != review["facts_sha256"]):
            raise ValueError("review_binding_mismatch")
        final = state["drafts"][-1] if state["drafts"] else None
        if (not final or not final["validation"]["valid"]
                or final["draft_id"] != review["draft_id"]
                or final["revision"] != review["draft_revision"]
                or digest(final["draft"]["request"]) != review["request_sha256"]
                or final["draft"]["evidence"] != review["field_evidence"]
                or any(state["observations"].get(identity) != observation
                       for identity, observation in review["evidence"].items())):
            raise ValueError("review_binding_mismatch")
    elif review is not None:
        raise ValueError("review_without_prepared_state")


def trusted_summary(state: dict, review: dict | None = None, *, store: Path | None = None) -> dict:
    """Host-only result from service-owned state and content-bound plan artifacts.

    This accepts no model final answer. A prepared card requires all seven bound
    artifacts; an unverified/missing package must not produce a success card.
    """
    _verify_review(state, review)
    result = {"schema_version": 1, "run_id": state["run_id"], "status": state["status"],
              "authority": "bound_service_artifacts", "event_sha256": state["event_sha256"],
              "policy_sha256": state["policy_sha256"], "state_sha256": digest(state),
              "phase": "awaiting_business_decision" if state["status"] == "needs_clarification"
                       else "investigating" if state["status"] == "investigating" else "stopped",
              "terminal": copy.deepcopy(state.get("terminal")),
              "business_usage": copy.deepcopy(state["usage"]), "execution_validated": False,
              "odoo_writes": False if review is not None else None,
              "zero_writes_verified": None, "approval_required": state["status"] == "prepared",
              "model_explanation_authoritative": False}
    if review is None:
        return result
    from forgeflow.recovery.service import ARTIFACTS
    if store is None:
        raise ValueError("prepared_artifacts_required")
    directory = Path(store) / review["plan_id"]
    manifest = read_json(directory / "manifest.json")
    artifacts = {name: read_json(directory / (name + ".json")) for name in ARTIFACTS}
    summary = read_json(directory / "prepared.json")
    if (digest(manifest) != review["plan_id"]
            or manifest["artifacts"] != {name: digest(value) for name, value in artifacts.items()}
            or artifacts["request"] != review["request"] or summary != review["summary"]):
        raise ValueError("prepared_artifact_binding_mismatch")
    plan = artifacts["plan"]["plan"]
    operations = artifacts["change-set"]["operations"]
    counts = Counter(row["action"] for row in operations)
    if (summary["expected_new_spend"] != plan["expected_new_spend"]
            or summary["operation_counts"] != {name: counts[name]
                                               for name in ("keep", "update", "cancel", "create")}):
        raise ValueError("prepared_summary_mismatch")
    # Keep the historical machine field and arithmetic intact. Its public label
    # describes the actual scope; no incremental-cash estimate is inferred.
    result.update({"phase": "plan_awaiting_approval", "plan_id": review["plan_id"],
                   "review_sha256": digest(review), "request_sha256": review["request_sha256"],
                   "request": copy.deepcopy(review["request"]),
                   "orders": [{"id": identity,
                               "budget_cap": review["request"]["sales_order_budget_caps"][str(identity)]}
                              for identity in review["request"]["sales_order_ids"]],
                   "plan_cost": {"label": PLAN_COST_LABEL, "value": plan["expected_new_spend"],
                                 "legacy_field": "expected_new_spend", "incremental_cash": None},
                   "purchase_orders": copy.deepcopy(plan["purchase_orders"]),
                   "manufacturing_orders": copy.deepcopy(plan.get("manufacturing_orders", [])),
                   "operation_counts": copy.deepcopy(summary["operation_counts"]),
                   "step_count": summary["step_count"],
                   "operations": copy.deepcopy(operations),
                   "artifact_sha256": copy.deepcopy(manifest["artifacts"])})
    return result


def render_report(state: dict, review: dict | None = None, *, store: Path | None = None) -> str:
    _verify_review(state, review)
    card = trusted_summary(state, review, store=store) if store is not None else None
    lines = ["# ForgeFlow 调查与恢复方案", "",
             f"运行：`{cell(state['run_id'])}`　状态：**{cell(state['status'])}**", "",
             "本报告记录调查和准备结果。ERP 尚未执行；原始业务验收尚未进行。", "",
             "## 原始事件", "", "> " + cell(state["event"]["text"]), "",
             "## 结论", ""]
    if review:
        summary = review["summary"]
        lines += [f"已绑定方案：`{review['plan_id']}`", "",
                  f"{PLAN_COST_LABEL}：{cell(summary.get('expected_new_spend', '未提供'))}；"
                  f"步骤数：{cell(summary.get('step_count', '未提供'))}。", "",
                  "该金额不是新增现金支出估计；保留原 expected_new_spend 字段和计算口径。", "",
                  "操作数量：" + cell(summary.get("operation_counts", {})), ""]
        if card is not None:
            lines += ["订单与授权预算：" + cell(card["orders"]), "",
                      "方案采购（包括实际供应商、数量和价格）：" + cell(card["purchase_orders"]), "",
                      "方案制造：" + cell(card["manufacturing_orders"]), "",
                      "当前阶段：方案待审批；执行验收尚未进行。", ""]
        lines += [
                  "## 字段依据", "",
                  "| 请求字段 | 来源记录 | 来源字段 | 观测值 | 观测标识 |",
                  "| --- | --- | --- | --- | --- |"]
        for ref in review["field_evidence"]:
            observed = review["evidence"][ref["observation_id"]]["record"]
            value = (observed["id"] if ref["source_field"] == "id"
                     else observed["fields"][ref["source_field"]])
            lines.append("| " + " | ".join(map(cell, (
                ref["field"], observed["entity"] + ":" + observed["id"],
                ref["source_field"], value, ref["observation_id"]))) + " |")
        lines += ["", "字段值按正式字段映射绑定；订单身份允许从记录 ID 转为请求整数 ID。", "",
                  "## 实际准备请求", "", "```json",
                  json.dumps(review["request"], ensure_ascii=False, indent=2).replace("```", "\\u0060\\u0060\\u0060"),
                  "```", ""]
    else:
        terminal = state.get("terminal") or {}
        lines += ["停止原因：" + cell(terminal.get("reason", "调查尚未结束")), ""]
        if terminal.get("question"):
            lines += ["待澄清：" + cell(terminal["question"]), ""]
    lines += ["## 修订记录", "",
              "通过仅指草稿字段与证据校验；模型修订理由保留原话，未经规划器验证。"
              "计划采购与制造成本和操作数量以本页受信摘要为准，具体动作以归档的 "
              "plan.json / change-set.json 为准。", "",
              "| 版本 | 改动字段 | 草稿字段与证据校验 | 模型修订理由（原文） |",
              "| --- | --- | --- | --- |"]
    for row in state["drafts"]:
        draft = row["draft"] if isinstance(row["draft"], dict) else {}
        lines.append("| " + " | ".join(map(cell, (
            row["revision"], row["changed_fields"],
            "通过" if row["validation"]["valid"] else row["validation"]["issues"],
            draft.get("reason", "未提供")))) + " |")
    lines += ["", "## 资源与身份", "",
              "业务工具用量：" + cell(state["usage"]), "",
              "模型 token、墙钟耗时和费用以运行 summary / token-budget / evaluation 文件为准；"
              "未记录的费用为未知，不按零计。", "",
              f"- 事件 SHA-256：`{state['event_sha256']}`",
              f"- 政策 SHA-256：`{state['policy_sha256']}`",
              f"- 完整合同 SHA-256：`{state.get('contract_sha256') or '无完整合同'}`"]
    if review:
        lines += [f"- 请求 SHA-256：`{review['request_sha256']}`",
                  f"- 事实 SHA-256：`{review['facts_sha256']}`",
                  f"- 结果包 SHA-256：`{state['prepared']['review_sha256']}`"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--store", type=Path, help="Trusted recovery store for the default result card")
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args(argv)
    state = read_json(args.state)
    review = read_json(args.review) if args.review else None
    rendered = render_report(state, review, store=args.store)
    if args.summary_output:
        from forgeflow.recovery.storage import atomic_json
        summary = trusted_summary(state, review, store=args.store)
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(args.summary_output, summary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
