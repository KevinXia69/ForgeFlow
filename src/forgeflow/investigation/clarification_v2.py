"""Version 2 finite reply semantics and host-rendered questions.

Source spans establish provenance, never authority. Every action is checked
against the complete current reply; a later authenticated confirmation alone
can turn a budget candidate into policy. Version 1 remains independently read.
"""
from __future__ import annotations

import copy
import re

from forgeflow.investigation import clarification as v1
from forgeflow.recovery.storage import digest

ClarificationError = v1.ClarificationError
VERSION = 2
NEEDS = ("current_authorization", "explicit_amount", "single_order_amount",
         "unconditional_single_field_decision")
KINDS = ("budget_authorization_proposal", "ask_followup", "withdraw_budget", "pause", "cancel")
PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "kind", "source_spans"],
    "description": "Version 2 proposals only. Budget requires order_id, amount, currency. ask_followup requires need. Other actions omit all budget fields and need. Cite visible reply IDs using Unicode code-point offsets, never UTF-16 indices. question/explanation are untrusted model text; the host renders the actual question.",
    "properties": {
        "schema_version": {"type": "integer", "const": VERSION},
        "kind": {"type": "string", "enum": list(KINDS)},
        "need": {"type": "string", "enum": list(NEEDS)},
        "order_id": {"type": "string", "pattern": "^[1-9][0-9]*$"},
        "amount": {"type": "string"}, "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "question": {"type": "string", "maxLength": 1200},
        "explanation": {"type": "string", "maxLength": 1600},
        "source_spans": {"type": "array", "minItems": 1, "maxItems": 16, "items": {
            "type": "object", "additionalProperties": False, "required": ["reply_id", "start", "end"],
            "properties": {"reply_id": {"type": "string", "minLength": 1, "maxLength": 128},
                           "start": {"type": "integer", "minimum": 0},
                           "end": {"type": "integer", "minimum": 1}}}},
    },
}


def require(condition, code):
    if not condition:
        raise ClarificationError(code)


def visible_manifest(replies, current_reply_id):
    """Canonical, ordered, host-visible input; preserve text and legacy digest."""
    require(isinstance(replies, (list, tuple)) and 0 < len(replies) <= 128, "invalid_reply_history")
    rows, ids = [], set()
    for row in replies:
        require(isinstance(row, dict), "invalid_reply_history")
        identity, text = row.get("reply_id"), row.get("text")
        require(isinstance(identity, str) and 0 < len(identity) <= 128 and identity not in ids,
                "invalid_visible_reply_id")
        require(isinstance(text, str) and text.strip() and len(text.encode("utf-8")) <= 8000,
                "invalid_reply")
        expected = digest(text)
        hashes = [row[key] for key in ("sha256", "reply_sha256") if key in row]
        require(bool(hashes) and all(value == expected for value in hashes), "reply_hash_mismatch")
        rows.append({"reply_id": identity, "reply_sha256": expected, "text": text})
        ids.add(identity)
    require(current_reply_id == rows[-1]["reply_id"], "current_reply_not_latest_visible")
    return {"current_reply_id": current_reply_id, "replies": rows}


