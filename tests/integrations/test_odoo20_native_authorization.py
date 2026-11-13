"""Actual addon entry-point code in an isolated Python harness (not an ORM test).

Real ACL/RPC acceptance lives in test_odoo20_native_authorization_live.py.
These tests intentionally extract source methods, so exception-path regressions
in the addon glue are covered rather than reimplementing the approval logic.
"""
from __future__ import annotations

import ast
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ADDON = Path(__file__).resolve().parents[2] / "odoo-addons/forgeflow_general/models"
spec = importlib.util.spec_from_file_location("native_authorization_logic", ADDON / "recovery_logic.py")
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)


class Denied(Exception):
    pass


class ModelEnv(dict):
    cr = SimpleNamespace(execute=lambda *args: None)


def method(file, name, **namespace):
    tree = ast.parse((ADDON / file).read_text())
    node = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    code = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    env = {"UserError": Denied, "AccessError": Denied, "recovery_logic": logic,
           "clean_for_rpc": logic.clean_for_rpc, **namespace}
    exec(compile(code, str(ADDON / file), "exec"), env)
    return env[name]


permission = method("authorization.py", "permission",
                    _logger=SimpleNamespace(warning=lambda *a: None))


class User:
    def __init__(self, uid=7, permitted=True, active=True):
        self.id, self.active, self.permitted = uid, active, permitted
        self.company_ids = [1]

    def has_group(self, group):
        if isinstance(self.permitted, Exception):
            raise self.permitted
        return self.permitted


@pytest.mark.parametrize("allowed", [True, False, None, "yes", 1])
def test_group_permission_requires_explicit_true(allowed):
    assert permission(User(permitted=allowed), "approver", plan_id=42) is (allowed is True)


def approval_plan(allowed=True, *, active=True, missing=False):
    user = User(permitted=allowed, active=active)
    decision = SimpleNamespace(
        id=9, invalidate_recordset=lambda: None,
        approver_id=None if missing else user, caller_id=user, delegation_id=None,
        decision="approved", plan_hash="hash", plan_version=1, plan_digest="digest",
        action_digest="actions", environment_generation="env", execution_identity="identity",
        expires_at=datetime.now(UTC) + timedelta(hours=1), revoked_at=None)
    plan = SimpleNamespace(id=42, company_id=1, plan_hash="hash", version=1, environment_generation="env",
                           action_digest="actions", ensure_one=lambda: None,
                           invalidate_recordset=lambda: None)
    plan.env = ModelEnv({"forgeflow.decision_record": SimpleNamespace(search=lambda *a, **kw: decision)})
    plan._forgeflow_lock = lambda: method("recovery_plan.py", "_forgeflow_lock")(plan)
    plan._forgeflow_approval = lambda **kw: method(
        "recovery_plan.py", "_forgeflow_approval", permission=permission)(plan, **kw)
    return plan


def test_valid_approver_can_reach_execution_gate():
    assert approval_plan()._forgeflow_approval(expected_digest="digest")["execution_identity"] == "identity"


def test_direct_approver_losing_company_scope_cannot_execute_prior_approval():
    plan = approval_plan()
    decision = plan.env["forgeflow.decision_record"].search()
    decision.approver_id.company_ids = [2]
    writes = []
    plan._forgeflow_check_expiry = plan._forgeflow_check_rule_epoch = lambda: None
    plan._forgeflow_digest = lambda: "digest"
    plan._forgeflow_actions = lambda: writes.append("business_boundary")
    with pytest.raises(Denied, match="approval_approver_company_denied"):
        method("recovery_plan.py", "forgeflow_apply_plan")(plan, "retry", "identity")
    assert writes == []


