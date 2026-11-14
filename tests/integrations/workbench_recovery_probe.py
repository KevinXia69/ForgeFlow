"""Execute one explicitly approved synthetic recovery through the real HTTP API.

This is only mounted into the acceptance runner's disposable stack. It neither
uses production credentials nor claims to test every action or the browser UI.
"""
from __future__ import annotations

import hashlib
import json
import os

from workbench_http_probe import local_session

STAGE = "isolation"


def probe():
    global STAGE
    if os.environ.get("FORGEFLOW_WORKBENCH_ISOLATED_TEST") != "1":
        raise RuntimeError("disposable_workbench_probe_required")
    from forgeflow.integrations.odoo20.adapter import Odoo20Error
    from forgeflow.integrations.odoo20.verifier import Odoo20BusinessVerifier
    from forgeflow.local_workbench.admission import paused
    from forgeflow.local_workbench.bootstrap import _adapter
    from forgeflow.local_workbench.config import load_config
    from forgeflow.local_workbench.runtime import configure_environment

    config = load_config()
    configure_environment(config)
    assert paused(config), "background_admission_must_be_paused"
    adapter = _adapter(config)
    assert adapter.config.database == "forgeflow_local"
    assert adapter.config.url.rstrip("/") == "http://odoo:8069"
    actor = adapter._authenticate()
    companies = adapter.user_company_ids(actor)
    assert len(companies) == 1
    adapter = adapter.for_company(companies[0])
    orders = adapter.search_read("purchase.order", [("name", "=", "FF-LOCAL-PO-001")],
                                 ["id", "state", "company_id"], limit=2)
    assert len(orders) == 1 and orders[0]["state"] == "cancel"
    assert orders[0]["company_id"][0] == companies[0]

    def business_state():
        result = {}
        for model in ("purchase.order", "purchase.order.line", "sale.order", "sale.order.line",
                      "stock.move", "stock.quant", "mrp.production"):
            # Fresh synthetic fixtures are tiny. Reaching the limit is not a
            # complete snapshot and therefore blocks this acceptance claim.
            rows = adapter.search_read(model, [], ["id", "write_date"], limit=1000, order="id")
            assert len(rows) < 1000
            result[model] = rows
        return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()

    request, read, jar = local_session()
    assert request("/api/auth/session", method="POST", body={
        "invite_code": config["env"]["FORGEFLOW_INVITE_CODES"]})[0] == 201
    saved_cookie = "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)
    assert saved_cookie
    checks = {}
    try:
        STAGE = "prepare"
        before = business_state()
        status, raw, _ = request("/api/odoo20/plans", method="POST", body={
            "purchase_order_id": orders[0]["id"], "reason": "Disposable installer acceptance"})
        assert status == 200, "http_plan_prepare_failed"
        prepared = json.loads(raw)
        assert prepared["actions"] and prepared["action_count"] == len(prepared["actions"])
        assert any(action["type"] == "create_purchase_order" for action in prepared["actions"])
        plan_id = prepared["plan_id"]
        review = read(f"/api/odoo20/plans/{plan_id}")
        for field in ("plan_id", "plan_version", "plan_digest", "action_digest", "actions"):
            assert prepared[field] == review[field]
        assert business_state() == before
        checks["prepare_and_review_business_unchanged"] = True
        STAGE = "unapproved"
        try:
            adapter.apply_plan(plan_id, "installer-unapproved-probe", "installer-unapproved")
        except Odoo20Error:
            pass
        else:
            raise AssertionError("unapproved_execution_accepted")
        assert business_state() == before
        assert not adapter.search_read("forgeflow.execution_operation", [("plan_id", "=", plan_id)], ["id"])
        checks["unapproved_native_execution_rejected_without_operations"] = True
        STAGE = "review_binding"
        approval = {"comment": "Explicit disposable synthetic acceptance",
                    "expected_plan_version": review["plan_version"],
                    "expected_plan_digest": review["plan_digest"],
                    "expected_action_digest": review["action_digest"]}
        stale = {**approval, "expected_plan_version": int(review["plan_version"]) + 1}
        assert request(f"/api/odoo20/plans/{plan_id}/approve", method="POST", body=stale)[0] == 409
        assert not adapter.search_read("forgeflow.decision_record", [("plan_id", "=", plan_id)], ["id"])
        assert business_state() == before
        checks["stale_review_rejected_without_decision"] = True
        STAGE = "approve"
        status, raw, _ = request(f"/api/odoo20/plans/{plan_id}/approve", method="POST", body=approval)
        assert status == 200 and json.loads(raw)["state"] == "approved"
        assert business_state() == before
        decisions = adapter.search_read("forgeflow.decision_record", [("plan_id", "=", plan_id)],
                                        ["approver_id", "caller_id", "plan_hash"])
        assert len(decisions) == 1
        assert decisions[0]["approver_id"][0] == decisions[0]["caller_id"][0] == actor
        checks["exact_review_approved_by_bound_operator"] = True
        STAGE = "apply"
        status, raw, _ = request(f"/api/odoo20/plans/{plan_id}/apply", method="POST",
                                 body={"idempotency_key": "installer-synthetic-recovery"})
        assert status == 200, "http_plan_apply_failed"
        applied = json.loads(raw)
        assert applied["state"] == "succeeded" and applied["verification"] == "verified"
        assert business_state() != before
        STAGE = "independent_readback"
        verified = Odoo20BusinessVerifier(adapter).verify_plan(plan_id)
        assert verified.verified
        assert adapter.readback_plan(plan_id)
        operations = adapter.search_read("forgeflow.execution_operation", [("plan_id", "=", plan_id)],
                                         ["id", "state", "action_id"], order="id")
        assert len(operations) == prepared["action_count"]
        assert {row["action_id"] for row in operations} == {a["action_id"] for a in prepared["actions"]}
        checks["http_apply_and_independent_native_readback_verified"] = True
        STAGE = "native_replay"
        after = business_state()
        from forgeflow.integrations.odoo20.business import Odoo20BusinessService

        current = Odoo20BusinessService(adapter).load_prepared(plan_id)
        replay = adapter.apply_plan(plan_id, "installer-retry", current.execution_identity)
        assert replay["state"] in {"executed", "verified"}
        assert business_state() == after
        assert adapter.search_read("forgeflow.execution_operation", [("plan_id", "=", plan_id)],
                                   ["id", "state", "action_id"], order="id") == operations
        checks["native_repeat_apply_no_business_or_operation_duplication"] = True
    finally:
        assert request("/api/auth/session", method="DELETE")[0] == 204
        assert request("/api/auth/me", cookie=saved_cookie)[0] == 401
    return {"ok": True, "checks": checks, "approved_plans": 1,
            "actions": len(operations), "action_types": sorted({a["type"] for a in prepared["actions"]}),
            "real_provider_requests": 0, "scope": "one_synthetic_purchase_recovery_via_http_and_native_rpc"}


if __name__ == "__main__":
    try:
        result = probe()
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__, "stage": STAGE}
    print("FORGEFLOW_WORKBENCH_RECOVERY_PROBE=" + json.dumps(result), flush=True)
    raise SystemExit(0 if result["ok"] else 1)