_HISTORY = re.compile(r"\b(?:yesterday|previously|formerly|historically|archived|copied\s+text|last\s+(?:year|week|month|winter|summer|spring|autumn|fall)|used\s+to|(?:prior|previous)\s+(?:repair|budget|cap|authorization))\b|\bearlier\s+(?:i|we)\s+(?:said|wrote|approved|authorized|authorised)\b|\b(?:budget|cap|authorization)\s+(?:was|had\s+been)\b|\bi\s+had\s+(?:approved|authorized|authorised)\b|昨天|以前|之前的预算|过去|历史|存档|复制的文本", re.I)
_EXAMPLE = re.compile(r"\b(?:for\s+example|example|hypothetically|suppose|illustration|pretend|for\s+training\s+purposes)\b|例如|举例|假设|示例", re.I)
_CURRENT = re.compile(r"\b(?:now|currently|today|instead)\b|现在|当前|今天|改成|改为|更正", re.I)
_OLD = re.compile(r"\b(?:old|earlier|previous|prior|before|former)\b|旧|之前|以前|原先|先前", re.I)
_WITHDRAW = re.compile(
    r"\s*(?:(?:please|i|we)\s+)?(?:withdraw|retract|revoke|rescind|cancel|disregard|remove)\s+"
    r"(?:(?:the|that|this|my|our|any|old|earlier|previous|prior|former|preceding|pending|unconfirmed|tentative)\s+)*"
    r"(?:[0-9,.]+\s*(?:USD\s*)?)?(?:budget(?:\s+(?:candidate|decision|cap|authorization|authorisation))?"
    r"|cap|authorization|authorisation|decision|candidate)"
    r"(?:\s+of\s+[0-9,.]+\s*(?:USD)?)?"
    r"(?:\s+from\s+(?:my|the)\s+(?:previous|prior|earlier|last)\s+reply)?"
    r"(?:\s+now)?\s*[.!！。]*\s*"
    r"|\s*(?:撤回|撤销|取消)(?:刚才|之前|以前|原先|该|这个|旧)?(?:的)?(?:预算|授权|决定)(?:决定|候选)?[。！\s]*"
    r"|\s*(?:撤回|撤销)(?:之前|以前|原先|旧)(?:的)?[0-9,.]+(?:美元|USD)?(?:预算|授权|决定)?[。！\s]*", re.I)
_FALSE = re.compile(r"\b(?:that|this|above|previous)\s+(?:statement|sentence|claim|decision|authorization|authorisation|budget)\s+(?:(?:is|was)\s+(?:false|incorrect|invalid|untrue)|does\s+not\s+stand|is\s+not\s+valid)\b|\b(?:this|that)\s+is\s+(?:a\s+)?(?:false|incorrect|invalid|untrue)(?:\s+(?:statement|claim|authorization))?\b|(?:上面|上述|这|那|刚才的|之前的)(?:一句|句|句话|个决定|项决定|个陈述|授权)(?:话)?(?:是)?(?:不成立|不正确|无效|假的|不算数)|不是真实授权", re.I)
_CONTROL = r"^\s*(?:(?:please|i\s+(?:want|would\s+like)\s+to)\s+)?{verbs}(?:\s+(?:this|the|my))?(?:\s+(?:entire|recovery))?(?:\s+(?:case|task|investigation|process|request))?(?:\s+now)?\s*[.!！。]*\s*$"
_PAUSE = re.compile(_CONTROL.format(verbs=r"(?:pause|wait|hold\s+off)") + r"|^\s*(?:请)?(?:暂停|先等等)(?:本次|这次|这个)?(?:任务|调查|流程|请求)?[。！\s]*$", re.I)
_CANCEL = re.compile(_CONTROL.format(verbs=r"(?:cancel|abort|stop)"), re.I)
_END_CASE = re.compile(
    r"\s*(?:(?:please|i\s+(?:want\s+to|would\s+like\s+to))\s+)?(?:end|terminate|close)\s+"
    r"(?:(?:this|the|my|our)\s+)?(?:entire\s+)?(?:recovery\s+)?(?:case|task|investigation|process|request)"
    r"(?:\s+now)?\s*[.!！。]*\s*", re.I)
_CONTROL_REFERENCE = re.compile(
    r"\s*(?:please\s+)?(?:cancel|abort|stop|end|terminate|close)\s+it"
    r"(?:\s+(?:entirely|completely))?(?:\s+now)?\s*[.!！。]*\s*", re.I)
_CURRENT_ANSWER = re.compile(
    r"\s*(?:my|our)\s+(?:(?:new|current|final)\s+)?(?:answer|decision)"
    r"(?:\s+(?:now|today|currently))?(?:\s+for(?:\s+(?:the\s+)?(?:open\s+)?(?:budget\s+)?ticket)?)?"
    r"\s*(?:is|:|=)\s*(.+)\s*", re.I)