def test_historical_decision_cannot_acquire_the_module_upgraders_identity():
    tree = ast.parse((ADDON / "decision_record.py").read_text())
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "caller_id"
                              for target in node.targets))
    field_options = eval(compile(ast.Expression(assignment.value), "caller_field", "eval"),
                         {"fields": SimpleNamespace(Many2one=lambda *args, **kwargs: kwargs)})
    assert "default" not in field_options and not field_options.get("required")
    plan = approval_plan()
    plan.env["forgeflow.decision_record"].search().caller_id = None
    with pytest.raises(Denied, match="approval_caller_unverified"):
        plan._forgeflow_approval(expected_digest="digest")


@pytest.mark.parametrize("name,args", [("forgeflow_plan_checks", ()),
                                       ("forgeflow_reconcile", ("key",))])
def test_readonly_plan_entrypoints_check_access_before_reading_related_evidence(name, args):
    def deny(operation):
        assert operation == "read"
        raise Denied("plan_read_denied")

    plan = SimpleNamespace(ensure_one=lambda: None, check_access=deny)
    with pytest.raises(Denied, match="plan_read_denied"):
        method("recovery_plan.py", name)(plan, *args)


@pytest.mark.parametrize("allowed,active,missing,code", [
    (False, True, False, "approval_approver_not_permitted"),
    (RuntimeError("lookup failed"), True, False, "approval_permission_unavailable"),
    (True, False, False, "approver_inactive"),
    (True, True, True, "approval_caller_unverified"),
])
def test_denial_and_lookup_failure_never_reach_native_business_writes(allowed, active, missing, code):
    plan = approval_plan(allowed, active=active, missing=missing)
    writes = []
    plan._forgeflow_check_expiry = plan._forgeflow_check_rule_epoch = lambda: None
    plan._forgeflow_digest = lambda: "digest"
    plan._forgeflow_actions = lambda: writes.append("business_boundary")
    with pytest.raises(Denied, match=code):
        method("recovery_plan.py", "forgeflow_apply_plan")(plan, "retry", "identity")
    assert writes == []


@pytest.mark.parametrize("value", [None, True, "true", {}, 1])
def test_rpc_context_cannot_forge_workflow_capability(value):
    token = object()
    check = method("authorization.py", "_forgeflow_require_workflow", _TOKEN=token, _KEY="cap")
    with pytest.raises(Denied, match="forgeflow_workflow_required"):
        check(SimpleNamespace(env=SimpleNamespace(context={"cap": value})))
    check(SimpleNamespace(env=SimpleNamespace(context={"cap": token})))


def test_review_binding_rechecks_actual_actions_and_snapshot():
    action = {"action_id": "a1", "type": "cancel_purchase_order", "target_model": "purchase.order",
              "target_id": 1, "params": {}}
    actions = [action]
    import json
    plan = SimpleNamespace(version=1, action_digest=logic.action_digest(actions),
                           fact_snapshot_hash=logic.digest({"order": 1}),
                           fact_snapshot_json=json.dumps({"order": 1}),
                           _forgeflow_digest=lambda: "plan", _forgeflow_actions=lambda: actions,
                           _forgeflow_loads=json.loads)
    check = method("recovery_plan.py", "_forgeflow_check_review")
    check(plan, 1, "plan", plan.action_digest)
    for version, digest, action_digest in [(2, "plan", plan.action_digest),
                                           (1, "changed", plan.action_digest), (1, "plan", "changed")]:
        with pytest.raises(Denied, match="plan_review_stale"):
            check(plan, version, digest, action_digest)
    plan.fact_snapshot_json = '{"order":2}'
    with pytest.raises(Denied, match="plan_review_stale"):
        check(plan, 1, "plan", plan.action_digest)


def delegation_fixture():
    human, service = User(), User(8)
    now = datetime(2026, 11, 13)
    plan = SimpleNamespace(id=42, company_id=1, version=1, action_digest="actions",
                           _forgeflow_digest=lambda: "plan")
    grant = SimpleNamespace(ensure_one=lambda: None, check_access=lambda op: None,
                            id=11, env=ModelEnv(), invalidate_recordset=lambda: None,
                            plan_id=plan, company_id=1, approver_id=human, service_user_id=service,
                            plan_version=1, plan_digest="plan", action_digest="actions",
                            revoked_at=None, expires_at=now + timedelta(hours=1))
    check = method("approval_delegation.py", "_forgeflow_validate", permission=permission,
                   fields=SimpleNamespace(Datetime=SimpleNamespace(now=lambda: now)))
    return plan, grant, check


