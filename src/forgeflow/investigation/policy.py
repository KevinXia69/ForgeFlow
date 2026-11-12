"""Trusted, incomplete business policy; never an executable authorization.

An intake policy may omit budgets. Only an independently supplied TaskContract
can authorize prepare; neither the model nor this module upgrades intake data.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from forgeflow.contract import TaskContract, contract_from_payload
from forgeflow.investigation.sources import _policy
from forgeflow.recovery.storage import digest

KIND = "forgeflow.investigation_policy"


@dataclass(frozen=True)
class PartialPolicy:
    _json: str

    def payload(self) -> dict:
        return json.loads(self._json)

    @property
    def task_id(self):
        return self.payload()["task_id"]

    @property
    def contract_id(self):
        # Compatibility label only. This object cannot authorize preparation.
        return self.payload()["policy_id"]

    def digest(self):
        return digest(self.payload())

    def source_policy(self):
        value = self.payload()
        return {"contract_id": value["policy_id"], **{
            key: value[key] for key in ("task_type", "anchor_date", "margin",
                                        "margin_basis", "time_granularity", "orders")}}


def policy_from_payload(value: dict) -> PartialPolicy:
    keys = {"kind", "schema_version", "policy_id", "task_id", "task_type", "anchor_date",
            "margin", "margin_basis", "time_granularity", "orders", "sources"}
    if (not isinstance(value, dict) or set(value) != keys or value.get("kind") != KIND
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
        raise ValueError("invalid_investigation_policy")
    from forgeflow.contract import CODE
    if (not isinstance(value["task_id"], str) or not CODE.fullmatch(value["task_id"])
            or not isinstance(value["sources"], dict) or not value["sources"]
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                   for k, v in value["sources"].items())):
        raise ValueError("invalid_investigation_policy")
    source = {"contract_id": value["policy_id"], **{
        key: value[key] for key in ("task_type", "anchor_date", "margin", "margin_basis",
                                    "time_granularity", "orders")}}
    _policy(source)
    if source["task_type"] != "supplier_cancellation" or not source["orders"]:
        raise ValueError("invalid_investigation_policy")
    return PartialPolicy(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))


def load_investigation_policy(value: dict) -> TaskContract | PartialPolicy:
    if isinstance(value, dict) and value.get("kind") == KIND:
        return policy_from_payload(value)
    return contract_from_payload(value)
