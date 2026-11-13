"""Real RPC/ORM matrix, only against the disposable acceptance runner's database.

Never consumes the ordinary FORGEFLOW_ODOO20_* production/developer credentials.
Absent isolation configuration is explicitly skipped; the runner treats skips
as unverified and exits nonzero.
"""
from __future__ import annotations

import os
import time
import uuid
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from urllib.parse import urlparse

import pytest

PLAN = "forgeflow.recovery_plan"
DECISION = "forgeflow.decision_record"
OPERATION = "forgeflow.execution_operation"
RECEIPT = "forgeflow.execution_receipt"


class RPC:
    def __init__(self, url, database, login, password, company=None):
        self.url, self.login = url, login
        self.database, self.password, self.company = database, password, company
        common = xmlrpc.client.ServerProxy(url + "/xmlrpc/2/common")
        self.uid = common.authenticate(database, login, password, {})
        assert self.uid, "isolated_test_identity_authentication_failed"
        self.models = xmlrpc.client.ServerProxy(url + "/xmlrpc/2/object", allow_none=True)

    def call(self, model, method, args, **kwargs):
        context = kwargs.pop("context", {})
        if self.company:
            context = {"allowed_company_ids": [self.company],
                       "default_company_id": self.company, **context}
        if context:
            kwargs["context"] = context
        return self.models.execute_kw(self.database, self.uid, self.password,
                                      model, method, args, kwargs)

    def create(self, model, values):
        return self.call(model, "create", [values])

    def rows(self, model, domain, fields):
        return self.call(model, "search_read", [domain], fields=fields, order="id")