def test_delegation_accepts_exact_live_scope():
    plan, grant, check = delegation_fixture()
    check(grant, plan, 7, 8)


@pytest.mark.parametrize("field,value,code", [
    ("plan_version", 2, "scope_mismatch"), ("company_id", 2, "scope_mismatch"),
    ("plan_digest", "other", "scope_mismatch"), ("action_digest", "other", "scope_mismatch"),
    ("plan_id", None, "scope_mismatch"), ("revoked_at", True, "revoked"),
    ("expires_at", datetime(2026, 11, 12), "expired"),
])
def test_delegation_rejects_changed_scope_expiry_and_revocation(field, value, code):
    plan, grant, check = delegation_fixture()
    setattr(grant, field, value)
    with pytest.raises(Denied, match=code):
        check(grant, plan, 7, 8)


@pytest.mark.parametrize("human,caller", [(9, 8), (7, 9)])
def test_delegation_rejects_different_human_or_service(human, caller):
    plan, grant, check = delegation_fixture()
    with pytest.raises(Denied, match="scope_mismatch"):
        check(grant, plan, human, caller)


def test_native_approval_rejects_forged_human_before_creating_decision():
    human, caller = User(7), User(8, False)
    writes = []
    class Env(dict):
        uid = 8
        user = caller
        cr = SimpleNamespace(execute=lambda *a: None)
    env = Env({"res.users": SimpleNamespace(browse=lambda uid: SimpleNamespace(exists=lambda: human)),
               "forgeflow.approval_delegation": None})
    plan = SimpleNamespace(id=42, company_id=1, env=env, ensure_one=lambda: None,
                           _forgeflow_lock=lambda: None,
                           invalidate_recordset=lambda: None,
                           _forgeflow_check_review=lambda *a: None, write=lambda *a: writes.append(a))
    with pytest.raises(Denied, match="approval_delegation_required"):
        method("recovery_plan.py", "forgeflow_approve", permission=permission)(plan, 7, "identity")
    assert writes == []


def test_plan_lock_invalidates_only_after_row_lock_is_acquired():
    events = []
    plan = SimpleNamespace(id=42, ensure_one=lambda: None,
                           env=SimpleNamespace(cr=SimpleNamespace(
                               execute=lambda sql, params: events.append((sql, params)))),
                           invalidate_recordset=lambda: events.append("invalidate"))
    method("recovery_plan.py", "_forgeflow_lock")(plan)
    assert "forgeflow_recovery_plan" in events[0][0] and "FOR UPDATE" in events[0][0]
    assert events[0][1] == (42,) and events[1] == "invalidate"


@pytest.mark.parametrize("name,args", [
    ("forgeflow_apply_plan", ("key", "identity")),
    ("forgeflow_apply_action", ("a1", "key", "identity")),
])
def test_execution_stops_before_approval_and_business_writes_on_lock_conflict(name, args):
    def conflict():
        raise Denied("concurrent_revocation")

    plan = SimpleNamespace(ensure_one=lambda: None, _forgeflow_lock=conflict)
    with pytest.raises(Denied, match="concurrent_revocation"):
        method("recovery_plan.py", name)(plan, *args)


@pytest.mark.parametrize("file", ["decision_record.py", "approval_delegation.py"])
def test_revocation_uses_the_same_plan_lock_before_mutating_evidence(file):
    def conflict():
        raise Denied("execution_in_progress")

    record = SimpleNamespace(ensure_one=lambda: None,
                             plan_id=SimpleNamespace(_forgeflow_lock=conflict))
    with pytest.raises(Denied, match="execution_in_progress"):
        method(file, "forgeflow_revoke")(record)


