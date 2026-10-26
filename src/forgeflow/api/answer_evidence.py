"""Question-scoped public facts shared by deterministic and model case answers.

This module does not fetch, authorize, execute, or ask a model. Its input must be
the authenticated public case captured for the turn. Lists are explicitly
bounded; omission never becomes evidence that the underlying record is absent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .evidence import public_evidence

MAX_ROWS = 24
MAX_EVIDENCE_CHARS = 24000
_MISSING = object()
_ACTION_LABELS = {
    "cancel_po": ("取消采购", "cancel purchase"),
    "create_po": ("新建采购", "create purchase"),
    "update_po": ("调整采购", "update purchase"),
    "cancel_mo": ("取消制造", "cancel manufacturing"),
    "create_mo": ("新建制造", "create manufacturing"),
    "update_mo": ("调整制造", "update manufacturing"),
    "manufacturing_order": ("保留制造单", "retained manufacturing order"),
    "purchase_order": ("保留采购单", "retained purchase order"),
}


def language(question: str, lang: str | None = None) -> str:
    if lang is not None and lang not in {"zh", "en"}:
        raise ValueError("unsupported case answer language")
    return lang or ("zh" if re.search(r"[\u3400-\u9fff]", question) else "en")


def requested_topics(question: str) -> list[str]:
    """Extend the existing case question vocabulary with required detail slots."""
    from .case_qa import _EXECUTION_WORDS

    text = question.casefold()
    # A request not to substitute costs for quantities does not request a cost answer.
    positive = re.split(r"不要|不要用|do not use|don't use", text, maxsplit=1)[0]
    action_text = re.sub(r"取消(?:费用|费)|cancellation (?:fees?|costs?)", "", positive)
    def has(words):
        return any(word in positive for word in words)
    topics = []
    if has(("状态", "涉及", "多少张", "多少台", "status", "how many orders", "how many units", "current state")):
        topics.append("status")
    if any(word in action_text for word in ("取消", "替代采购", "缺口", "几件", "件数", "数量明细", "哪些单据", "改了", "cancel", "replacement", "shortfall", "gap", "which documents", "how many items", "quantities")):
        topics.append("actions")
    if has(("保留", "保持不变", "原有安排", "retain", "preserv", "unchanged", "kept", "keep")):
        topics.append("preserved")
    if has(("成本", "费用", "金额", "节省", "cost", "spend", "saving", "expense", "purchase value")):
        topics.append("costs")
    if has(_EXECUTION_WORDS) or has(("实际改", "actually chang")):
        topics.append("execution")
    if has(("交付", "签收", "收到货", "收货", "deliver", "receipt by customer", "customer received", "signed for")):
        topics.append("delivery")
    return topics


def wants_document_detail(question: str) -> bool:
    """Narrow additive routing, preserving existing broader decision answers."""
    from .case_qa import _COMPARISON_WORDS
    from .impact_context import chain_intent

    text = question.casefold()
    if any(word in text for word in _COMPARISON_WORDS) or chain_intent(question):
        return False
    return any(word in text for word in (
        "保留", "保持不变", "缺口", "几件", "件数", "数量明细", "哪些单据", "替代采购",
        "preserved", "retained", "unchanged", "which documents", "shortfall", "replacement quantity",
    ))


def _get(value: Any, path: str, default: Any = None) -> Any:
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isascii() and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return default
    return value


def _simple(value: Any) -> bool:
    return (isinstance(value, (str, bool, int, float))
            and not (isinstance(value, str) and len(value) > 240)
            and not (isinstance(value, float) and not math.isfinite(value)))


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _num(value: Any) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else str(number)


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _pending(case: dict) -> bool:
    decision = case.get("decision") or {}
    return any(decision.get(key) is True for key in (
        "selection_pending", "revision_pending", "repreparation_pending", "preparation_pending",
    )) or case.get("reason") == "product_selection_conflict"


def _unknown_execution(case: dict) -> bool:
    from .case_qa import _execution_is_unknown
    return _execution_is_unknown(case) or case.get("state") == "executing"


def _record(public: dict, specs: dict[str, str]) -> dict:
    row, sources = {}, {}
    for name, path in specs.items():
        value = _get(public, path, _MISSING)
        if value is not _MISSING and value is not None and _simple(value):
            row[name], sources[name] = value, path
    row["sources"] = sources
    return row


def _document(public: dict, path: str, *, preserved: bool = False) -> dict:
    raw = _get(public, path, {})
    side = "before" if raw.get("kind", "").startswith("cancel") else "after"
    if not isinstance(raw.get(side), dict):
        side = "before" if isinstance(raw.get("before"), dict) else "after"
    specs = {"kind": f"{path}.kind", "document": f"{path}.document"}
    if preserved:
        specs.update(action=f"{path}.action", product_code=f"{path}.product_code")
    for field in ("quantity", "product_code", "supply_role", "uom_label", "uom_id"):
        direct = _get(public, f"{path}.{field}", _MISSING)
        specs[field] = f"{path}.{field}" if direct is not _MISSING and direct is not None else f"{path}.{side}.{field}"
    # Unit metadata may live in the cost document, rather than the action.
    # Join only an exact document/product/quantity match, retaining its true path.
    if not preserved and _get(public, specs["uom_label"]) is None:
        rows = _get(public, "plan.costs.lines", [])
        for i, candidate in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(candidate, dict) or candidate.get("document") != raw.get("document"):
                continue
            if candidate.get("product_code") != _get(public, specs["product_code"]):
                continue
            basis = candidate.get(side)
            if not isinstance(basis, dict) or _number(basis.get("quantity")) != _number(_get(public, specs["quantity"])):
                continue
            for field in ("uom_label", "uom_id"):
                specs[field] = f"plan.costs.lines.{i}.{side}.{field}"
            break
    return _record(public, specs)


def _citations(groups: dict, public: dict, lang: str) -> list[dict]:
    result, seen = [], set()
    labels = {"document": ("单据", "Document"), "quantity": ("数量", "Quantity"),
              "product_code": ("物料", "Product"), "kind": ("动作或单据类型", "Action or document type"),
              "action": ("安排动作", "Arrangement action"), "supply_role": ("供给角色", "Supply role"),
              "uom_label": ("计量单位", "Unit"), "uom_id": ("单位标识", "Unit identifier"),
              "state": ("案例状态", "Case state"), "status": ("执行状态", "Execution status"),
              "execution_status": ("执行状态", "Execution status"), "updated_at": ("记录更新时间", "Record updated at"),
              "order_ref": ("订单范围", "Order scope"), "units": ("产品数量", "Product quantity"),
              "customer": ("客户", "Customer"), "product": ("产品", "Product"), "outcome": ("记录结果", "Recorded outcome"),
              "baseline": ("原安排规划成本", "Original planned cost"), "planned": ("方案规划成本", "Proposed planned cost"),
              "known_delta": ("已知规划成本差额", "Known planned-cost difference"), "new_purchase_value": ("新建采购单金额", "New purchase value"),
              "purchase_increase": ("既有采购单金额增量", "Existing purchase value increase"),
              "actual_cost": ("实际财务成本", "Actual accounting cost"), "full_delta": ("完整费用差额", "Full cost difference"),
              "currency": ("币种", "Currency"), "planned_purchase": ("方案采购成本", "Planned purchasing cost"),
              "planned_manufacturing": ("方案制造成本", "Planned manufacturing cost"),
              "baseline_purchase": ("原安排采购成本", "Original purchasing cost"), "baseline_manufacturing": ("原安排制造成本", "Original manufacturing cost"),
              "readback_validated": ("执行回读确认", "Execution readback confirmed"), "verified_steps": ("回读核对步骤数", "Verified readback steps"),
              "verifier_status": ("验收状态", "Verification status"), "evaluation_scope": ("验收范围", "Verification scope"),
              "source": ("验收来源", "Verification source"), "strict_success": ("严格验收通过", "Strict verification passed"),
              "evaluation_valid": ("验收结果有效", "Verification valid"), "passed": ("通过规则数", "Passed rules"),
              "applicable": ("适用规则数", "Applicable rules"), "score": ("验收得分", "Verification score"), "reason": ("记录原因", "Recorded reason")}

    def collect(value):
        if isinstance(value, dict):
            for name, path in value.get("sources", {}).items():
                original = _get(public, path, _MISSING)
                if path not in seen and original is not _MISSING and original is not None and _simple(original) and not isinstance(original, bool):
                    result.append({"field": path, "label": labels.get(name, (f"案例字段：{name}", name))[lang != "zh"], "value": original})
                    seen.add(path)
            for key, item in value.items():
                if key != "sources":
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(groups)
    return result


def _retained_summary(rows: list[dict]) -> list[dict]:
    """Derived counts and same-product/unit totals; never fabricated field refs."""
    kinds: dict[str, dict] = {}
    for row in rows:
        kind = str(row.get("kind", "unknown"))
        bucket = kinds.setdefault(kind, {"kind": kind, "document_count": 0, "quantity_groups": []})
        bucket["document_count"] += 1
        quantity = _number(row.get("quantity"))
        if quantity is None or not row.get("product_code") or not row.get("uom_label"):
            continue
        identity = (row["product_code"], row["uom_label"], row.get("uom_id"))
        match = next((group for group in bucket["quantity_groups"] if
                      (group["product_code"], group["uom_label"], group.get("uom_id")) == identity), None)
        if match is None:
            bucket["quantity_groups"].append({"product_code": identity[0], "uom_label": identity[1],
                                              "uom_id": identity[2], "quantity": _num(quantity)})
        else:
            match["quantity"] = _num(_number(match["quantity"]) + quantity)
    return list(kinds.values())


def _order_count(reference: Any) -> int | None:
    """Recognize the same explicit compact range used by the workbench."""
    if not isinstance(reference, str):
        return None
    match = re.fullmatch(r"SO-(\d+)\s*\.\.\s*(\d+)", reference)
    if not match:
        return None
    first, last = map(int, match.groups())
    return last - first + 1 if last >= first else None


def question_evidence(case: dict[str, Any], question: str, lang: str | None = None, *,
                      max_rows: int = MAX_ROWS, max_chars: int = MAX_EVIDENCE_CHARS) -> dict[str, Any]:
    """Return bounded facts, exact scalar field references and coverage per topic.

    `available` means the selected region was read, not that every optional field
    exists. `not_recorded` is only used after inspecting the complete region.
    `omitted` indicates an incomplete projection requiring further scoped reads.
    """
    from .case_context import answer_context
    from .case_qa import _execution_answer

    if type(max_rows) is not int or not 1 <= max_rows <= 100:
        raise ValueError("max_rows must be between 1 and 100")
    if type(max_chars) is not int or not 2048 <= max_chars <= 64000:
        raise ValueError("max_chars must be between 2048 and 64000")
    lang = language(question, lang)
    public = public_evidence(case)
    topics = requested_topics(question)
    groups, coverage = {}, []

    def cover(topic, status, *, total=None, shown=None, reason=None):
        row = {"topic": topic, "status": status}
        if total is not None:
            row["total"] = total
        if shown is not None:
            row["shown"] = shown
        if reason:
            row["reason"] = reason
        coverage.append(row)

    for topic in topics:
        if topic in {"actions", "preserved", "costs"} and _pending(public):
            cover(topic, "unavailable", reason="plan_confirmation_pending")
            continue
        if topic in {"actions", "preserved"}:
            path = "plan.actions" if topic == "actions" else "plan.costs.lines"
            raw = _get(public, path, _MISSING)
            if not isinstance(raw, list):
                cover(topic, "not_recorded", reason="region_not_recorded")
                continue
            indexed = [(i, row) for i, row in enumerate(raw)
                       if isinstance(row, dict) and (topic != "preserved" or row.get("action") == "keep")]
            rows = [_document(public, f"{path}.{i}", preserved=topic == "preserved") for i, _ in indexed[:max_rows]]
            groups[topic] = {"rows": rows}
            invalid = sum(not isinstance(row, dict) for row in raw)
            cover(topic, "omitted" if len(indexed) > len(rows) or invalid else "available",
                  total=len(indexed) if not invalid else None, shown=len(rows),
                  reason="invalid_rows" if invalid else "row_limit" if len(indexed) > len(rows) else None)
        elif topic == "costs":
            specs = {"baseline": "plan.costs.baseline.total", "planned": "plan.costs.planned.total",
                     "known_delta": "plan.costs.known_delta", "new_purchase_value": "plan.costs.new_purchase_value",
                     "purchase_increase": "plan.costs.purchase_increase", "actual_cost": "plan.costs.actual_cost",
                     "full_delta": "plan.costs.full_delta", "currency": "plan.costs.basis.currency",
                     "comparable": "plan.costs.comparable", "planned_purchase": "plan.costs.planned.purchase",
                     "planned_manufacturing": "plan.costs.planned.manufacturing",
                     "baseline_purchase": "plan.costs.baseline.purchase", "baseline_manufacturing": "plan.costs.baseline.manufacturing"}
            if _get(public, specs["planned"]) is None:
                specs["planned"] = "plan.new_spend"
            row = _record(public, specs)
            row["unknown"] = [name for name, path in specs.items() if _get(public, path) is None]
            groups[topic] = row
            cover(topic, "available" if row["sources"] else "not_recorded")
        elif topic == "status":
            groups[topic] = _record(public, {"state": "state", "updated_at": "updated_at",
                "order_ref": "order.ref", "units": "order.units", "customer": "order.customer",
                "product": "order.product", "outcome": "result.outcome",
                "execution_status": "execution.status", "readback_validated": "execution.readback.execution_validated"})
            count = _order_count(groups[topic].get("order_ref"))
            if count is not None:
                groups[topic].update(order_count=count, order_count_basis="derived from inclusive order.ref range")
            cover(topic, "available" if groups[topic]["sources"] else "not_recorded")
        elif topic == "execution":
            if _unknown_execution(public):
                groups[topic] = _record(public, {"status": "execution.status", "state": "state", "reason": "reason"})
                groups[topic]["summary"] = _execution_answer(public, lang)["answer"]
                cover(topic, "unavailable", reason="execution_outcome_unknown")
                continue
            raw = public.get("execution")
            if not isinstance(raw, dict):
                cover(topic, "not_recorded", reason="execution_not_recorded")
                continue
            groups[topic] = _record(public, {"status": "execution.status", "readback_validated": "execution.readback.execution_validated",
                "verified_steps": "execution.readback.verified_steps", "verifier_status": "execution.verifier.status",
                "evaluation_scope": "execution.verifier.evaluation_scope", "source": "execution.verifier.source",
                "strict_success": "execution.verifier.strict_success", "evaluation_valid": "execution.verifier.evaluation_valid",
                "passed": "execution.verifier.passed_rule_count", "applicable": "execution.verifier.applicable_rule_count",
                "score": "execution.verifier.score"})
            # Preserve the existing fail-closed verifier interpretation in the
            # selected evidence itself; rendering must not revisit omitted data.
            groups[topic]["summary"] = _execution_answer(public, lang)["answer"]
            writes = raw.get("writes")
            if isinstance(writes, list):
                groups[topic]["writes"] = [_document(public, f"execution.writes.{i}") for i, row in enumerate(writes[:max_rows]) if isinstance(row, dict)]
                groups[topic]["write_count"] = len(writes)
                groups[topic]["write_count_basis"] = "document entries, not ERP transactions"
                partial = len(groups[topic]["writes"]) != len(writes)
                cover(topic, "omitted" if partial else "available", total=len(writes), shown=len(groups[topic]["writes"]), reason="row_limit_or_invalid_rows" if partial else None)
            else:
                groups[topic]["writes_unknown"] = True
                cover(topic, "available", reason="write_details_not_recorded")
        elif topic == "delivery":
            # No CaseFile contract currently binds a customer receipt to this case.
            # Never promote free-form text, plan.on_time or readback into a delivery fact.
            groups[topic] = {"boundary": "planning feasibility, ERP changes and task verification do not establish physical production, shipment or customer receipt", "sources": {}}
            cover(topic, "not_recorded", reason="customer_delivery_evidence_not_bound")

    result = {"schema_version": 1, "language": lang, "case_context": answer_context(public),
              "topics": topics, "groups": groups, "coverage": coverage, "citations": []}

    def refresh():
        preserved = groups.get("preserved")
        if preserved is not None:
            preserved["summary"] = _retained_summary(preserved["rows"])
            preserved["summary_basis"] = "derived from shown entries only; quantities grouped by kind, product and unit"
        result["citations"] = _citations(groups, public, lang)
        # Fingerprint only the authenticated public content, not owner identity.
        result["snapshot_id"] = hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    refresh()
    while len(_encode(result)) > max_chars:
        removed = False
        for entry in reversed(coverage):
            group = groups.get(entry["topic"], {})
            rows = group.get("rows", group.get("writes"))
            if isinstance(rows, list) and rows:
                rows.pop()
                entry.update(status="omitted", shown=len(rows), reason="character_limit")
                removed = True
                break
        if not removed:
            for entry in reversed(coverage):
                if entry["topic"] in groups:
                    del groups[entry["topic"]]
                    entry.update(status="omitted", reason="character_limit")
                    removed = True
                    break
        if not removed:
            # Public provenance strings are optional if a malformed source makes
            # them larger than the entire context budget.
            result["case_context"] = {"schema_version": 1}
            break
        refresh()
    return result


def _document_text(row: dict, lang: str) -> str:
    zh = lang == "zh"
    doc = row.get("document", "单号未记录" if zh else "identifier unrecorded")
    kind = _ACTION_LABELS.get(row.get("kind"), (row.get("kind", "单据"), row.get("kind", "document")))[not zh]
    quantity = _num(row["quantity"]) if "quantity" in row else ("未知" if zh else "unknown")
    unit = row.get("uom_label", "单位未记录" if zh else "unit unrecorded")
    if zh and unit == "Units":
        unit = "件（原单位 Units）"
    product = row.get("product_code", "物料未记录" if zh else "product unrecorded")
    role = row.get("supply_role")
    role_text = {"finished": "成品", "component": "组件"}.get(role, role) if zh else role
    suffix = f"；{role_text}" if role_text else ""
    planned = "（计划内新单标识）" if zh else " (planned new-document identifier)"
    return f"{doc}{planned if str(doc).startswith('new:') else ''}：{kind}，{quantity} {unit}，{product}{suffix}" if zh else f"{doc}{planned if str(doc).startswith('new:') else ''}: {kind}, {quantity} {unit}, {product}{suffix}"


def _three_sentence_answer(question: str, evidence: dict, lang: str) -> str | None:
    """Compact only complete, small factual groups; detail requests stay lists."""
    if not re.search(r"三(?:句|句话)|(?:three|3) sentences", question, re.I):
        return None
    groups, topics = evidence["groups"], set(evidence["topics"])
    states = {row["topic"]: row["status"] for row in evidence["coverage"]}
    if any(state != "available" for topic, state in states.items() if topic != "delivery"):
        return None
    zh = lang == "zh"
    sentences = []
    if topics == {"actions", "preserved"} and not re.search(r"明细|编号|逐行|each document|line by line", question, re.I):
        actions, retained = groups["actions"]["rows"], groups["preserved"]
        if not 1 <= len(actions) <= 4 or not retained.get("summary"):
            return None
        sentences.append(("历史方案记录：" if zh else "The historical plan records: ") + "；".join(_document_text(row, lang) for row in actions))
        summaries = []
        for summary in retained["summary"]:
            label = _ACTION_LABELS.get(summary["kind"], (summary["kind"], summary["kind"]))[not zh]
            amount_groups = summary["quantity_groups"]
            if len(amount_groups) > 4:
                return None
            totals = "；".join(f"{row['product_code']} {_num(row['quantity'])} {row['uom_label']}" for row in amount_groups)
            summaries.append(f"{summary['document_count']} 张{label}" + (f"（按同物料和单位统计：{totals}）" if totals else "") if zh else f"{summary['document_count']} {label}s" + (f" (same-product/unit totals: {totals})" if totals else ""))
        sentences.append(("保留安排：" if zh else "Existing documents retained: ") + "；".join(summaries))
        sentences.append("这些是历史失效供给和替代安排，不表示当前仍然缺货；不同物料与单据层级的数量不相加" if zh else "These are historical disrupted supply and replacement plans, not a claim of a current shortage; quantities across different products or document levels are not added")
    elif topics == {"costs"}:
        costs = groups["costs"]
        keys = ("baseline", "planned", "known_delta", "new_purchase_value")
        if any(_number(costs.get(key)) is None for key in keys) or costs.get("comparable") is not True or not costs.get("currency"):
            return None
        if not {"actual_cost", "full_delta"}.issubset(costs.get("unknown", [])):
            return None
        labels = ("原安排规划成本", "方案规划成本", "已知规划成本差额", "新建采购单金额") if zh else ("Original planned cost", "Proposed planned cost", "Known planned-cost difference", "New purchase value")
        sentences.append("；".join(f"{label} {costs['currency']} {_number(costs[key]):,.2f}" for key, label in zip(keys, labels)))
        delta = _number(costs["known_delta"])
        direction = ("已知规划成本增加，不代表节省" if delta > 0 else "已知规划成本下降，尚不证明实际节省" if delta < 0 else "已知规划成本差额为零，未知费用仍不能当作零") if zh else ("Known planned cost increases, which is not a saving" if delta > 0 else "Known planned cost decreases, which does not establish actual savings" if delta < 0 else "Known planned-cost difference is zero, while unknown fees remain unknown")
        sentences.append(direction + ("；规划成本包含保留的采购与制造安排，仅现有库存按既有规划口径作沉没成本处理" if zh else "; planned cost includes retained purchasing and manufacturing, while only existing stock is treated as sunk in this planning basis"))
        sentences.append("实际财务成本未记录；取消费用与税费调整等完整费用差额仍未知；规划金额本身不能推导实际收益" if zh else "Actual accounting cost is unrecorded; the full cost difference including cancellation fees and tax adjustments remains unknown; planned amounts cannot establish actual returns")
    elif topics == {"actions", "execution", "delivery"}:
        writes, actions = groups["execution"].get("writes", []), groups["actions"]["rows"]
        def signature(row):
            return row.get("kind"), row.get("document"), _number(row.get("quantity"))
        if not 1 <= len(writes) <= 4 or [signature(row) for row in writes] != [signature(row) for row in actions]:
            return None
        summary = groups["execution"].get("summary")
        if not summary:
            return None
        sentences.append(("历史执行记录中的单据动作：" if zh else "Document actions in the historical execution record: ") + "；".join(_document_text(row, lang) for row in writes))
        sentences.append(summary.strip("。.").replace("。", "；") if zh else re.sub(r"\.\s+", "; ", summary.rstrip(".")))
        sentences.append("原始验收只核对该任务的适用业务规则；ERP 回读不能证明生产完工、发货或客户签收，本案例未绑定可核验的客户交付证据，实际交付仍未知" if zh else "Original verification covers only this task's applicable business rules; ERP readback does not establish production, shipment or customer receipt, and this case has no bound verifiable customer-delivery evidence")
    return "\n\n".join(sentence + ("。" if zh else ".") for sentence in sentences) if sentences else None


def grounded_answer(question: str, case: dict[str, Any], lang: str | None = None) -> dict[str, Any]:
    """Render the selected facts without changing or inferring business records."""
    from .case_qa import _answer_case_impl, _missing_plan

    lang = language(question, lang)
    evidence = question_evidence(case, question, lang)
    zh = lang == "zh"
    if _pending(case) or not evidence["topics"]:
        if _get(case, "decision.preparation_pending") is True:
            reply = _missing_plan(case, lang)
        elif case.get("reason") == "product_selection_conflict" and case.get("execution"):
            reply = {"answer": "方案选择仍存在版本冲突，旧方案不能作为当前可执行安排；请核对已有执行回执与最新版本。" if zh else "The plan choice still has a version conflict. The earlier plan cannot establish the current executable arrangement; reconcile the existing receipt with the latest version.", "mode": "case", "citations": []}
        else:
            reply = _answer_case_impl(question, case, lang)
        return {**reply, "evidence": evidence, "source_mode": "deterministic"}
    compact = _three_sentence_answer(question, evidence, lang)
    if compact is not None:
        return {"answer": compact, "mode": "case", "citations": evidence["citations"], "evidence": evidence, "source_mode": "deterministic"}
    parts = []
    for cov in evidence["coverage"]:
        topic, state = cov["topic"], cov["status"]
        group = evidence["groups"].get(topic, {})
        if topic in {"actions", "preserved"}:
            title = ("异常恢复方案的单据变更" if topic == "actions" else "方案保留的既有单据") if zh else ("Recovery-plan changes" if topic == "actions" else "Existing documents retained by the plan")
            if state == "not_recorded":
                parts.append(title + ("：完整案例未记录相关明细。" if zh else ": the complete case does not record these details."))
            elif state == "unavailable":
                parts.append(title + ("：方案仍待确认，暂时不能使用旧版明细回答。" if zh else ": the plan is awaiting confirmation; earlier details cannot establish the requested result."))
            else:
                rows = group.get("rows", [])
                if not rows and state == "available":
                    parts.append(title + ("：该区域明确记录为空列表。" if zh else ": this region explicitly records an empty list."))
                else:
                    parts.append(title + "：\n" + "\n".join("- " + _document_text(row, lang) for row in rows))
                if state == "omitted":
                    parts.append((f"本次只展开 {len(rows)} 项，尚未覆盖全部明细；其余需继续读取同版本案例。" if zh else f"Only {len(rows)} entries are expanded; the remaining details require another read of the same case version."))
                if topic == "actions":
                    parts.append("这些是方案记录的失效供给与替代安排，不表示当前仍然缺货；实际执行另看回执。" if zh else "These are recorded plan changes, not a claim that the historical shortage remains; actual execution requires its receipt.")
                else:
                    parts.append("不同物料与单据层级分别列出，数量不跨物料相加。" if zh else "Products and document levels are listed separately; quantities are not summed across materials.")
        elif topic == "costs":
            if not group or state == "not_recorded":
                parts.append(("成本依据尚未展开，暂时无法核对。" if state == "omitted" else "完整案例没有记录可核对的成本金额。") if zh else ("Cost evidence is not expanded and cannot yet be checked." if state == "omitted" else "The complete case does not record verifiable cost amounts."))
                continue
            labels = {"baseline": ("原安排规划成本", "Original planned cost"), "planned": ("方案规划成本", "Proposed planned cost"),
                "known_delta": ("已知规划成本差额", "Known planned-cost difference"), "new_purchase_value": ("新建采购单金额", "New purchase value")}
            if "actual_cost" in group:
                labels["actual_cost"] = ("已记录实际财务成本", "Recorded actual accounting cost")
            if "full_delta" in group:
                labels["full_delta"] = ("已记录完整费用差额", "Recorded full cost difference")
            currency = group.get("currency", "币种未记录" if zh else "currency unrecorded")
            values = []
            for name, label in labels.items():
                value = group.get(name)
                amount = f"{_number(value):,.2f}" if _number(value) is not None else ("未知" if zh else "unknown")
                values.append(f"{label[not zh]} {currency} {amount}")
            parts.append("；".join(values) + "。")
            delta = _number(group.get("known_delta"))
            if delta is not None and group.get("comparable") is True:
                parts.append(("已知规划成本增加，不代表节省。" if delta > 0 else "已知规划成本下降，尚不证明实际节省。" if delta < 0 else "已知规划成本差额为零，未知费用仍不能当作零。") if zh else ("Known planned cost increases; this is not a saving." if delta > 0 else "Known planned cost decreases; actual savings are not established." if delta < 0 else "Known planned-cost difference is zero; unknown fees remain unknown."))
            elif delta is not None:
                parts.append("记录的差额尚缺可比基线确认，不能据此判断成本增减或节省。" if zh else "The recorded difference lacks confirmation of a comparable baseline; it cannot establish an increase, decrease or saving.")
            parts.append("规划成本包含保留的采购与制造安排；仅现有库存按既有规划口径作沉没成本处理。规划金额本身不能推导实际收益。" if zh else "Planned cost includes retained purchasing and manufacturing; only existing stock is treated as sunk in this planning basis. Planning amounts alone cannot establish actual returns.")
            if "actual_cost" in group.get("unknown", []):
                parts.append("实际财务成本未记录。" if zh else "Actual accounting cost is unrecorded.")
            if "full_delta" in group.get("unknown", []):
                parts.append("取消费用与税费调整等完整费用差额仍未知。" if zh else "The full cost difference, including cancellation fees and tax adjustments, remains unknown.")
        elif topic == "status":
            if not group:
                parts.append("状态依据尚未取得，暂时无法核对。" if zh else "Status evidence has not been obtained and cannot yet be checked.")
                continue
            case_state = group.get("state", "未记录" if zh else "unrecorded")
            state_label = {"succeeded": "已完成", "executing": "执行中", "execution_unknown": "执行结果未知", "awaiting_approval": "待批准", "awaiting_selection": "待选择", "failed": "失败"}.get(case_state, case_state) if zh else case_state
            parts.append(f"案例记录状态：{state_label}。" if zh else f"Recorded case state: {state_label}.")
            if "order_count" in group:
                parts.append(f"涉及 {group['order_count']} 张订单（按订单范围计算）。" if zh else f"The order range contains {group['order_count']} orders.")
            if "units" in group:
                parts.append(f"记录的产品数量为 {_num(group['units'])} 台。" if zh else f"Recorded product quantity: {_num(group['units'])} units.")
            if group.get("state") not in {"execution_unknown", "executing"} and group.get("readback_validated") is True and group.get("execution_status") not in {None, "unknown", "not_started"}:
                parts.append("回读记录已确认本次单据执行；不代表客户交付。" if zh else "The readback record confirms the document execution, not customer delivery.")
            else:
                parts.append("执行回读尚未确认，不能仅凭案例状态推断 ERP 写入结果。" if zh else "Readback is not confirmed; case state alone cannot establish ERP write outcomes.")
            if "order_ref" in group:
                parts.append(("订单范围：" if zh else "Order scope: ") + str(group["order_ref"]) + (f"；{group['customer']}" if group.get("customer") else ""))
            if group.get("updated_at"):
                parts.append(("以上为案例历史记录，更新于 " if zh else "These are historical case records, updated at ") + str(group["updated_at"]))
        elif topic == "execution":
            parts.append(group.get("summary") or ("执行依据尚未取得，暂时无法核对。" if zh else "Execution evidence has not been obtained and cannot yet be checked."))
            if group.get("writes") and state != "unavailable":
                parts.append(("记录中的单据动作：\n" if zh else "Recorded document actions:\n") + "\n".join("- " + _document_text(row, lang) for row in group["writes"]))
            if state == "omitted":
                parts.append("执行明细尚未完整展开，不能称以上条目是全部。" if zh else "Execution details are not fully expanded; the entries above are not the complete list.")
        elif topic == "delivery":
            parts.append("原始验收只核对该任务的适用业务规则；ERP 回读不证明实际生产完工、发货或客户签收。这份案例没有绑定可核验的客户交付证据，实际交付仍未知。" if zh else "Original verification checks the task's applicable business rules. ERP readback does not establish physical production, shipment or customer receipt. This case has no bound verifiable customer-delivery evidence; actual delivery remains unknown.")
    return {"answer": "\n\n".join(parts), "mode": "case", "citations": evidence["citations"],
            "evidence": evidence, "source_mode": "deterministic"}


def _identifier_present(identifier: Any, text: str) -> bool:
    identifier = str(identifier)
    if identifier in text:
        return True
    match = re.fullmatch(r"(.*?)(\d+)", identifier)
    if not match:
        return False
    prefix, number = match.groups()
    pattern = re.escape(prefix) + r"(\d+)\s*(?:[-–—~至到]|through|to)\s*(?:" + re.escape(prefix) + r")?(\d+)"
    return any(int(low) <= int(number) <= int(high) for low, high in re.findall(pattern, text))


def _affirmed(pattern: str, text: str) -> bool:
    """Catch specific unsupported assertions while allowing explicit negation."""
    negation = re.compile(r"不|未|无法|不能|没有|是否|能否|并非|未知|not|never|cannot|no\b|unknown|whether|without|doesn.t", re.I)
    for match in re.finditer(pattern, text, re.I):
        # Negation belongs to the current clause, not an earlier disclaimer.
        prefix = re.split(r"[。！？\n;；]", text[:match.start()])[-1][-80:]
        if not negation.search(prefix):
            return True
    return False


_AMOUNT = r"(?<![A-Za-z0-9_.])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_.])"
_QUANTITY = rf"({_AMOUNT})\s*(?:件|台|个(?:单位)?|kg\b|units?\b|items?\b)"
_ACTION_WORDS = {
    "cancel": r"取消(?!费)|撤销|作废|cancel(?:led|ed)?(?: purchase)?",
    "create": r"替代(?:采购)?|新建(?:采购)?|新增采购|新采购(?:单)?|补采|补购|补齐|补回|replacement|new purchase|create(?:d)? purchase",
    "update": r"调整(?:采购)?|修改采购|update(?:d)? purchase",
}


def _binding_text(text: str) -> str:
    # Preserve clause delimiters; only remove separators inside numeric amounts.
    return re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text).replace("**", "").casefold()


def _action_claims(text: str) -> list[tuple[str, str]]:
    events = sorted((match.start(), match.end(), kind)
                    for kind, pattern in _ACTION_WORDS.items() for match in re.finditer(pattern, text))
    claims = []
    for index, (start, end, kind) in enumerate(events):
        stop = events[index + 1][0] if index + 1 < len(events) else len(text)
        fragment = re.split(r"[。;；\n]", text[end:stop], maxsplit=1)[0][:180]
        claims.append((kind, text[start:end] + fragment))
    return claims


def _document_quantities(document: str, text: str, documents: list[str]) -> list[str]:
    """Only explicit per-document quantities; ranges and totals are not rows."""
    suffix = re.fullmatch(r"(.*?)(\d+)", document)
    ranges = []
    if suffix:
        prefix = re.escape(suffix[1])
        pattern = prefix + r"\d+\s*(?:[-–—~至到]|through|to)\s*(?:" + prefix + r")?\d+"
        ranges = [match.span() for match in re.finditer(pattern, text, re.I)]
    quantities = []
    for match in re.finditer(re.escape(document) + r"(?!\d)", text, re.I):
        if any(start <= match.start() < end for start, end in ranges):
            continue
        tail = text[match.end():match.end() + 180]
        first_line = tail.split("\n", 1)[0]
        if "|" in first_line:
            table_values = [part.strip() for part in first_line.split("|")]
            quantity = next((part for part in table_values if re.fullmatch(_AMOUNT, part)), None)
            if quantity is not None:
                quantities.append(quantity)
            continue
        other_offsets = [tail.find(other.casefold()) for other in documents if other != document and other.casefold() in tail]
        if other_offsets:
            tail = tail[:min(other_offsets)]
        tail = re.split(r"[。;；]", tail, maxsplit=1)[0]
        quantity = re.search(_QUANTITY, tail, re.I) or re.search(rf"(?:数量|quantity)\s*[:：]?\s*({_AMOUNT})", tail, re.I)
        if quantity and not re.search(r"合计|总计|共|total|combined|[、]", tail[:quantity.start()], re.I):
            quantities.append(quantity.group(1))
    return quantities


def _cost_claims(text: str) -> list[tuple[str, Decimal]]:
    """Bind stated amounts to explicit cost labels, including unasked claims."""
    patterns = {
        "baseline": r"原安排(?:规划)?成本|原安排(?:的)?(?:基线|基准)(?:成本)?|原安排规划成本|基线(?:成本)?|baseline|original (?:planned )?cost",
        "planned": r"方案(?:规划)?成本|计划(?:总额|成本)|规划总(?:成本|额)|planned (?:cost|total)|proposed planned cost",
        "new_purchase_value": r"(?:新建|新增|新)采购(?:单)?(?:金额|增量|额)|new_purchase_value|new purchase value|newly created purchase (?:amount|value)",
        "purchase_increase": r"(?:既有|已有|原有)采购(?:单)?(?:金额)?增量|existing purchase (?:amount )?increase",
        "ambiguous_purchase_increase": r"采购增量|purchase increase",
        "actual_cost": r"实际(?:财务|财务结算)?成本|actual (?:accounting )?cost",
        "full_delta": r"完整(?:费用)?差额|full (?:cost )?difference",
        "known_delta": r"已知(?:规划成本)?(?:差额|增加额)|known_delta|known (?:planned.cost )?difference|(?:增加|高出|上升|减少|降低)(?:了|额)?|difference|increase|decrease",
    }
    matches = sorted((m.start(), m.end(), key) for key, pattern in patterns.items() for m in re.finditer(pattern, text, re.I))
    selected = []
    for start, end, key in sorted(matches, key=lambda item: (item[0], -item[1])):
        if selected and start < selected[-1][1]:
            continue
        selected.append((start, end, key))
    claims = []
    for index, (start, end, key) in enumerate(selected):
        stop = selected[index + 1][0] if index + 1 < len(selected) else len(text)
        after = re.split(r"[。;；\n]", text[end:stop], maxsplit=1)[0][:100]
        before = re.split(r"[。;；\n]", text[:start])[-1][-50:]
        previous = (re.search(rf"({_AMOUNT})\s*(?:美元|usd|dollars?|元|\$)\s*(?:的)?\s*$", before, re.I)
                    if key != "known_delta" or "差额" in text[start:end] else None)
        # A label must directly introduce an amount. Its explanatory alias,
        # component breakdown or subtraction formula is not another total.
        number = previous or re.match(
            rf"\s*[)）]?\s*(?:(?:总额|金额|合计)\s*)?"
            rf"(?:(?:[:：=]|为|是|约|记为|达到|is|equals|of|amounts to)\s*)?"
            rf"(?:(?:usd|美元|\$)\s*)?({_AMOUNT})", after, re.I)
        if number:
            value = _number(number.group(1))
            if value is not None:
                claims.append((key, value))
    return claims


def _unqualified_historical_countdown(text: str) -> bool:
    """A stored relative deadline needs a local historical qualifier."""
    countdown = re.compile(
        r"(?:距(?:离)?(?:交期|交付期限|交付日|截止日|到期).{0,8}(?:还(?:有|剩)?|剩余|仅剩)|还剩)\s*\d+(?:\.\d+)?\s*天"
        r"|\bdue\s+in\s+\d+(?:\.\d+)?\s+days?\b"
        r"|\b\d+(?:\.\d+)?\s+days?\s+(?:left|remaining)\b", re.I)
    for sentence in re.split(r"[。;；\n]|(?<!\d)\.(?!\d)", text):
        for match in countdown.finditer(sentence):
            before, after = sentence[:match.start()], sentence[match.end():match.end() + 60]
            if re.search(r"(?:不是|并非|不能说|不能称|不应称|not|cannot say|isn't).{0,8}$", before, re.I):
                continue
            qualified_before = re.search(r"历史|快照|当时|截至|记录(?:显示|中|于|记载)|historical|snapshot|as of|at the time|recorded on|record (?:shows|states)", before, re.I)
            qualified_after = re.search(r"历史|快照|当时|记录时点|historical|snapshot|as of|at the time", after, re.I)
            if not qualified_before and not qualified_after:
                return True
    return False


def validate_model_answer(question: str, case: dict[str, Any], text: str,
                          lang: str | None = None) -> dict[str, Any]:
    """Conservative mechanical checks, not a claim of general semantic proof.

    Known numeric/document slots and required coverage are checked. Unrecognized
    questions preserve the legacy route with no invented field citations.
    """
    evidence = question_evidence(case, question, lang)
    normalized = text.replace(",", "").casefold()
    binding = _binding_text(text)
    question_text = question.casefold()
    details_requested = any(word in question_text for word in (
        "明细", "单据", "编号", "逐行", "which documents", "document numbers", "line by line", "each document",
    ))
    issues = []
    if not text.strip():
        issues.append("empty_answer")
    if _unqualified_historical_countdown(binding):
        issues.append("historical_deadline_presented_as_current_countdown")
    if _pending(case):
        issues.append("plan_confirmation_requires_deterministic_answer")
    if _unknown_execution(case) and set(evidence["topics"]) & {"execution", "status", "delivery"}:
        issues.append("unknown_execution_requires_deterministic_answer")

    def number_present(value):
        number = _number(value)
        if number is None:
            return True
        return any(_number(found) == number for found in re.findall(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?(?![A-Za-z0-9_.])", normalized))

    for cov in evidence["coverage"]:
        topic, state = cov["topic"], cov["status"]
        group = evidence["groups"].get(topic, {})
        if state in {"omitted", "unavailable"} and not re.search(r"未|未知|待|不足|不完整|尚|unknown|unavailable|partial|not.*complete|pending|not.*confirm", normalized):
            issues.append(f"unreported_coverage:{topic}")
        if topic in {"actions", "preserved"} and state == "available":
            documents = [str(row["document"]) for row in group.get("rows", []) if row.get("document")]
            for row in group.get("rows", []):
                document = row.get("document")
                if details_requested and document and not _identifier_present(document, text):
                    if not (str(document).startswith("new:") and re.search(r"新建|替代|new.*purchase|replacement", normalized)):
                        issues.append(f"missing_document:{document}")
                if (topic == "actions" or details_requested) and not number_present(row.get("quantity")):
                    issues.append(f"missing_quantity:{document}")
                if document and str(document) in text and _number(row.get("quantity")) is not None:
                    for value in _document_quantities(str(document), binding, documents):
                        if _number(value) != _number(row["quantity"]):
                            issues.append(f"contradictory_quantity:{document}")
                if topic == "actions":
                    kind = str(row.get("kind", "")).split("_")[0]
                    relevant = [fragment for action, fragment in _action_claims(binding) if action == kind]
                    if kind in _ACTION_WORDS and not relevant and not (document and str(document).casefold() in binding):
                        issues.append(f"missing_action:{kind}")
                    # An action label is strong enough to reject a contradiction
                    # even when its identifier was reasonably omitted in a summary.
                    for fragment in relevant:
                        quantity = re.search(_QUANTITY, fragment, re.I)
                        if quantity and _number(quantity.group(1)) != _number(row.get("quantity")):
                            issues.append(f"contradictory_action_quantity:{kind}")
                        role = re.search(r"成品|组件|零部件|finished(?: goods)?|components?", fragment, re.I)
                        expected_role = row.get("supply_role")
                        if role and expected_role in {"finished", "component"}:
                            actual_role = "finished" if re.fullmatch(r"成品|finished(?: goods)?", role.group(), re.I) else "component"
                            if actual_role != expected_role:
                                issues.append(f"contradictory_supply_role:{kind}")
            if topic == "preserved" and not details_requested:
                all_ids = bool(group.get("rows")) and all(_identifier_present(row.get("document", ""), text) for row in group["rows"])
                if not all_ids:
                    for summary in group.get("summary", []):
                        kind = summary["kind"]
                        words = r"制造|生产|manufactur|\bmo\b" if kind == "manufacturing_order" else r"采购|purchase|\bpo\b" if kind == "purchase_order" else re.escape(kind)
                        if not re.search(words, normalized) or not number_present(summary["document_count"]):
                            issues.append(f"missing_preserved_summary:{kind}")
            if group.get("rows") and re.search(r"(?:没有|未)(?:写出|记录|列出).{0,12}(?:件数|数量|编号|单据)|no.{0,12}(?:quantit|document).*record", normalized):
                issues.append(f"false_absence:{topic}")
        if topic == "costs":
            required = ["planned"]
            if any(word in question_text for word in ("基线", "原安排成本", "原安排多少", "原安排、", "baseline", "original cost")):
                required.append("baseline")
            if any(word in question_text for word in ("增加", "减少", "差额", "节省", "difference", "increase", "delta", "saving")):
                required.append("known_delta")
            if any(word in question_text for word in ("新增采购", "新建采购", "新采购", "新增金额", "采购金额", "new purchase", "newly created purchase")):
                required.append("new_purchase_value")
            claims = _cost_claims(binding)
            for key in required:
                if key in group and not any(claim_key == key and value == _number(group[key]) for claim_key, value in claims):
                    issues.append(f"missing_cost:{key}")
            for key, value in claims:
                if key == "ambiguous_purchase_increase":
                    issues.append("ambiguous_purchase_increase")
                elif key in group and _number(group[key]) is not None and value != _number(group[key]):
                    issues.append(f"contradictory_cost:{key}")
                elif key in {"actual_cost", "full_delta"} and key in group.get("unknown", []):
                    issues.append(f"unrecorded_cost_claim:{key}")
            actual_unknown = re.search(r"实际.{0,24}(?:未知|未|尚|不能|为空|缺失)|(?:未知|未|尚|不能).{0,24}实际|actual.{0,60}(?:unknown|not|unavailable|unrecorded|null|empty)|not.{0,40}actual", normalized)
            fees_asked = any(word in question_text for word in ("取消费", "税费", "cancellation fee", "tax"))
            fees_unconfirmed = re.search(r"(?:取消费(?:用)?|税费).{0,55}(?:未知|未确认|未记录|未单列|不能确认|无法确认)|(?:没有|未)(?:单独)?列示.{0,20}(?:取消费|税费)|(?:无法|不能).{0,16}确认.{0,20}(?:取消费|税费)|(?:cancellation fees?|tax).{0,70}(?:unknown|unconfirmed|unrecorded|not confirmed)", normalized)
            if "actual_cost" in group.get("unknown", []) and not actual_unknown and not (fees_asked and fees_unconfirmed):
                issues.append("missing_actual_cost_boundary")
            if re.search(r"已有采购.{0,20}(?:当作|作为|是).{0,8}沉没成本|existing purchas.{0,30}(?:are|as).*sunk", normalized):
                issues.append("retained_purchase_sunk_cost")
            if (_number(group.get("known_delta")) or Decimal(0)) > 0 and _affirmed(r"(?:节省|节约)(?:了|\s)*(?:usd\s*)?\d|(?:saved|saving of)\s*(?:usd|\$)?\s*\d", normalized):
                issues.append("positive_cost_difference_called_saving")
        if topic == "status":
            if "units" in group and not number_present(group["units"]):
                issues.append("missing_order_units")
            if "order_count" in group and any(word in question_text for word in ("多少张", "多少单", "how many orders")):
                # The explicit compact range is also a valid representation.
                if not number_present(group["order_count"]) and group.get("order_ref") not in text:
                    issues.append("missing_order_count")
            if any(word in question_text for word in ("状态", "status", "current state")):
                state_terms = {
                    "succeeded": r"succeeded|success|completed|verified|已执行|已验证|成功|完成|验收.{0,8}通过",
                    "executing": r"executing|in progress|being applied|执行中|正在执行",
                    "execution_unknown": r"unknown|unconfirmed|未知|未确认|不能确认|无法确认",
                    "awaiting_approval": r"awaiting.approval|pending.approval|waiting.{0,20}approval|待批准|待审批|等待.{0,8}(?:批准|审批|决定)",
                    "awaiting_selection": r"awaiting.selection|pending.selection|waiting.{0,20}(?:choice|selection)|待选择|待选|等待.{0,8}选择",
                    "failed": r"failed|failure|失败",
                }.get(group.get("state"))
                found_state = (re.search(state_terms, normalized) if group.get("state") != "succeeded" else _affirmed(state_terms, binding)) if state_terms else True
                if not found_state:
                    issues.append("missing_case_state")
                if group.get("state") == "succeeded" and re.search(r"(?:尚未|还未|未曾|没有|未)(?:执行|成功|完成|验证)|not\s+(?:yet\s+)?(?:completed|successful|succeeded|verified)", binding):
                    # Scope the negation to execution/case completion, excluding
                    # the necessary customer-delivery disclaimer.
                    for clause in re.split(r"[。;；\n]", binding):
                        if re.search(r"案例|执行|状态|case|execution|status", clause) and re.search(r"(?:尚未|还未|未曾|没有|未)(?:执行|成功|完成|验证)|not\s+(?:yet\s+)?(?:completed|successful|succeeded|verified)", clause):
                            issues.append("contradictory_case_state")
        if topic == "execution" and state != "unavailable":
            if any(word in question_text for word in ("验收", "规则", "verif", "rules")):
                for key in ("passed", "applicable"):
                    if key in group and not number_present(group[key]):
                        issues.append(f"missing_verification:{key}")
            if re.search(r"(?:几|哪些|what|which).{0,12}(?:单据|document)|实际改|actually chang", question.casefold()):
                for row in group.get("writes", []):
                    document = row.get("document")
                    if document and not _identifier_present(document, text) and not (str(document).startswith("new:") and re.search(r"新建|替代|new.*purchase|replacement", normalized)):
                        issues.append(f"missing_written_document:{document}")
        if topic == "delivery":
            if not re.search(r"(?:不能|不等于|不代表|不证明|无法|未知|未记录|未提供|没有).{0,26}(?:交付|签收|收到|发货)|(?:交付|签收).{0,16}(?:未知|未确认)|(?:not|unknown|doesn't|cannot|no).{0,70}(?:deliver|receipt|shipment)", normalized):
                issues.append("missing_delivery_boundary")
            if re.search(r"更接近.{0,8}交付证据|closer to.{0,12}delivery evidence", normalized):
                issues.append("readback_is_not_delivery_evidence")
            if _affirmed(r"(?:已经|已)(?:完成)?(?:交付|签收|发货|生产完工)|(?:has|have)\s+(?:already\s+)?(?:been delivered|received|signed for)|delivery (?:is )?complete", normalized):
                issues.append("unbound_customer_delivery_claim")
        if topic == "execution" and _affirmed(r"(?:证明|通过|满足)(?:了)?法律合规|(?:proves|passed|meets).{0,20}legal compliance", normalized):
            issues.append("task_verification_called_legal_compliance")
    return {"valid": not issues, "issues": list(dict.fromkeys(issues)),
            "citations": evidence["citations"], "evidence": evidence}
