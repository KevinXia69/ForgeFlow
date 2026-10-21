"""Business-effect deduplication for independently validated product candidates.

This compares intended ERP documents, not solver labels or objective scores.
Fresh document references are resolved through their contents and origin graph.
Identical fresh documents remain a multiset; incidental numbering is ignored.
The supported change-set graph is acyclic (PO -> MO -> SO).
"""
from __future__ import annotations

from decimal import Decimal

from forgeflow.recovery.storage import digest, encoded


def business_fingerprint(changes):
    operations = changes["operations"]
    by_token = {op["target"]["token"]: op for op in operations}
    cache, visiting = {}, set()

    def ordered(rows):
        return sorted(rows, key=encoded)

    def reference(ref):
        if ref["id"] is not None:
            return {"model": ref["model"], "id": ref["id"]}
        token = ref["token"]
        if token in visiting:
            raise ValueError("product_candidate_graph_invalid")
        if token not in cache:
            visiting.add(token)
            op = by_token[token]
            cache[token] = {"model": ref["model"], "new_document": fields(op["after"])}
            visiting.remove(token)
        return cache[token]

    def fields(value):
        if isinstance(value, dict):
            return {key: (ordered(reference(ref) for ref in item) if key == "origin_refs" else fields(item))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [fields(item) for item in value]
        if type(value) in (int, float):
            return {"number": str(Decimal(str(value)).normalize())}
        return value

    effects = [{"kind": op["kind"], "action": op["action"],
                "target": ({"model": op["target"]["model"], "new": True}
                           if op["target"]["id"] is None else reference(op["target"])),
                "after": fields(op["after"])} for op in operations]
    return digest(ordered(effects))