def test_approval_rechecks_decision_after_lock_invalidates_cached_state():
    plan = approval_plan()
    decision = plan.env["forgeflow.decision_record"].search()
    decision.invalidate_recordset = lambda: setattr(decision, "revoked_at", datetime.now(UTC))
    with pytest.raises(Denied, match="approval_revoked"):
        plan._forgeflow_approval(expected_digest="digest")


def test_delegation_rechecks_revocation_after_lock_invalidates_cached_state():
    plan, grant, check = delegation_fixture()
    statements = []
    grant.env.cr = SimpleNamespace(execute=lambda sql, params: statements.append(sql))
    grant.invalidate_recordset = lambda: setattr(grant, "revoked_at", True)
    with pytest.raises(Denied, match="approval_delegation_revoked"):
        check(grant, plan, 7, 8)
    assert len(statements) == 1 and "FOR SHARE" in statements[0]


@pytest.mark.parametrize("kind", logic.ACTION_TYPES)
def test_mismatched_approved_model_is_rejected_before_any_plan_or_business_write(kind):
    writes = []
    plan = SimpleNamespace(ensure_one=lambda: None, create=lambda *args: writes.append(args))
    action = {"action_id": "forged", "type": kind, "target_model": "res.users",
              "target_id": 7, "params": {}, "quantity": 1, "unit": "Units", "currency": "USD"}
    with pytest.raises(Denied, match="action_target_model_mismatch"):
        method("recovery_plan.py", "forgeflow_prepare")(plan, 1, [action], {}, "env")
    assert writes == []


def test_purchase_action_cannot_name_a_manufacturing_object_with_same_id():
    action = {"action_id": "forged", "type": "cancel_purchase_order",
              "target_model": "mrp.production", "target_id": 7, "params": {}}
    with pytest.raises(logic.PlanError, match="action_target_model_mismatch"):
        logic.execution_order([action])


def native_record(company=1, **values):
    record = SimpleNamespace(company_id=company, _fields={"company_id", "active"},
                             active=True, check_access=lambda op: None, **values)
    record.exists = lambda: record
    return record


@pytest.mark.parametrize("company,shared,allowed", [
    (1, False, True), (2, False, False), (False, False, False),
    (False, True, True), (2, True, False), (1, True, True),
])
def test_plan_company_boundary_does_not_expand_with_multicompany_caller(company, shared, allowed):
    plan = SimpleNamespace(company_id=1)
    check = method("recovery_plan.py", "_forgeflow_check_company")
    if allowed:
        check(plan, native_record(company), allow_shared=shared)
    else:
        with pytest.raises(Denied, match="target_company_mismatch"):
            check(plan, native_record(company), allow_shared=shared)


