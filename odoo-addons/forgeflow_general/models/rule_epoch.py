"""Canonical fingerprint of the business rules that are in force.

The Odoo model calls these functions; the tests import this file directly, so
the digest can be checked without an Odoo runtime.

Why this module exists
----------------------

The previous epoch hashed ``name:version:rule_hash:scope_model``.  Four things
could change while that string stayed identical:

* the rule body (``condition_json``) edited in place;
* the object scope (``scope_ref``), unit or currency;
* the validity window, including a rule silently expiring or starting;
* a stale ``rule_hash`` that nobody recomputed.

An approval prepared before such a change therefore kept passing the "rules did
not change" check.  The fingerprint below is built from the *content* that
decides what the rule does, and from whether the rule is effective *now*, so
each of those changes moves the epoch.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

#: Bumped when the canonical form changes.  It is part of the digest, so an
#: epoch recorded by an older algorithm cannot be mistaken for a current one.
PROTOCOL = "rule-epoch-v2"

#: Fields whose value decides what a confirmed rule means.  Editable metadata
#: (who confirmed it, when) is deliberately absent: it cannot change behaviour.
CONTENT_FIELDS: tuple[str, ...] = (
    "name",
    "company_id",
    "scope_model",
    "scope_ref",
    "condition_json",
    "unit",
    "currency_id",
    "valid_from",
    "valid_to",
    "version",
    "source_ref",
)


def canonical_condition(raw: Any) -> str:
    """Normalise the rule body so formatting alone is not a change.

    Valid JSON is re-serialised with sorted keys.  Text that is not JSON is
    kept verbatim (after trimming): a rule nobody can parse is still a rule
    whose text matters, and silently dropping it would hide a broken rule.
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return text
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _as_utc(value: Any) -> datetime | None:
    """Odoo datetimes are naive UTC; accept aware values without shifting them."""
    if value is None or value == "" or value is False:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.replace(microsecond=0)


def _stamp(value: Any) -> str:
    moment = _as_utc(value)
    return moment.isoformat() if moment is not None else ("" if value in (None, False)
                                                          else str(value).strip())


def is_effective(valid_from: Any, valid_to: Any, now: datetime) -> bool:
    """A rule applies when ``valid_from <= now < valid_to``.

    A missing bound is open-ended.  A bound that cannot be parsed is treated as
    open, not as "outside the window": excluding a rule because its date is
    malformed would silently remove a confirmed constraint.
    """
    start = _as_utc(valid_from)
    end = _as_utc(valid_to)
    moment = _as_utc(now) or datetime.now(UTC).replace(tzinfo=None)
    if start is not None and moment < start:
        return False
    if end is not None and moment >= end:
        return False
    return True


def rule_payload(rule: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a rule that decides behaviour, in canonical form."""
    return {
        "name": str(rule.get("name") or ""),
        "company": str(rule.get("company") or ""),
        "scope_model": str(rule.get("scope_model") or ""),
        "scope_ref": str(rule.get("scope_ref") or ""),
        "condition": canonical_condition(rule.get("condition_json")),
        "unit": str(rule.get("unit") or ""),
        "currency": str(rule.get("currency") or ""),
        "valid_from": _stamp(rule.get("valid_from")),
        "valid_to": _stamp(rule.get("valid_to")),
        "version": int(rule.get("version") or 1),
        "source_ref": str(rule.get("source_ref") or ""),
    }


def rule_content_hash(rule: Mapping[str, Any]) -> str:
    """A stable hash of one rule's content, for ``rule_hash``.

    The stored hash is only a convenience for display and comparison; the epoch
    does not trust it, because a write path can forget to refresh it.
    """
    payload = rule_payload(rule)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def epoch(rules: Iterable[Mapping[str, Any]], *, now: datetime) -> str:
    """Digest of every effective rule.

    The order of records is normalised so a database reordering is not a rule
    change.  Two rules with identical content collapse to the same entry only
    when *every* payload field matches, which is what makes duplicates harmless
    and edits visible.
    """
    entries = [rule_payload(rule) for rule in rules
               if is_effective(rule.get("valid_from"), rule.get("valid_to"), now)]
    entries.sort(key=lambda item: json.dumps(item, sort_keys=True,
                                             separators=(",", ":"), ensure_ascii=False))
    raw = json.dumps({"protocol": PROTOCOL, "rules": entries}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


#: Convenience values for the model's ``write`` guard and the epoch call.
def changed_content_fields(values: Mapping[str, Any]) -> frozenset[str]:
    """Content fields present in an ORM ``write`` payload."""
    return frozenset(CONTENT_FIELDS).intersection(values)