class Native:
    def __init__(self, url, database, password):
        self.url, self.database, self.password = url, database, password
        self.admin = RPC(url, database, "ff_acceptance_admin", password)
        self.companies = [self.admin.create("res.company", {"name": f"Acceptance {label}"})
                          for label in ("A", "B")]
        admin_companies = self.admin.rows("res.users", [("id", "=", self.admin.uid)],
                                         ["company_ids"])[0]["company_ids"]
        self.admin.call("res.users", "write", [[self.admin.uid], {
            "company_ids": [(6, 0, admin_companies + self.companies)]}])
        self.picking_types = {}
        for index, company in enumerate(self.companies):
            self.admin.company = company
            warehouses = self.admin.rows("stock.warehouse", [("company_id", "=", company)],
                                         ["in_type_id"])
            if not warehouses:
                warehouse = self.admin.create("stock.warehouse", {
                    "name": f"Acceptance warehouse {index}", "code": f"FFA{index}",
                    "company_id": company})
                warehouses = self.admin.rows("stock.warehouse", [("id", "=", warehouse)],
                                             ["in_type_id"])
            self.picking_types[company] = warehouses[0]["in_type_id"][0]
        self.admin.company = self.companies[0]
        fields = self.admin.call("res.users", "fields_get", [], attributes=["type"])
        self.group_field = "group_ids" if "group_ids" in fields else "groups_id"
        self.employee = self.user("employee", [])
        self.planner = self.user("planner", ["planner"])
        self.approver = self.user("approver", ["approver"])
        self.other_approver = self.user("other-approver", ["approver"])
        self.executor = self.user("executor", ["executor"])
        self.service = self.user("service", ["service"])
        self.other_service = self.user("other-service", ["service"])

    def group(self, xmlid):
        module, name = xmlid.split(".")
        rows = self.admin.rows("ir.model.data", [("module", "=", module),
                                                ("name", "=", name)], ["res_id"])
        assert rows, f"required_group_missing:{xmlid}"
        return rows[0]["res_id"]

    def user(self, label, roles):
        groups = [self.group("base.group_user")]
        groups.extend(self.group(f"forgeflow_general.group_forgeflow_{role}") for role in roles)
        if roles:
            groups.extend(self.group(xmlid) for xmlid in (
                "purchase.group_purchase_user", "stock.group_stock_user", "mrp.group_mrp_user"))
        login = f"ff-{label}-{uuid.uuid4().hex[:8]}"
        self.admin.create("res.users", {
            "name": label, "login": login, "password": self.password,
            "company_id": self.companies[0], "company_ids": [(6, 0, [self.companies[0]])],
            self.group_field: [(6, 0, groups)],
        })
        return RPC(self.url, self.database, login, self.password, self.companies[0])

    def plan(self, *, company=None, rollback=False, foreign_partner=None):
        company = company or self.companies[0]
        creator = self.planner if company == self.companies[0] else self.admin
        previous = creator.company
        creator.company = company
        try:
            # Fixture setup uses the administrator; tested actors retain only
            # their normal business/ForgeFlow role permissions.
            partner = self.admin.create("res.partner", {"name": "Acceptance vendor",
                                                        "supplier_rank": 1})
            order = creator.create("purchase.order", {"partner_id": partner,
                                                        "picking_type_id": self.picking_types[company],
                                                        "company_id": company})
            exception = creator.create("forgeflow.exception_event", {
                "name": "Acceptance disruption", "company_id": company,
                "event_type": "supplier_disruption",
                "occurred_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            })
            actions = [{"action_id": "cancel", "type": "cancel_purchase_order",
                        "target_model": "purchase.order", "target_id": order, "params": {}}]
            if rollback or foreign_partner:
                # Cancellation executes first, then a nonexistent product fails
                # creation inside the same business savepoint.
                actions.append({
                    "action_id": "replacement", "type": "create_purchase_order",
                    "target_model": "purchase.order", "target_id": 0,
                    "quantity": 1, "unit": "Units", "currency": "USD",
                    "params": {"partner_id": foreign_partner or partner, "lines": [
                        {"product_id": 2147483000, "product_qty": 1, "price_unit": 1}]}})
            plan_id = creator.call(PLAN, "forgeflow_prepare", [
                [], exception, actions, {"affected_targets": [f"purchase.order:{order}"]},
                "native-isolated-acceptance"], tenant_ref="isolated-acceptance")
            row = creator.rows(PLAN, [("id", "=", plan_id)],
                               ["version", "plan_digest", "action_digest"])[0]
            return {"id": plan_id, "order": order, "company": company,
                    "review": {"expected_plan_version": row["version"],
                               "expected_plan_digest": row["plan_digest"],
                               "expected_action_digest": row["action_digest"]}}
        finally:
            creator.company = previous

    def approve(self, plan, *, caller=None, approver=None, **kwargs):
        caller = caller or self.approver
        return caller.call(PLAN, "forgeflow_approve", [
            [plan["id"]], (approver or self.approver).uid, "native-isolated-executor"],
            **{**plan["review"], **kwargs})

    def delegate(self, plan, **kwargs):
        return self.approver.call(PLAN, "forgeflow_delegate_approval",
                                  [[plan["id"]], self.service.uid],
                                  **{**plan["review"], **kwargs})

    def snapshot(self, plan):
        return {"order": self.admin.rows("purchase.order", [("id", "=", plan["order"])],
                                          ["state", "order_line"]),
                "plan": self.admin.rows(PLAN, [("id", "=", plan["id"])], ["state"]),
                "operations": self.admin.rows(OPERATION, [("plan_id", "=", plan["id"])],
                                               ["state", "action_id"])}


@pytest.fixture(scope="module")
def native():
    if os.environ.get("FORGEFLOW_ACCEPTANCE_ISOLATED") != "1":
        pytest.skip("UNVERIFIED: use accept_odoo20_native_isolated.py to create disposable Odoo")
    url = os.environ.get("FORGEFLOW_ACCEPTANCE_URL", "")
    database = os.environ.get("FORGEFLOW_ACCEPTANCE_DB", "")
    password = os.environ.get("FORGEFLOW_ACCEPTANCE_PASSWORD", "")
    assert urlparse(url).hostname == "127.0.0.1" and database.startswith("ff_acceptance_")
    assert password, "isolated_password_missing"
    return Native(url, database, password)


def test_employee_and_forged_approver_cannot_approve_or_execute(native):
    plan = native.plan()
    before = native.snapshot(plan)
    with pytest.raises(xmlrpc.client.Fault):
        native.approve(plan, caller=native.employee)
    with pytest.raises(xmlrpc.client.Fault):
        native.approve(plan, caller=native.approver, approver=native.other_approver)
    with pytest.raises(xmlrpc.client.Fault):
        native.employee.call(PLAN, "forgeflow_apply_plan",
                             [[plan["id"]], "employee", "native-isolated-executor"])
    assert native.snapshot(plan) == before


@pytest.mark.parametrize("identity", ["employee", "service", "approver"])
def test_control_records_reject_direct_rpc_mutation_and_context_forgery(native, identity):
    plan = native.plan()
    caller = getattr(native, identity)
    before = native.snapshot(plan)
    for context in ({}, {"_forgeflow_workflow_capability": True},
                    {"_forgeflow_workflow_capability": "trusted"}):
        with pytest.raises(xmlrpc.client.Fault):
            caller.call(PLAN, "write", [[plan["id"]], {"state": "approved"}], context=context)
        for model, values in (
            (DECISION, {"plan_id": plan["id"], "approver_id": native.approver.uid,
                        "decision": "approved", "plan_hash": "forged"}),
            (OPERATION, {"plan_id": plan["id"], "operation_type": "cancel_purchase_order",
                         "target_model": "purchase.order", "target_id": plan["order"],
                         "fingerprint": "forged", "request_hash": "forged",
                         "idempotency_key": "forged"}),
            (RECEIPT, {"operation_id": 2147483000, "state": "verified", "verified": True}),
        ):
            with pytest.raises(xmlrpc.client.Fault):
                caller.call(model, "create", [values], context=context)
    assert native.snapshot(plan) == before


def test_company_scope_cannot_be_overridden_by_client_context(native):
    plan = native.plan(company=native.companies[1])
    for caller in (native.employee, native.approver, native.service):
        assert caller.rows(PLAN, [("id", "=", plan["id"])], ["state"]) == []
        with pytest.raises(xmlrpc.client.Fault):
            caller.call(PLAN, "read", [[plan["id"]]], fields=["state"])
        with pytest.raises(xmlrpc.client.Fault):
            native.approve(plan, caller=caller,
                           context={"allowed_company_ids": [native.companies[1]]})


def test_direct_approval_executes_and_replays_original_attempt(native):
    plan = native.plan()
    decision = native.approve(plan)
    audit = native.admin.rows(DECISION, [("id", "=", decision)],
                              ["approver_id", "caller_id", "delegation_id"])[0]
    assert audit["approver_id"][0] == audit["caller_id"][0] == native.approver.uid
    first = native.executor.call(PLAN, "forgeflow_apply_plan",
                                 [[plan["id"]], "first", "native-isolated-executor"])
    assert first["state"] == "executed"
    assert native.executor.call(PLAN, "forgeflow_verify", [[plan["id"]]])["status"] == "verified"
    before = native.snapshot(plan)
    second = native.executor.call(PLAN, "forgeflow_apply_plan",
                                  [[plan["id"]], "different-prefix", "native-isolated-executor"])
    assert second["state"] == "executed"
    assert native.snapshot(plan) == before
    assert before["order"][0]["state"] == "cancel"
    assert len(before["operations"]) == 1


def test_existing_decision_operation_and_receipt_cannot_be_edited_or_deleted(native):
    plan = native.plan()
    decision = native.approve(plan)
    native.executor.call(PLAN, "forgeflow_apply_plan",
                         [[plan["id"]], "audit", "native-isolated-executor"])
    operation = native.admin.rows(OPERATION, [("plan_id", "=", plan["id"])], ["id"])[0]["id"]
    receipt = native.admin.rows(RECEIPT, [("operation_id", "=", operation)], ["id"])[0]["id"]
    for caller in (native.employee, native.service, native.approver):
        for model, record, values in ((DECISION, decision, {"decision": "rejected"}),
                                      (OPERATION, operation, {"state": "verified"}),
                                      (RECEIPT, receipt, {"verified": True})):
            with pytest.raises(xmlrpc.client.Fault):
                caller.call(model, "write", [[record], values])
            with pytest.raises(xmlrpc.client.Fault):
                caller.call(model, "unlink", [[record]])


def test_executor_cannot_apply_an_unapproved_plan(native):
    plan = native.plan()
    before = native.snapshot(plan)
    with pytest.raises(xmlrpc.client.Fault):
        native.executor.call(PLAN, "forgeflow_apply_plan",
                             [[plan["id"]], "no-approval", "native-isolated-executor"])
    assert native.snapshot(plan) == before


def test_native_verify_cannot_bypass_missing_execution_evidence(native):
    plan = native.plan()
    native.approve(plan)
    before = native.snapshot(plan)
    result = native.executor.call(PLAN, "forgeflow_verify", [[plan["id"]]])
    assert result["status"] == "unknown"
    assert native.snapshot(plan) == before


def test_readback_before_approval_does_not_change_plan_or_business_state(native):
    plan = native.plan()
    before = native.snapshot(plan)
    native.executor.call(PLAN, "forgeflow_readback", [[plan["id"]]])
    assert native.snapshot(plan) == before
    assert native.approve(plan)


def test_unknown_and_verify_race_requires_reconciliation_after_unknown_commits(native):
    plan = native.plan()
    native.approve(plan)
    native.executor.call(PLAN, "forgeflow_apply_plan",
                         [[plan["id"]], "unknown-race", "native-isolated-executor"])
    operation = native.admin.rows(OPERATION, [("plan_id", "=", plan["id"])],
                                   ["id", "idempotency_key"])[0]
    barrier = Barrier(2)

    def invoke(method, args):
        actor = native.executor
        client = RPC(actor.url, actor.database, actor.login, actor.password, actor.company)
        barrier.wait(timeout=15)
        return client.call(PLAN, method, args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        verification = pool.submit(invoke, "forgeflow_verify", [[plan["id"]]])
        uncertain = pool.submit(invoke, "forgeflow_mark_unknown",
                                [[plan["id"]], operation["idempotency_key"], "lost response probe"])
        verify_result, unknown_result = verification.result(timeout=45), uncertain.result(timeout=45)
    # Both lock acquisition orders are legitimate: verification may complete
    # before the unknown transition. After that transition commits, it cannot
    # create another successful verification from the now-unknown operation.
    assert verify_result["status"] in {"verified", "unknown"}
    assert unknown_result["state"] == "unknown"
    receipts = native.admin.rows(RECEIPT, [("operation_id", "=", operation["id"])],
                                  ["state", "verified"])
    verified_count = sum(bool(row["verified"]) for row in receipts)
    assert verified_count == (1 if verify_result["status"] == "verified" else 0)
    assert native.executor.call(PLAN, "forgeflow_verify", [[plan["id"]]])["status"] == "unknown"
    assert native.admin.rows(RECEIPT, [("operation_id", "=", operation["id"])],
                              ["state", "verified"]) == receipts
    assert native.snapshot(plan)["order"][0]["state"] == "cancel"


def test_service_requires_plan_specific_human_delegation_and_audits_both(native):
    plan = native.plan()
    with pytest.raises(xmlrpc.client.Fault):
        native.approve(plan, caller=native.service)
    grant = native.delegate(plan)
    other = native.plan()
    with pytest.raises(xmlrpc.client.Fault):
        native.approve(other, caller=native.service, delegation_id=grant)
    with pytest.raises(xmlrpc.client.Fault):
        native.approve(plan, caller=native.other_service, delegation_id=grant)
    decision = native.approve(plan, caller=native.service, delegation_id=grant)
    audit = native.admin.rows(DECISION, [("id", "=", decision)],
                              ["approver_id", "caller_id", "delegation_id"])[0]
    assert audit["approver_id"][0] == native.approver.uid
    assert audit["caller_id"][0] == native.service.uid
    assert audit["delegation_id"][0] == grant
    result = native.service.call(PLAN, "forgeflow_apply_plan",
                                  [[plan["id"]], "delegated", "native-isolated-executor"])
    assert result["state"] == "executed"


@pytest.mark.parametrize("expiry", [False, True])
def test_expired_or_revoked_delegation_cannot_approve(native, expiry):
    plan = native.plan()
    before = native.snapshot(plan)
    grant = native.delegate(plan, expires_in_hours=0.000001 if expiry else 1)
    if expiry:
        # Odoo's Datetime.now() and RPC values have second granularity.
        time.sleep(1.2)
    else:
        native.approver.call("forgeflow.approval_delegation", "forgeflow_revoke", [[grant]])
    with pytest.raises(xmlrpc.client.Fault):
        native.approve(plan, caller=native.service, delegation_id=grant)
    assert native.snapshot(plan) == before


def test_delegation_revoked_after_approval_blocks_execution(native):
    plan = native.plan()
    grant = native.delegate(plan)
    native.approve(plan, caller=native.service, delegation_id=grant)
    native.approver.call("forgeflow.approval_delegation", "forgeflow_revoke", [[grant]])
    before = native.snapshot(plan)
    with pytest.raises(xmlrpc.client.Fault):
        native.service.call(PLAN, "forgeflow_apply_plan",
                             [[plan["id"]], "revoked", "native-isolated-executor"])
    assert native.snapshot(plan) == before


def test_stale_review_is_rejected_before_any_decision(native):
    plan = native.plan()
    before = native.snapshot(plan)
    with pytest.raises(xmlrpc.client.Fault):
        native.approve(plan, expected_plan_version=plan["review"]["expected_plan_version"] + 1)
    assert native.snapshot(plan) == before
    assert native.admin.rows(DECISION, [("plan_id", "=", plan["id"])], ["id"]) == []


def test_failed_second_business_action_rolls_back_first_and_keeps_receipts(native):
    plan = native.plan(rollback=True)
    native.approve(plan)
    before = native.snapshot(plan)
    result = native.executor.call(PLAN, "forgeflow_apply_plan",
                                  [[plan["id"]], "rollback", "native-isolated-executor"])
    assert result["state"] == "execution_failed" and result["rolled_back"] is True
    after = native.snapshot(plan)
    assert after["order"] == before["order"]
    assert len(after["operations"]) == 2
    for operation in after["operations"]:
        receipts = native.admin.rows(RECEIPT, [("operation_id", "=", operation["id"])],
                                      ["state", "verified"])
        assert receipts and all(not receipt["verified"] for receipt in receipts)


def test_revoke_and_execute_race_serializes_to_one_authorized_outcome(native):
    plan = native.plan()
    decision = native.approve(plan)
    barrier = Barrier(2)

    def invoke(actor, model, method, args):
        # XML-RPC transports are not shared between the concurrent requests.
        client = RPC(actor.url, actor.database, actor.login, actor.password, actor.company)
        barrier.wait(timeout=15)
        try:
            return {"ok": True, "result": client.call(model, method, args)}
        except xmlrpc.client.Fault as error:
            return {"ok": False, "error": error.faultString}

    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(invoke, native.executor, PLAN, "forgeflow_apply_plan",
                                [[plan["id"]], "race", "native-isolated-executor"])
        revocation = pool.submit(invoke, native.approver, DECISION, "forgeflow_revoke",
                                 [[decision]])
        applied, revoked = execution.result(timeout=45), revocation.result(timeout=45)
    after = native.snapshot(plan)
    if applied["ok"]:
        assert applied["result"]["state"] == "executed"
        assert revoked["ok"] is False
        assert "plan_already_executing" in revoked["error"]
        assert after["order"][0]["state"] == "cancel"
        assert len(after["operations"]) == 1
    else:
        assert revoked["ok"] is True
        assert "approval" in applied["error"]
        assert after["order"][0]["state"] == "draft"
        assert after["operations"] == []


def test_multicompany_executor_cannot_use_another_company_partner(native):
    native.admin.company = native.companies[1]
    try:
        partner = native.admin.create("res.partner", {
            "name": "Other company private vendor", "company_id": native.companies[1],
            "supplier_rank": 1})
    finally:
        native.admin.company = native.companies[0]
    native.admin.call("res.users", "write", [[native.executor.uid], {
        "company_ids": [(6, 0, native.companies)]}])
    plan = native.plan(foreign_partner=partner)
    native.approve(plan)
    before = native.snapshot(plan)
    result = native.executor.call(PLAN, "forgeflow_apply_plan",
                                  [[plan["id"]], "foreign-partner", "native-isolated-executor"],
                                  context={"allowed_company_ids": native.companies})
    assert result["state"] == "execution_failed" and result["rolled_back"]
    assert "company" in result["error"]
    assert native.snapshot(plan)["order"] == before["order"]