@pytest.mark.parametrize("handler,wrong_model", [
    ("_forgeflow_create_purchase_order", "res.partner"),
    ("_forgeflow_create_purchase_order", "product.product"),
    ("_forgeflow_create_manufacturing_order", "product.product"),
    ("_forgeflow_create_manufacturing_order", "mrp.bom"),
    ("_forgeflow_create_manufacturing_order", "mrp.workcenter"),
    ("_forgeflow_reserve_stock_locked", "stock.move"),
    ("_forgeflow_reserve_stock_locked", "stock.location"),
    ("_forgeflow_link_supply_to_demand", "sale.order.line"),
])
def test_foreign_company_dependencies_stop_before_native_writes(handler, wrong_model):
    currency = SimpleNamespace(id=1, name="USD")
    company = SimpleNamespace(id=1, currency_id=currency)
    records = {name: native_record(company, id=index, product_tmpl_id=1)
               for index, name in enumerate([
                   "res.partner", "product.product", "mrp.bom", "mrp.workcenter",
                   "stock.move", "stock.location", "sale.order.line"], 1)}
    records[wrong_model].company_id = 2
    env = ModelEnv({name: SimpleNamespace(browse=lambda _, record=record: record)
                    for name, record in records.items()})
    env["res.currency"] = SimpleNamespace(search=lambda *a, **kw: currency)
    writes = []
    env["purchase.order"] = env["mrp.production"] = SimpleNamespace(
        create=lambda values: writes.append(values))
    plan = SimpleNamespace(company_id=company, env=env)
    plan._forgeflow_check_company = lambda record, **kw: method(
        "recovery_plan.py", "_forgeflow_check_company")(plan, record, **kw)
    action = {"target_model": "stock.move", "target_id": 1, "quantity": 1, "currency": "USD",
              "params": {"partner_id": 1, "product_id": 2, "bom_id": 3, "workcenter_id": 4,
                         "move_id": 5, "location_id": 6, "sale_line_id": 7,
                         "lines": [{"product_id": 2, "product_qty": 1}]}}
    with pytest.raises(Denied, match="target_company_mismatch"):
        method("recovery_plan.py", handler)(plan, action)
    assert writes == []


def test_prepare_pins_company_instead_of_inheriting_a_forged_default_context():
    company = SimpleNamespace(id=1)
    exception = native_record(company)
    env = ModelEnv({"forgeflow.exception_event": SimpleNamespace(browse=lambda _: exception),
                    "forgeflow.business_rule": SimpleNamespace(forgeflow_rule_epoch=lambda: "epoch")})
    env.company, env.context = company, {"allowed_company_ids": [1, 2], "default_company_id": 2}
    created = []
    new_plan = SimpleNamespace(id=42, write=lambda vals: None, _forgeflow_digest=lambda: "digest")

    def create(values):
        created.append(values)
        return new_plan

    plan = SimpleNamespace(env=env, ensure_one=lambda: None, create=create,
                           _forgeflow_rule_scope=lambda *args: {})
    action = {"action_id": "cancel", "type": "cancel_purchase_order",
              "target_model": "purchase.order", "target_id": 7, "params": {}}
    method("recovery_plan.py", "forgeflow_prepare", json=json,
           fields=SimpleNamespace(Datetime=SimpleNamespace(now=lambda: datetime.now(UTC))))(
               plan, 1, [action], {}, "env")
    assert created[0]["company_id"] == 1


def test_direct_native_verify_cannot_write_success_without_complete_execution_evidence():
    writes = []
    plan = SimpleNamespace(id=42, state="approved", ensure_one=lambda: None,
                           _forgeflow_lock=lambda: None,
                           _forgeflow_execution_evidence_complete=lambda: False,
                           write=lambda values: writes.append(values))
    result = method("recovery_plan.py", "forgeflow_verify")(plan)
    assert result["status"] == "unknown"
    assert plan.state == "approved" and writes == []


@pytest.mark.parametrize("completeness", [
    {"complete": False}, {"truncated": True}, {"lines_complete": False},
    {"query_failures": ["lines"]}, {"missing_relations": ["stock"]}, "invalid"])
def test_native_approval_cannot_bypass_partial_snapshot_guard(completeness):
    import json
    plan = SimpleNamespace(fact_snapshot_json=json.dumps({"completeness": completeness}),
                           _forgeflow_loads=json.loads)
    with pytest.raises(Denied, match="recovery_plan_snapshot_incomplete"):
        method("recovery_plan.py", "_forgeflow_check_review")(plan, 1, "plan", "actions")