_AMOUNT_LITERAL = re.compile(r"(?<![A-Za-z0-9_.-])(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?(?![A-Za-z0-9_]|\.[0-9])")
_NEGATIVE_CONTROL = re.compile(r"\b(?:not|never|don't|do\s+not)\s+(?:cancel(?:led|ed)?|abort(?:ed)?|stop(?:ped)?|end(?:ed)?|terminate(?:d)?|close(?:d)?|pause(?:d)?|wait(?:ed)?|withdraw(?:n)?|retract(?:ed)?|revok(?:e|ed)|rescind(?:ed)?|remove(?:d)?|disregard(?:ed)?)\b|不要(?:取消|终止|暂停|撤回|撤销)|别(?:取消|终止|暂停|撤回|撤销)", re.I)
_CONDITIONAL = re.compile(r"\b(?:when|once|subject\s+to|contingent\s+on)\b|仅当|只有.*才|等.*才", re.I)
_UNCERTAIN_DECISION = re.compile(r"\b(?:not\s+sure|undecided|pending\s+(?:final\s+)?approval|provisional|tentative\s+(?:answer|decision|authorization))\b|尚待批准|还不确定|暂定答案", re.I)
_UNSCOPED_CHANGE = re.compile(r"\b(?:set|update|edit|alter|reschedule|rescope|replace|switch|remove|drop|add)\b", re.I)
_QUOTED = re.compile(r'''"[^"\n]*"|(?<!\w)'[^'\n]+'(?!\w)|“[^”]*”|「[^」]*」|『[^』]*』''')
_STATEMENT_RETRACTION = re.compile(
    r"\b(?:that|this|the\s+previous)\s+(?:(?:answer|instruction|statement|decision|authorization)\s+)?"
    r"(?:(?:is|was)\s+(?:only\s+)?(?:an?\s+)?example|(?:is|was)\s+not\s+valid|(?:is|was)\s+withdrawn)\b"
    r"|\bi\s+take\s+(?:it|that)\s+back\b"
    r"|\bi\s+(?:have\s+)?changed\s+my\s+mind\b"
    r"|\b(?:ignore|disregard|do\s+not\s+use)\s+(?:that|this|the\s+previous)\s+(?:answer|instruction|statement)\b"
    r"|\b(?:this|that)\s+is\s+not\s+my\s+(?:actual\s+)?(?:answer|instruction|decision)\b", re.I)
_EXISTING_BUDGET = re.compile(
    r"(SO[-_][A-Za-z0-9_.-]+|S[0-9]+)\s+already\s+has\s+"
    r"(?:(?:its\s+own|an?|the)\s+)?(?:approved|existing|current)\s+(?:budget|cap)(?:\s+cap)?", re.I)
_COORDINATED_PRESERVE = re.compile(
    r"(?:keep|leave|preserve|retain)\s+(?:SO[-_][A-Za-z0-9_.-]+|S[0-9]+)"
    r"(?:\s+and\s+(?:SO[-_][A-Za-z0-9_.-]+|S[0-9]+))+\s+"
    r"(?:budgets|caps|limits)\s+(?:unchanged|unmodified)", re.I)
_ORDER_REFERENCE = r"(?:SO[-_][A-Za-z0-9_.-]+|S[0-9]+)"
_ORDER_GROUP = (rf"(?:{_ORDER_REFERENCE}(?:\s+and\s+{_ORDER_REFERENCE})*"
                r"|(?:(?:the|that|this|those|these)\s+)?(?:(?:other|remaining)\s+)?"
                r"(?:(?:two|three|all)\s+)?orders?)")
_BUDGET_NOUN = r"(?:budgets?|caps?|limits?)"
_PRESERVED_OBJECT = (rf"(?:{_ORDER_GROUP}(?:(?:['’]s?)?\s+(?:(?:existing|current)\s+)?{_BUDGET_NOUN})?"
                     r"|(?:(?:the|that|this|its|my|our)\s+)?(?:(?:existing|current)\s+)?"
                     rf"(?:{_BUDGET_NOUN}|spending\s+limit|deadline|delivery\s+date|anchor\s+date|margin|workcenter|supplier|product|SKU))")
_PRESERVE_ACTION = r"(?:please\s+)?(?:preserve|keep|leave|retain)\s+"


