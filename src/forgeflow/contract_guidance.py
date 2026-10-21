"""Opt-in visibility of reviewed task inputs, never a proposed recovery plan."""
from __future__ import annotations

import hashlib
import json

from .contract import contract_from_payload

PROFILES = ("none", "reviewed-request-v1")


def resolve_contract_guidance(profile: str = "none", payload: dict | None = None) -> dict:
    if profile not in PROFILES:
        raise ValueError("Unknown contract guidance.")
    if profile == "none":
        return {"profile": "none", "version": None, "sha256": None, "text": ""}
    if payload is None:
        raise ValueError("Contract guidance requires a reviewed contract.")
    contract = contract_from_payload(payload)
    identity = "vendor_ref" if contract.task_type == "supplier_cancellation" else "workcenter_code"
    request = {
        "target_product_code": contract.product_code,
        "anchor_date": contract.anchor_date,
        "margin": contract.margin,
        "sales_order_ids": list(contract.sales_order_ids),
        "sales_order_budget_caps": dict(contract.budgets),
        "disruption": {"kind": contract.task_type, identity: contract.disruption[identity]},
    }
    text = (
        "A reviewed task policy is enforced by the recovery executor. Its exact input "
        "values are supplied below; this is a request, not a solution or an execution result. "
        "Use this request with prepare_recovery, which reads the live ERP, checks the "
        "policy and computes a plan. Do not reconstruct the entire ERP before calling it. "
        "If preparation reports a specific identifier or scope conflict, use bounded "
        "read-only queries to investigate and report the conflict; do not weaken or "
        "silently replace the enforced policy. Execute only a successfully prepared plan "
        "through execute_recovery. No human approval is added to this autonomous trial.\n"
        + json.dumps(request, sort_keys=True, ensure_ascii=True)
    )
    return {"profile": profile, "version": 1,
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text}
