"""Conservative identity binding for a single supplier cancellation event.

This is deliberately not natural-language understanding. One affirmative,
active cancellation clause must name its supplier as the subject. Only that
clause can supply purchase-order or product anchors; facts in other clauses
cannot identify what was cancelled. Unclear, passive and compound events
require clarification. No model-supplied interpretation supplies an anchor.
"""
from __future__ import annotations

import hashlib
import re

from forgeflow.investigation.sources import collect_pages


def trusted_event(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"text", "sha256"}:
        raise ValueError("investigation_event_required")
    text = value["text"]
    if (not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 16384
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != value["sha256"]):
        raise ValueError("invalid_investigation_event")
    return dict(value)


def _mentioned(text: str, value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    return bool(re.search(r"(?<![\w-])" + re.escape(value.casefold()) + r"(?![\w-])",
                          text.casefold()))


def _entities(source, entity, query):
    return collect_pages(source, "entities", {"entity": entity, "query": query})


def _supply_object(text):
    """Accept a finite supply/order noun phrase, not an incidental PO mention."""
    token = r'''(?:"[^"\n]{1,128}"|'[^'\n]{1,128}'|[A-Za-z0-9][A-Za-z0-9_.-]*)'''
    purchase = r"(?:PO[-_][A-Za-z0-9_.-]+|PO\d+|P\d+)"
    english = (r"(?:(?:a|an|the|its|their)\s+)?(?:confirmed\s+)?(?:"
               r"(?:supply(?:\s+commitment)?|commitment)"
               r"(?:\s+(?:for|of)\s+(?:(?:product|SKU)\s+)?" + token + r")?"
               r"|purchase\s+order\s+" + token
               + r"|" + purchase
               + r"|(?:product|SKU)\s+" + token + r"(?:\s+supply)?)")
    chinese = (r"了?\s*(?:其|该|这笔)?(?:已确认的|确认的)?(?:供货(?:承诺)?|供应(?:承诺)?"
               r"|采购(?:订单|单)\s*" + token
               + r"|(?:产品|商品|SKU)\s*" + token + r"(?:的)?(?:供货|供应))")
    return bool(re.fullmatch(english, text.strip(), re.I)
                or re.fullmatch(chinese, text.strip(), re.I))


def _retracted_elsewhere(parts, cancellation_index, supplier):
    """Reject explicit corrections, without assigning arbitrary narrative scope.

    This finite grammar accepts one asserted cancellation. A correction marker
    or a statement-level denial elsewhere makes that assertion unsafe; another
    named supplier continuing an unrelated supply does not.
    """
    for index in range(0, len(parts), 2):
        if index == cancellation_index:
            continue
        clause = parts[index].strip()
        if re.search(
                r"\b(?:correction|retraction|retracted|withdrawn)\b|^clarification\s*:"
                r"|\b(?:statement|notice|report|claim|message|announcement)\b"
                r".*\b(?:false|incorrect|untrue|wrong|denied|invalid)\b"
                r"|\b(?:that|this|it)\s+(?:is|was)\s+(?:false|incorrect|untrue|wrong|not\s+(?:true|correct))\b"
                r"|\b(?:statement|notice|report|claim|message|announcement)\b.*\bnot\s+(?:true|correct)\b"
                r"|更正|纠正|撤回|^澄清[:：]|(?:该|上述|此前|先前|这(?:个|条|一条)?)(?:声明|通知|消息|说法)"
                r".*(?:不实|错误|无效|不正确)", clause, re.I):
            return True
        # A following condition may be separated by any sentence punctuation.
        if index > cancellation_index and re.match(
                r"(?:but\s+)?(?:if|unless|possibly)\b|(?:但)?(?:如果|假如|除非|可能)", clause, re.I):
            return True
        continued = (r"\b(?:still|continue[sd]?\s+to|will)\s+"
                     r"(?:supply|supplies|supplying|deliver|delivers|delivering)\b|仍|继续供|继续交")
        if re.search(continued, clause, re.I):
            anchors = [supplier["fields"].get(field) for field in ("ref", "name")]
            if any(_mentioned(clause, anchor) for anchor in anchors) or re.match(
                    r"(?:it|they|the\s+supplier|the\s+vendor)\b|该供应商|其仍", clause, re.I):
                return True
    return False


def _cancellation_clause(text, supplier):
    """Recognize a small active-voice grammar, never a bag of event keywords."""
    cancellation = re.compile(r"(?<![\w-])cancel(?:led|ed|lation|s)?(?![\w-])|取消|撤销", re.I)
    # Keep decimal/product-reference periods intact. Commas are deliberate
    # boundaries: a relative clause or split subject needs clarification.
    parts = re.split(r"([,，;；。\n!?！？]|\.(?=\s|$))", text)
    clauses = []
    for index in range(0, len(parts), 2):
        clause = parts[index].strip()
        for match in cancellation.finditer(clause):
            clauses.append((clause, match, index))
    if not clauses:
        return None, [], "event_cancellation_unproven"
    if len(clauses) != 1:
        return None, [], "event_supply_ambiguous"
    clause, verb, index = clauses[0]
    ending = parts[index + 1] if index + 1 < len(parts) else ""
    if ending in {"?", "？"}:
        return None, [], "event_cancellation_unproven"
    subject = clause[:verb.start()].strip()
    # Bare imperatives and cancellation nouns do not assert an actual event.
    if verb.group().casefold() not in {"cancelled", "canceled", "cancels", "取消", "撤销"}:
        return None, [], "event_cancellation_unproven"
    anchors = []
    for field in ("ref", "name"):
        anchor = supplier["fields"].get(field)
        if not isinstance(anchor, str) or not anchor.strip():
            continue
        pattern = (r"(?:(?:the\s+)?(?:supplier|vendor)\s+|供应商\s*)?"
                   + re.escape(anchor.strip())
                   + r"\s*(?:(?:has|have|had)\s*|已(?:经)?|现已)?")
        if re.fullmatch(pattern, subject, re.I):
            anchors.append((field, anchor))
    if not anchors:
        if re.search(r"\b(?:or|and|either|both)\b|或者|或|以及|和", subject, re.I):
            return None, [], "event_supply_ambiguous"
        return None, [], "event_supplier_unproven"
    if _retracted_elsewhere(parts, index, supplier):
        return None, [], "event_cancellation_unproven"
    # A recognized subject is insufficient for questions, conditions, or a
    # negated cancellation object. Do not infer intent from those forms.
    residual = clause[verb.end():]
    # A second predicate can attach the PO to continued supply rather than to
    # cancellation. Reject these compound tails instead of guessing its scope.
    if re.search(r"\b(?:and|or|either|both|but|while|whereas|although|however|still)\b"
                 r"|但是|但|而|仍|却|同时|不过|以及|和|且|并", residual, re.I):
        return None, [], "event_supply_ambiguous"
    following = parts[index + 2].strip() if index + 2 < len(parts) else ""
    if ending in {",", "，", ";", "；"} and re.search(
            r"\b(?:if|unless|possibly|false|incorrect|denied)\b|如果|假如|除非|可能|不实|错误", following, re.I):
        return None, [], "event_cancellation_unproven"
    if (re.search(r"\b(?:not|never|no|nothing|neither|may|might|could|would|should|if|unless|"
                  r"possibly|allegedly|reportedly|false|incorrect|denied)\b", residual, re.I)
            or re.search(r"没有|尚未|并未|不曾|不是|可能|如果|若|并非|不实|错误", residual)):
        return None, [], "event_cancellation_unproven"
    if not _supply_object(residual):
        return None, [], "event_cancellation_unproven"
    return clause, anchors, None


def bind_event(event, source, supplier, supply, product, remember):
    """Return a safe error code or None, based solely on visible business facts."""
    from forgeflow.investigation.evidence import related
    text, anchors, error = _cancellation_clause(trusted_event(event)["text"], supplier)
    if error:
        return error
    # Policy supplies the authorized disruption type. The event supplies which
    # actual commitment it concerns; an existing purchase alone is not proof.
    # Recognize conventional PO references even when none belong to the chosen
    # supplier. An explicit wrong/nonexistent reference must never disappear.
    # Typed product spans take precedence over their lexical spelling: SKU P123
    # is not a purchase-order assertion. The inverse masking below also keeps a
    # PO identifier from accidentally selecting a same-named product.
    product_pattern = (r'(?<![A-Za-z0-9_-])(?:(?:product|SKU)\s+|(?:产品|商品)\s*)'
                       r'(?:(?:"([^"\n]{1,128})")|(?:\'([^\'\n]{1,128})\')|([A-Za-z0-9][A-Za-z0-9_.-]*))')
    product_matches = list(re.finditer(product_pattern, text, re.I))
    po_text = list(text)
    for match in product_matches:
        po_text[match.start():match.end()] = " " * (match.end() - match.start())
    po_text = "".join(po_text)
    po_pattern = (r"(?<![A-Za-z0-9_-])(?:PO[-_][A-Za-z0-9_.-]+|PO\d+|P\d+)(?![A-Za-z0-9_-])"
                  r"|\bpurchase\s+order\s+[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)"
                  r"|采购(?:订单|单)\s*[\"']?([A-Za-z0-9][A-Za-z0-9_.-]*)")
    po_matches = list(re.finditer(po_pattern, po_text, re.I))
    po_tokens = {next((group for group in match.groups() if group), match.group())
                 for match in po_matches}
    product_text = list(text)
    for match in po_matches:
        product_text[match.start():match.end()] = " " * (match.end() - match.start())
    product_text = "".join(product_text)
    po_tokens = {token.rstrip(".,;:") for token in po_tokens
                 if token.casefold() not in {"has", "was", "is", "for", "that", "number"}}
    if len(po_tokens) > 20:
        return "event_supply_ambiguous"
    named_po_ids = set()
    for token in po_tokens:
        found = [row for row in _entities(source, "purchase_order", token)
                 if row["fields"]["name"].casefold() == token.casefold()]
        if not found:
            return "event_purchase_reference_unresolved"
        for row in found:
            remember(row)
            named_po_ids.add(row["id"])
    if len(named_po_ids) > 1:
        return "event_supply_ambiguous"
    product_tokens = set()
    for match in product_matches:
        token = next(group for group in match.groups() if group).rstrip(".,;:")
        if token.casefold() not in {"and", "or", "is", "has", "was", "for", "identity", "code", "policy"}:
            product_tokens.add(token)
    if len(product_tokens) > 20:
        return "event_supply_ambiguous"
    named_product_ids = set()
    for token in product_tokens:
        found = [row for row in _entities(source, "product", token) if any(
            str(row["fields"].get(field, "")).casefold() == token.casefold()
            for field in ("name", "default_code"))]
        if not found:
            return "event_product_reference_unresolved"
        for row in found:
            remember(row)
            named_product_ids.add(row["id"])
    if len(named_product_ids) > 1:
        return "event_supply_ambiguous"
    candidates = None
    for field, anchor in anchors:
        matches = {row["id"]: row for row in _entities(source, "supplier", anchor)
                   if str(row["fields"].get(field, "")).casefold() == anchor.casefold()}
        candidates = matches if candidates is None else {
            key: row for key, row in candidates.items() if key in matches}
    pairs = []
    for candidate in (candidates or {}).values():
        remember(candidate)
        for po in related(source, "supplier", candidate["id"]):
            if po["entity"] != "purchase_order" or po["fields"]["state"] not in {"purchase", "done"}:
                continue
            remember(po)
            for line in related(source, "purchase_order", po["id"]):
                if (line["entity"] != "purchase_order_line"
                        or line["fields"].get("quantity", 0) <= line["fields"].get("received_quantity", 0)):
                    continue
                remember(line)
                found = source.query("record", {"entity": "product", "id": line["fields"]["product_id"]})["records"]
                if not found:
                    return "event_supply_unproven"
                remember(found[0])
                pairs.append((candidate, po, found[0]))
    # An explicit order/product anchor can narrow a duplicate supplier name.
    explicit_po = [pair for pair in pairs if _mentioned(po_text, pair[1]["fields"].get("name"))]
    if named_po_ids:
        pairs = [pair for pair in pairs if pair[1]["id"] in named_po_ids]
    elif explicit_po:
        pairs = explicit_po
    explicit_product = [pair for pair in pairs if any(
        _mentioned(product_text, pair[2]["fields"].get(field)) for field in ("default_code", "name"))]
    if named_product_ids:
        pairs = [pair for pair in pairs if pair[2]["id"] in named_product_ids]
    elif explicit_product:
        pairs = explicit_product
    identities = {(s["id"], p["id"], item["id"]) for s, p, item in pairs}
    if not identities:
        return "event_supply_unproven"
    if len(identities) != 1:
        return "event_supply_ambiguous"
    if identities != {(supplier["id"], supply["id"], product["id"])}:
        return "event_supply_mismatch"
    return None