def _clauses(text):
    # Never split a decimal or thousands separator; original offsets are never
    # derived from these semantic working strings.
    for sentence in re.split(r"[;；。\n]|\.(?=\s|$)", text):
        if _COORDINATED_PRESERVE.fullmatch(sentence.strip()):
            yield sentence.strip(), None
            continue
        inherited = None
        for part in re.split(r"，|,(?![0-9]{3}\b)|\b(?:but|and)\b|但是|然而", sentence, flags=re.I):
            part = part.strip()
            if not part:
                continue
            if _EXAMPLE.search(part) and not _STATEMENT_RETRACTION.match(part):
                inherited = "example"
            elif _HISTORY.search(part) and not _WITHDRAW.fullmatch(part):
                # An inner "now" in a reported historical statement does not
                # overrule the enclosing time scope. A separate current clause
                # can still establish a new decision.
                inherited = "history"
            # A comma/"and" inside a reported example or historical statement
            # does not end that statement. In particular an inner "now" is not
            # a new current authorization. A new sentence/semicolon resets it.
            yield part, inherited


def _unchanged_background(clause):
    """Recognize explicit preservation, never merely the word keep/same.

    A number after "keep ... at" is a new decision unless the text explicitly
    calls it the existing/current value. Clauses that request a mutation cannot
    be hidden by a later unchanged word.
    """
    # The whole clause and its object must be known. A trailing "unchanged"
    # cannot hide "assign supplier ... while keeping deadline unchanged", nor
    # can an arbitrary prefix ending in "existing budget" become background.
    if re.fullmatch(rf"(?:{_PRESERVE_ACTION})?{_PRESERVED_OBJECT}\s+"
                    r"(?:(?:remain[s]?|stay[s]?)\s+)?(?:unchanged|unmodified)", clause, re.I):
        return True
    if re.fullmatch(rf"(?:保持|保留)?(?:(?:其他|其余)(?:两|三|所有)?(?:张|笔|个)?(?:订单|单)|{_ORDER_REFERENCE}"
                    r"|(?:既有|原有)?(?:交期|交货日期|毛利|利润率|供应商|工作中心))"
                    r"(?:的)?(?:预算|上限)?(?:保持)?(?:不变|照旧)", clause, re.I):
        return True
    # "Keep the case open" and "keep investigating the same case" contain no
    # policy or budget decision, but must not swallow a command about a PO.
    if re.fullmatch(r"(?:please\s+)?keep\s+(?:(?:this|the|same)\s+)?(?:recovery\s+)?case\s+open"
                    r"|keep\s+investigating\s+(?:this|the(?:\s+same)?|same)\s+case", clause, re.I):
        return True
    if (re.fullmatch(rf"{_PRESERVE_ACTION}{_PRESERVED_OBJECT}", clause, re.I)
            and re.search(r"\b(?:existing|current)\s+" + _BUDGET_NOUN, clause, re.I)):
        return True
    return bool(re.fullmatch(
        rf"{_PRESERVE_ACTION}{_ORDER_GROUP}\s+at\s+(?:(?:its|their|the)\s+)?"
        rf"(?:existing|current)\s+{_BUDGET_NOUN}(?:\s+(?:of|at)\s+[0-9,.]+\s*(?:USD)?)?",
        clause, re.I))


def _existing_budget_background(ticket, clause, preserved, description_count):
    """Only an amount-free existing-budget statement with a matching preserve.

    We do not read the other order's cap, infer a new cap, or treat a bare other
    order mention as authorization. A singular "that other order" can refer to
    exactly one such description in this current reply.
    """
    match = _EXISTING_BUDGET.fullmatch(clause)
    if match is None or match[1].casefold() == ticket["order_name"].casefold():
        return False
    name = match[1].casefold()
    for background in preserved:
        if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", background, re.I):
            return True
        if re.search(r"\bother\s+(?:(?:two|three|all|remaining)\s+)?orders\b", background, re.I):
            return True
        if description_count == 1 and re.search(r"\b(?:that|the)\s+other\s+order\b", background, re.I):
            return True
    return False


def _preserves_current_budget(ticket, clause):
    """A current-order/no-object preserve cannot coexist with a new cap."""
    name = r"(?<![A-Za-z0-9_-])" + re.escape(ticket["order_name"]) + r"(?![A-Za-z0-9_-])"
    if re.search(name, clause, re.I):
        return True
    _, other = v1._mask_orders(ticket, clause)
    if other or re.search(r"\b(?:other|remaining)\s+(?:(?:two|three|all)\s+)?orders?\b"
                          r"|(?:其他|其余)(?:两|三|所有)?(?:张|笔|个)?(?:订单|单)", clause, re.I):
        return False
    return bool(v1._BUDGET.search(clause) or re.search(r"\borders?\b", clause, re.I))