@pytest.mark.parametrize("state", ["awaiting_approval", "approved", "executed"])
def test_independent_readback_never_changes_plan_state(state):
    writes = []
    record = native_record(1, id=7, state="purchase")
    record._fields |= {"id", "state"}
    action = {"action_id": "keep", "type": "preserve_supply", "target_model": "purchase.order",
              "target_id": 7, "params": {}}
    plan = SimpleNamespace(state=state, ensure_one=lambda: None,
                           _forgeflow_executed_actions=lambda: [action],
                           _forgeflow_value=lambda row, field: getattr(row, field),
                           env={"purchase.order": SimpleNamespace(browse=lambda _: record)},
                           write=lambda values: writes.append(values))
    result = method("recovery_plan.py", "forgeflow_readback")(plan)
    assert result["purchase.order:7"]["state"] == "purchase"
    assert plan.state == state and writes == []


@pytest.mark.parametrize("name,args", [
    ("forgeflow_mark_unknown", ("key", "response lost")),
    ("forgeflow_resolve_unknown", ("key", True)),
])
def test_uncertainty_changes_stop_at_plan_lock_conflicts(name, args):
    def conflict():
        raise Denied("plan_lock_conflict")

    plan = SimpleNamespace(ensure_one=lambda: None, _forgeflow_lock=conflict)
    with pytest.raises(Denied, match="plan_lock_conflict"):
        method("recovery_plan.py", name)(plan, *args)


@pytest.mark.parametrize("name,args", [
    ("forgeflow_mark_unknown", ("key", "response lost")),
    ("forgeflow_resolve_unknown", ("key", True)),
])
def test_uncertainty_changes_stop_at_stale_operation_lock_conflicts(name, args):
    events = []
    operation = SimpleNamespace(id=9, write=lambda values: events.append("write"))

    def conflict(sql, params):
        assert "forgeflow_execution_operation" in sql and "FOR UPDATE" in sql
        assert params == (9,)
        raise Denied("operation_snapshot_changed")

    env = ModelEnv({"forgeflow.execution_operation": SimpleNamespace(search=lambda *a, **kw: operation)})
    env.cr = SimpleNamespace(execute=conflict)
    plan = SimpleNamespace(id=42, env=env, ensure_one=lambda: None,
                           _forgeflow_lock=lambda: events.append("plan_lock"))
    with pytest.raises(Denied, match="operation_snapshot_changed"):
        method("recovery_plan.py", name)(plan, *args)
    assert events == ["plan_lock"]


def test_native_verification_aborts_on_stale_operation_row_before_reading_receipts():
    operations = SimpleNamespace(ids=[9])

    def conflict(sql, params):
        assert "forgeflow_execution_operation" in sql and "FOR SHARE" in sql
        assert params == ((9,),)
        raise Denied("operation_snapshot_changed")

    env = ModelEnv({"forgeflow.execution_operation": SimpleNamespace(search=lambda *a, **kw: operations)})
    env.cr = SimpleNamespace(execute=conflict)
    plan = SimpleNamespace(id=42, env=env, execution_key="key")
    with pytest.raises(Denied, match="operation_snapshot_changed"):
        method("recovery_plan.py", "_forgeflow_execution_evidence_complete")(plan)


def test_native_verification_reloads_operation_state_after_acquiring_row_lock():
    row = {"state": "succeeded"}
    operations = SimpleNamespace(ids=[9], read=lambda fields: [row],
                                 invalidate_recordset=lambda: row.update(state="unknown"))
    receipts = SimpleNamespace(read=lambda fields: [])
    env = ModelEnv({"forgeflow.execution_operation": SimpleNamespace(search=lambda *a, **kw: operations),
                    "forgeflow.execution_receipt": SimpleNamespace(search=lambda *a, **kw: receipts)})
    plan = SimpleNamespace(id=42, env=env, execution_key="key", version=1,
                           company_id=SimpleNamespace(id=1), environment_generation="env",
                           _forgeflow_digest=lambda: "digest", _forgeflow_actions=lambda: [])
    gate = SimpleNamespace(execution_evidence_complete=lambda **evidence:
                           evidence["operations"][0]["state"] == "succeeded")
    assert method("recovery_plan.py", "_forgeflow_execution_evidence_complete",
                  recovery_logic=gate)(plan) is False
