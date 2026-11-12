"""Finite reply interpretation and shared host admission of *proposals* only.

No function grants policy, queries ERP or reads an expected answer. The model
may use language paths the fixed interpreter does not recognize. Shared
validation checks typed fields and explicit conflicting/unsupported statements;
it does not require the model's kind to equal the rule interpreter's kind.
Only the case's later authenticated confirmation creates authorization.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation

KINDS = ("budget_authorization_proposal", "price_fact", "ambiguous", "out_of_scope", "cancel", "pause")
PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind"],
    "description": "Budget proposals require kind, order_id, amount and currency. For every other kind, send only kind and optional question; omit order_id, amount and currency entirely, including empty or null values.",
    "properties": {"kind": {"type": "string", "enum": list(KINDS)},
        "order_id": {"type": "string", "pattern": "^[1-9][0-9]*$"},
        "amount": {"type": "string"}, "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "question": {"type": "string", "maxLength": 1200}},
}
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?(?![A-Za-z0-9_.])")
_BUDGET = re.compile(r"\b(?:budget|cap|spending\s+limit|spend|authori[sz](?:e[ds]?|ation)|approv(?:e[ds]?|al)|allocat(?:e[ds]?|ion)|maximum|up\s+to|no\s+more\s+than)\b"
                     r"|预算|上限|最多|批准|授权|限额", re.I)
_FACT = re.compile(r"\b(?:quot(?:e[ds]?|ation|ing)|pric(?:e[ds]?|ing)|revenues?|estimat(?:e[ds]?|ion|ing)|costs?|unit\s+rate)\b|报价|单价|定价|收入|估算|预计|成本", re.I)
_CORRECT = re.compile(r"\b(?:make\s+(?:it|that)|change(?:\s+(?:it|that))?\s+to|correct(?:\s+(?:it|that))?\s+to|instead)\b"
                      r"|改成|改为|更正为|修正为", re.I)
_TOTAL = re.compile(r"\b(?:both|all|two|three|combined)\s+(?:of\s+)?(?:the\s+)?(?:(?:target|affected|selected)\s+)*(?:sales\s+)?orders?\b"
                    r"|\b(?:combined|aggregate)\s+(?:budget|total)\b|两[张笔个]?(?:目标|受影响|选中)?订单|多[张笔个]?(?:目标|受影响|选中)?订单|(?:所有|全部)(?:目标|受影响|选中)?订单|两单|合并", re.I)
_NEGATED = re.compile(r"\b(?:not|never|no|don't|do\s+not|cannot|can't|won't|cancel|revoke)\b|不是|不要|不批准|不授权|不能|取消|撤销", re.I)
_UNCERTAIN = re.compile(r"\b(?:maybe|perhaps|possibly|might|could|whether)\b|也许|或许|可能|大概|能否|是否|[?？]", re.I)
_CONDITION = re.compile(r"\b(?:if|unless|provided\s+that|on\s+condition)\b|如果|除非|只要|前提|条件是", re.I)
_POLICY_FIELD = re.compile(r"\b(?:margin|deadline|delivery\s+date|anchor\s+date|workcenter|supplier|product|SKU)\b"
                           r"|毛利|利润率|交期|交货日期|锚定日期|工作中心|供应商|产品|商品", re.I)
_CHANGE = re.compile(r"\b(?:change|lower|reduce|raise|increase|move|replace|switch|remove|drop|add|ignore|execute|apply)\b"
                     r"|改|降|提高|延[后迟]|换|删除|移除|新增|增加|忽略|执行", re.I)
_CURRENCIES = {"USD", "EUR", "GBP", "CNY", "JPY", "CAD", "AUD", "CHF", "HKD", "SGD", "NZD", "INR"}


class ClarificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise ClarificationError(code)


def _amount(value):
    _require(type(value) in (str, int, float), "invalid_budget_amount")
    if isinstance(value, str):
        _require(re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", value.strip()),
                 "invalid_budget_amount")
    try:
        amount = Decimal(str(value).replace(",", ""))
        _require(amount.is_finite() and 0 < amount <= Decimal("1000000000000")
                 and amount == amount.quantize(Decimal("0.01")), "invalid_budget_amount")
    except InvalidOperation:
        raise ClarificationError("invalid_budget_amount") from None
    return format(amount.quantize(Decimal("0.01")), "f")


def _input(ticket, text, currency):
    _require(isinstance(ticket, dict) and isinstance(ticket.get("order_id"), str)
             and re.fullmatch(r"[1-9][0-9]{0,15}", ticket["order_id"])
             and isinstance(ticket.get("order_name"), str) and 0 < len(ticket["order_name"]) <= 512
             and ticket.get("kind", "set_missing_order_budget") == "set_missing_order_budget"
             and ticket.get("status", "open") == "open", "invalid_clarification_ticket")
    _require(isinstance(text, str) and text.strip() and len(text.encode("utf-8")) <= 8000,
             "invalid_reply")
    _require(isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency), "invalid_currency")


def _mask_orders(ticket, text):
    """Return text with explicit identifiers removed, and any wrong objects."""
    target, name = ticket["order_id"], ticket["order_name"]
    # Explicit typed numeric IDs and business names are distinct from a cap
    # whose amount happens to equal the ID. Never strip an untyped number.
    patterns = [r"\b(?:sales\s+order|SO)\s+(?:id\s*[:=]?\s*|#\s*)?([1-9][0-9]*)\b",
                r"销售订单\s*(?:ID\s*[:=：]?\s*|编号\s*[:=：]?\s*)?([1-9][0-9]*)",
                r"(?<![A-Za-z0-9_-])(SO[-_][A-Za-z0-9_.-]+)(?![A-Za-z0-9_-])",
                r"\b(?:sales\s+order|SO)\s+(S[0-9]+)\b",
                r"销售订单\s*(S[0-9]+)(?![A-Za-z0-9_-])"]
    # Odoo business names use S00001. Match this finite family only when the
    # ticket itself establishes that naming family; arbitrary SKUs are not IDs.
    if re.fullmatch(r"S[0-9]+", name, re.I):
        patterns.append(r"(?<![A-Za-z0-9_-])(S[0-9]+)(?![A-Za-z0-9_-])")
    wrong = []
    result = text
    name_pattern = r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])"
    # Permit a matching parenthetical ID only directly after the business name.
    annotation = r"\s*[（(]\s*(?:id|编号)\s*[:=：]?\s*([1-9][0-9]*)\s*[）)]"
    for match in re.finditer(name_pattern + annotation, result, re.I):
        if match[1] != target:
            wrong.append(match[1])
    result = re.sub(name_pattern + annotation, " ", result, flags=re.I)
    result = re.sub(name_pattern, " ", result, flags=re.I)
    for pattern in patterns:
        def replace(match):
            value = match[1].rstrip(".")
            if re.search(r"\b(?:SKU|product|PO|purchase\s+order)\s*(?:(?:code|id)\s*[:=]?\s*)?$"
                         r"|(?:商品|产品|采购单)(?:编号)?\s*$", result[:match.start()], re.I):
                return match[0]
            if value != target and value.casefold() != name.casefold():
                wrong.append(value)
            return " "
        result = re.sub(pattern, replace, result, flags=re.I)
    return result, wrong


def _foreign_currency(text, currency):
    codes = {word.upper() for word in re.findall(r"\b[A-Za-z]{3}\b", text)} & _CURRENCIES
    if codes - {currency}:
        return True
    for pattern, allowed in [(r"€|\beuros?\b", {"EUR"}), (r"£|\bpounds?\b", {"GBP"}),
                             (r"人民币|(?<![日欧美])元|\byuan\b", {"CNY"}), (r"日元|\byen\b", {"JPY"}),
                             (r"欧元", {"EUR"}),
                             (r"美元|美金|US\$", {"USD"})]:
        if re.search(pattern, text, re.I) and currency not in allowed:
            return True
    if "$" in text and currency not in {"USD", "CAD", "AUD", "HKD", "SGD", "NZD"}:
        return True
    for before, after in re.findall(r"\b([A-Z]{3})\s*[0-9]|[0-9]\s*([A-Z]{3})\b", text):
        if (before or after) != currency:
            return True
    return False


def _cancel(text):
    return bool(re.fullmatch(r"\s*(?:please\s+)?(?:cancel|abort|stop)(?:\s+(?:this|the))?"
                             r"(?:\s+(?:case|request|investigation|process|task))?[.!。！\s]*"
                             r"|\s*(?:请)?(?:取消|终止)(?:本次|这次|这个)?(?:任务|调查|流程|请求)?[。！\s]*",
                             text, re.I))


def _pause(text):
    for match in re.finditer(r"\b(?:pause|wait|hold\s+off|not\s+ready|no\s+decision|decide\s+later)\b"
                            r"|暂停|先等等|稍后|暂不|不作决定|不做决定|先不决定", text, re.I):
        if not re.search(r"\b(?:not|never|don't)\s+$|不要\s*$|别\s*$", text[:match.start()], re.I):
            return True
    return False


def _cancel_clause(text):
    return any(_cancel(clause) for clause in re.split(r"[;；。\n,，]|\.(?=\s|$)", text))


def _outside(text):
    if _CONDITION.search(text):
        return True
    for clause in re.split(r"[;；。\n,，]|\.(?=\s|$)", text):
        if _POLICY_FIELD.search(clause) and _CHANGE.search(clause):
            return True
        if (re.search(r"\b(?:margin|deadline|delivery\s+date|anchor\s+date|workcenter)\b|毛利|利润率|交期|交货日期|锚定日期|工作中心", clause, re.I)
                and _NUMBER.search(clause) and not re.search(r"\b(?:unchanged|same|remains?|stays?)\b|照旧|不变|保持", clause, re.I)):
            return True
        if re.search(r"\b(?:remove|drop|add|exclude)\b.*\border\b|(?:删|移除|增加|新增).*(?:订单|范围)", clause, re.I):
            return True
        if re.search(r"\b(?:execute|apply|approve\s+the\s+plan)\b|执行方案|执行计划|直接执行", clause, re.I):
            return True
    return False


def _combined(text):
    return bool(_TOTAL.search(text) or len(set(re.findall(r"\bSO[-_][A-Za-z0-9_.-]+", text, re.I))) > 1
                and re.search(r"\b(?:total|combined|together)\b|合计|总共|加起来", text, re.I))


def _budget_denied(text):
    """Reject current denial/retraction across clauses, without borrowing markers.

    This is a conservative finite gate, not general negation resolution. A
    rejected old numeric value ("not 800; make it 900") is not an authorization
    denial. Unsupported contradictory authorization statements need clarification.
    """
    # Denial of a quotation alone is not denial of a later independent budget.
    clauses = re.split(r"[;；。\n，]|,(?![0-9]{3}\b)|\.(?=\s|$)|\b(?:but|and)\b|但是|但", text, flags=re.I)
    relevant = []
    prior_fact = False
    for clause in clauses:
        budget_subject = re.search(r"\b(?:budget|cap|authorization|authorisation)\b|预算|上限|授权", clause, re.I)
        if _FACT.search(clause) and not budget_subject:
            prior_fact = True
            continue
        if prior_fact and re.fullmatch(r"\s*not\s+(?:my|an?)\s+authori[sz]ation\s*", clause, re.I):
            continue
        prior_fact = False
        relevant.append(clause)
    text = "; ".join(relevant)
    return bool(re.search(
        r"\b(?:not|never|no\s+longer|haven't|hasn't|didn't|don't|cannot|can't|won't)\s+"
        r"(?:(?:yet|actually|currently|ever)\s+)?(?:authori[sz]\w*|approv\w*|confirm\w*|allocat\w*)\b"
        r"|\b(?:no|without)\s+(?:budget|authorization|authorisation|approval)\b"
        r"|\bnot\s+(?:(?:my|the|an?|any|your)\s+)?(?:budget|cap|authorization|authorisation)\b"
        r"|\b(?:retract(?:ed|s|ing)?|withdraw(?:n|s|ing)?|revok(?:e[ds]?|ing)|rescind(?:ed|s|ing)?|cancel(?:led|ed|s|ling)?)\s+"
        r"(?:(?:that|this|the|my|its|budget|authorization|authorisation|approval)\b|it\b)"
        r"|\b(?:budget|authorization|authorisation|approval)\s+(?:is\s+|has\s+been\s+)?(?:revoked|withdrawn|retracted|rescinded|cancelled)\b"
        r"|(?:尚未|没有|并未|未曾|未|不予|不|不能|暂不)(?:正式|明确|实际)?(?:授权|批准|确认)"
        r"|(?:没有|尚无|无)(?:任何)?预算|(?:撤回|撤销|取消)(?:该|此|这个|这项|之前的)?(?:预算|授权|批准)"
        r"|(?:预算|授权|批准)(?:已|已经|现已|予以)?(?:撤回|撤销|取消|作废|被拒绝|被否决)"
        r"|\b(?:budget|cap|authorization|authorisation|approval)\b[^;；。\n]*\b(?:rejected|denied|unapproved|unconfirmed)\b"
        r"|不是(?:我的|实际的|授权的)?(?:预算|上限)", text, re.I))


def _amount_candidates(text):
    """Amounts bound to a positive local budget/correction or a bare answer.

    A budget keyword elsewhere can never turn an untyped number or a price
    fact into an authorization. Mixed factual clauses stay conservative; users
    can state their budget in a separate clause. No history supplies an amount.
    """
    candidates, unclassified, facts = set(), set(), False
    currency_names = "|".join(sorted(_CURRENCIES))
    text = re.sub(r"\b(?:" + currency_names + r")(?=[0-9])|(?<=[0-9])(?:" + currency_names + r")\b", " ", text, flags=re.I)
    bare_answer = _numeric_answer(text)
    # Do not split decimal points or thousands-group commas. Each clause needs
    # its own semantic evidence; unclassified second amounts cannot disappear.
    clauses = re.split(r"[;；。\n，]|,(?![0-9]{3}\b)|\.(?=\s|$)|\b(?:but|and)\b|但是|但|然而", text, flags=re.I)
    for clause in clauses:
        if not clause.strip():
            continue
        fact = bool(_FACT.search(clause))
        facts |= fact
        if fact:
            continue
        correction = re.search(r"\b(?:change|correct)\s+(?:[0-9,.]+)\s+to\s+([0-9,.]+)"
                               r"|(?:从)[0-9,.]+(?:改成|改为)([0-9,.]+)"
                               r"|([0-9,.]+)\s+instead\s+of\s+[0-9,.]+"
                               r"|不是[0-9,.]+(?:而)?是([0-9,.]+)", clause, re.I)
        negation_text = re.sub(r"\bno\s+more\s+than\b", "up to", clause, flags=re.I)
        if _NEGATED.search(negation_text) and not re.search(r"不是[0-9,.]+(?:而)?是[0-9,.]+", clause):
            continue
        if correction:
            try:
                candidates.add(_amount(next(group for group in correction.groups() if group)))
            except ClarificationError:
                pass
            continue
        locally_bound = bool(bare_answer or _BUDGET.search(clause) or _CORRECT.search(clause))
        for match in _NUMBER.finditer(clause):
            if (match.start() and clause[match.start() - 1] in "-+"
                    or clause[match.end():].lstrip().startswith("%")):
                continue
            try:
                value = _amount(match.group())
            except ClarificationError:
                continue
            (candidates if locally_bound else unclassified).add(value)
    # Unclassified numbers may block an otherwise clear amount, but never prove
    # authorization themselves. This deliberately prefers clarification when a
    # second amount cannot be explained as an explicit fact, identity or old value.
    return (candidates | unclassified if candidates else set()), facts


def _numeric_answer(text):
    currency_names = "|".join(sorted(_CURRENCIES))
    cleaned = re.sub(r"\b(?:" + currency_names + r")(?=\b|[0-9])|(?<=[0-9])(?:" + currency_names
                     + r")\b|美元|美金|欧元|日元|人民币|元|[$€£¥]", " ", text, flags=re.I)
    cleaned = cleaned.strip(" \t\n:：。.!！")
    return bool(re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?", cleaned))


def validate_proposal(ticket, text, proposal, currency):
    """Normalize a shared schema and reject explicit unsafe interpretation.

    A safe ambiguous/price/out-of-scope interpretation need not match the fixed
    interpreter. Budget proposals require a literal current-reply amount and no
    explicit wrong identity, combined allocation or condition. No history alone
    can supply an amount. Human confirmation remains a separate gate.
    """
    _input(ticket, text, currency)
    _require(isinstance(proposal, dict) and set(proposal) <= {"kind", "order_id", "amount", "currency", "question"}
             and proposal.get("kind") in KINDS, "invalid_reply_proposal")
    result = copy.deepcopy(proposal)
    question = result.get("question", "")
    _require(isinstance(question, str) and len(question) <= 1200, "invalid_reply_proposal")
    if result["kind"] != "budget_authorization_proposal":
        _require(not {"order_id", "amount", "currency"} & set(result), "invalid_reply_proposal")
        if result["kind"] == "cancel":
            _require(_cancel(text), "cancellation_unproven")
        if result["kind"] == "pause":
            _require(_pause(text), "pause_unproven")
        return result
    _require({"order_id", "amount", "currency"} <= set(result), "invalid_reply_proposal")
    _require(result["order_id"] == ticket["order_id"], "wrong_budget_order")
    _require(result["currency"] == currency and not _foreign_currency(text, currency), "wrong_budget_currency")
    value = _amount(result["amount"])
    masked, wrong = _mask_orders(ticket, text)
    _require(not wrong, "wrong_budget_order")
    _require(not _outside(text), "out_of_scope_reply")
    _require(not _combined(text), "combined_budget_ambiguous")
    _require(not _cancel_clause(text) and not _pause(text) and not _UNCERTAIN.search(text)
             and not _budget_denied(masked), "budget_decision_unproven")
    _require(bool(_BUDGET.search(masked) or _CORRECT.search(masked) or _numeric_answer(masked)
                  or re.search(r"\b(?:change|correct)\s+[0-9]|不是[0-9]|从[0-9]", masked, re.I)),
             "budget_decision_unproven")
    candidates, facts = _amount_candidates(masked)
    _require(candidates == {value}, "budget_amount_unproven" if not candidates else "budget_amount_ambiguous")
    _require(not facts or bool(_BUDGET.search(masked) or _CORRECT.search(masked)), "price_is_not_budget")
    result["amount"] = value
    return result


def interpret_reply(ticket, text, currency, history=()):
    """Fixed-controller baseline. History provides context, never an old grant."""
    _input(ticket, text, currency)
    _require(isinstance(history, (list, tuple)), "invalid_reply_history")
    target = f"{ticket['order_name']} (sales order ID {ticket['order_id']})"
    question = f"What positive budget cap in {currency} do you authorize for {target} only?"
    def stop(kind, message=question):
        return {"kind": kind, "question": message}
    if _cancel(text):
        return stop("cancel", "")
    if _pause(text):
        return stop("pause", "No budget authorization has been recorded. Resume with an explicit decision when ready.")
    if _outside(text):
        return stop("out_of_scope", "This step only fills the missing order budget. Please state that decision without changing other policy or approving execution.")
    masked, wrong = _mask_orders(ticket, text)
    if _combined(text):
        return stop("ambiguous", f"Please give the budget for {target} individually; I cannot split a combined total.")
    if wrong:
        return stop("out_of_scope", f"Only the missing budget for {target} can be changed in this step.")
    if _foreign_currency(text, currency):
        return stop("ambiguous", f"This case uses {currency}; please state an authorization in that currency. No conversion is available.")
    if _budget_denied(masked):
        return stop("ambiguous", "No current budget authorization is established. " + question)
    candidates, facts = _amount_candidates(masked)
    if not candidates and facts:
        return stop("price_fact", "A quotation, revenue or cost is a fact, not spending authorization. " + question)
    if len(candidates) != 1:
        return stop("ambiguous")
    proposal = {"kind": "budget_authorization_proposal", "order_id": ticket["order_id"],
                "amount": next(iter(candidates)), "currency": currency}
    try:
        return validate_proposal(ticket, text, proposal, currency)
    except ClarificationError:
        return stop("ambiguous")