def _analysis(ticket, text, currency):
    """A finite scope check shared by all methods, with no oracle/history grant."""
    v1._input(ticket, text, currency)
    candidates, controls, denied_controls, relevant = set(), set(), set(), []
    denied, withdrew, old_withdrawn, new_current = False, False, False, False
    preserved_current_budget = False
    facts, historical, wrong, total, outside = False, bool(_QUOTED.search(text)), False, False, False
    # Quoted text is evidence of a statement, not a fresh decision by its reader.
    # Only the semantic working copy is masked; source text/offsets are intact.
    # Without a bounded reporting scope, masking could hide a decisive identity
    # such as `authorize 800 for "SO-31"`. Treat that whole reply as uncertain.
    for quoted in _QUOTED.finditer(text):
        prefix = re.split(r"[;；。\n]|\.(?=\s|$)", text[:quoted.start()])[-1]
        if not (_HISTORY.search(prefix) or _EXAMPLE.search(prefix)):
            outside = True
    working = _QUOTED.sub(lambda match: " " * len(match.group()), text)
    if re.search(r"""["“”「」『』]|(?<!\w)'""", working):
        # Incomplete/unsupported quotation cannot establish a current decision.
        outside = True
    clauses = list(_clauses(working))
    preserved = [clause for clause, inherited in clauses
                 if not inherited and not (v1._CONDITION.search(clause) or _CONDITIONAL.search(clause))
                 and not v1._budget_denied(clause) and _unchanged_background(clause)]
    descriptions = sum(bool(_EXISTING_BUDGET.fullmatch(clause)) for clause, inherited in clauses if not inherited)
    referent = None
    for clause, inherited in clauses:
        # A current retraction refers back to the decision just expressed. It
        # must not become harmless history merely because it says "example".
        if _STATEMENT_RETRACTION.match(clause) and not inherited:
            outside = True
            referent = None
            continue
        if inherited:
            historical = True
            referent = None
            continue
        # Conditions do not become immediate control commands or budgets.
        if (v1._CONDITION.search(clause) or _CONDITIONAL.search(clause)
                or v1._UNCERTAIN.search(clause) or _UNCERTAIN_DECISION.search(clause)):
            outside = True
            referent = None
            continue
        unchanged = _unchanged_background(clause) and not v1._budget_denied(clause)
        # Scope checks precede negative-control handling: a denied cancel does
        # not conceal an additional supplier/deadline change in that clause.
        if not unchanged:
            outside |= v1._outside(clause) or v1._foreign_currency(clause, currency)
            if v1._POLICY_FIELD.search(clause) and not v1._FACT.search(clause):
                outside = True
            # Unbound change commands such as "replace it" cannot silently
            # disappear after a separate unchanged-policy clause.
            if _UNSCOPED_CHANGE.search(clause) and not v1._BUDGET.search(clause):
                outside = True
        negatives = _NEGATIVE_CONTROL.findall(clause)
        if negatives:
            for negative in negatives:
                if re.search(r"withdraw|retract|revok|rescind|remove|disregard|撤回|撤销", negative, re.I):
                    denied_controls.add("withdraw_budget")
                else:
                    denied_controls.add("pause" if re.search(r"pause|wait|暂停", negative, re.I) else "cancel")
            referent = None
            continue
        if _FALSE.search(clause):
            denied = True
            referent = None
            continue
        # Only explicitly unchanged context is excluded from the current
        # decision. An amount-free fact about a different order additionally
        # requires that same order to be preserved in this current reply.
        if unchanged:
            preserved_current_budget |= _preserves_current_budget(ticket, clause)
            if re.search(r"\bcase\s+open$|\bkeep\s+investigating\b", clause, re.I):
                denied_controls.add("cancel")
            referent = None
            continue
        if _existing_budget_background(ticket, clause, preserved, descriptions):
            referent = None
            continue
        masked, other = v1._mask_orders(ticket, clause)
        wrong |= bool(other)
        total |= v1._combined(clause)
        if _WITHDRAW.fullmatch(clause):
            if _OLD.search(clause):
                old_withdrawn = True
            else:
                withdrew = True
            referent = "budget"
            continue
        if _CONTROL_REFERENCE.fullmatch(clause):
            if referent == "case":
                controls.add("cancel")
            elif referent == "budget" and re.match(r"(?:please\s+)?cancel\b", clause, re.I):
                # The immediately preceding current budget withdrawal cannot
                # turn into closure of the entire case through a pronoun.
                withdrew = True
            else:
                outside = True
            continue
        if v1._cancel(clause) or _CANCEL.fullmatch(clause) or _END_CASE.fullmatch(clause):
            controls.add("cancel")
            referent = "case" if re.search(r"\b(?:case|task|investigation|process|request)\b|任务|调查|流程|请求", clause, re.I) else None
            continue
        if _PAUSE.fullmatch(clause):
            controls.add("pause")
            referent = None
            continue
        referent = None
        # A preserve/keep verb does not neutralize an explicit denied grant.
        if v1._budget_denied(clause):
            denied = True
            continue
        denied |= v1._budget_denied(masked)
        # The current ticket supplies the field for a direct named-order answer
        # such as "SO-30 为 800 USD", but not a quotation or arbitrary number.
        simple = re.sub(r"^\s*(?:为|是|is|:|：)\s*", "", masked, flags=re.I)
        if (ticket["order_name"].casefold() in clause.casefold() and simple != masked
                and v1._numeric_answer(simple)):
            masked = "budget " + simple
        answer = _CURRENT_ANSWER.fullmatch(masked)
        if answer is not None and v1._numeric_answer(answer[1]):
            # The open budget ticket supplies the field, but never the amount.
            # No previous reply or contextual amount is substituted here.
            masked = "budget " + answer[1]
            new_current = True
        new_current |= bool(_CURRENT.search(clause) and (v1._BUDGET.search(masked) or v1._CORRECT.search(masked)))
        values, fact = v1._amount_candidates(masked)
        candidates |= values
        facts |= fact
        relevant.append(masked)
    outside |= preserved_current_budget and bool(candidates)
    if outside or wrong or total:
        need = "single_order_amount" if (wrong or total) and not outside else "unconditional_single_field_decision"
        return {"kind": "ask_followup", "need": need}
    if (len(controls) > 1 or controls & denied_controls
            or (withdrew or old_withdrawn) and "withdraw_budget" in denied_controls
            or controls and (candidates or withdrew or old_withdrawn or denied)):
        return {"kind": "ask_followup", "need": "unconditional_single_field_decision"}
    if controls:
        return {"kind": next(iter(controls))}
    if denied or withdrew:
        return {"kind": "withdraw_budget"} if withdrew and not candidates else {"kind": "ask_followup", "need": "current_authorization"}
    if old_withdrawn and not new_current:
        return {"kind": "withdraw_budget"}
    # Compare across clauses too: a second unexplained amount cannot disappear.
    values, _ = v1._amount_candidates("; ".join(relevant))
    candidates |= values
    if len(candidates) == 1:
        return {"kind": "budget_authorization_proposal", "order_id": ticket["order_id"],
                "amount": next(iter(candidates)), "currency": currency}
    return {"kind": "ask_followup", "need": "current_authorization" if facts or historical or denied else "explicit_amount"}


def render_question(ticket, currency, proposal):
    """Only host facts and finite reasons influence the user-visible question."""
    target = f"{ticket['order_name']} (sales order ID {ticket['order_id']})"
    kind = proposal["kind"]
    if kind == "budget_authorization_proposal":
        return None
    if kind == "cancel":
        return "Cancellation has been requested for this case. Existing work must be reconciled before closure."
    if kind == "pause":
        return "This case is waiting for your decision. No budget has been authorized."
    if kind == "withdraw_budget":
        return f"The unconfirmed budget candidate is withdrawn. What current budget cap in {currency} do you authorize for {target} only?"
    prefix = {
        "current_authorization": "A quote, historical value or denied statement is not a current budget decision. ",
        "explicit_amount": "A single explicit amount is required. ",
        "single_order_amount": "A combined total or another order's amount cannot fill this budget. ",
        "unconditional_single_field_decision": "This step can only fill the missing budget; conditional or conflicting decisions remain unconfirmed. ",
    }[proposal["need"]]
    return prefix + f"What current budget cap in {currency} do you authorize for {target} only?"


def validate_proposal(ticket, reply, proposal, currency, *, visible_replies, current_reply_id):
    manifest = visible_manifest(visible_replies, current_reply_id)
    current = manifest["replies"][-1]
    require(reply == current["text"], "current_reply_text_mismatch")
    v1._input(ticket, reply, currency)
    allowed = {"schema_version", "kind", "need", "order_id", "amount", "currency", "question", "explanation", "source_spans"}
    require(isinstance(proposal, dict) and set(proposal) <= allowed
            and type(proposal.get("schema_version")) is int and proposal["schema_version"] == VERSION
            and proposal.get("kind") in KINDS, "invalid_reply_proposal")
    value = copy.deepcopy(proposal)
    for field, limit in (("question", 1200), ("explanation", 1600)):
        require(isinstance(value.get(field, ""), str) and len(value.get(field, "")) <= limit, "invalid_reply_proposal")
    spans = value.get("source_spans")
    require(isinstance(spans, list) and 0 < len(spans) <= 16, "invalid_source_spans")
    visible = {row["reply_id"]: row for row in manifest["replies"]}
    for span in spans:
        require(isinstance(span, dict) and set(span) == {"reply_id", "start", "end"}
                and isinstance(span.get("reply_id"), str) and span["reply_id"] in visible,
                "invisible_source_span")
        require(type(span["start"]) is int and type(span["end"]) is int
                and 0 <= span["start"] < span["end"] <= len(visible[span["reply_id"]]["text"]),
                "invalid_source_span_range")
    expected = _analysis(ticket, reply, currency)
    kind = value["kind"]
    if kind == "budget_authorization_proposal":
        require({"order_id", "amount", "currency"} <= set(value) and "need" not in value, "invalid_reply_proposal")
        value["amount"] = v1._amount(value["amount"])
        require(value["order_id"] == ticket["order_id"], "wrong_budget_order")
        require(value["currency"] == currency, "wrong_budget_currency")
        require(expected["kind"] == kind and all(value[key] == expected[key] for key in ("order_id", "amount", "currency")),
                "budget_decision_unproven")
        cited = [reply[span["start"]:span["end"]] for span in spans if span["reply_id"] == current_reply_id]
        amounts = set()
        for text in cited:
            for match in _AMOUNT_LITERAL.finditer(text):
                try:
                    amounts.add(v1._amount(match.group()))
                except ClarificationError:
                    pass
        require(value["amount"] in amounts, "current_amount_span_required")
    else:
        require(not {"order_id", "amount", "currency"} & set(value), "invalid_reply_proposal")
        if kind == "ask_followup":
            # Several safe questions can express the same missing decision;
            # admission is not equality with the fixed interpreter's wording or
            # preferred label. Clear current budgets/controls cannot be replaced
            # by an unnecessary question merely to make a model call pass.
            require(expected["kind"] in {"ask_followup", "withdraw_budget"}, "reply_action_unproven")
            expected_need = expected.get("need", "current_authorization")
            permitted = ({"current_authorization", "explicit_amount"}
                         if expected_need in {"current_authorization", "explicit_amount"} else {expected_need})
            require(value.get("need") in permitted, "followup_need_unproven")
        else:
            require(kind == expected["kind"], "reply_action_unproven")
            require("need" not in value and any(s["reply_id"] == current_reply_id for s in spans), "current_action_span_required")
    return value


def interpret_reply(ticket, reply, currency, *, visible_replies, current_reply_id):
    proposal = {"schema_version": VERSION, **_analysis(ticket, reply, currency),
                "source_spans": [{"reply_id": current_reply_id, "start": 0, "end": len(reply)}]}
    return validate_proposal(ticket, reply, proposal, currency,
                             visible_replies=visible_replies, current_reply_id=current_reply_id)
