"""Server-independent product approval tests with a deterministic fake executor.

The tests deliberately do not start Docker, Odoo or a model.  They prove the
approval semantics that a future real executor must preserve: no apply before
approval, one apply for one plan, owner isolation, private atomic persistence,
and stable 503s when execution is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import Settings, create_settings
from forgeflow.product import (
    AWAITING_APPROVAL,
    EXPIRED,
    FAILED,
    REJECTED,
    SUCCEEDED,
    FakeProductExecutor,
    ProductCaseError,
    ProductCaseService,
)

SECRET = "test-product-session-secret-0123456789"
INVITE_A = "product-invite-owner-a-0001"
INVITE_B = "product-invite-owner-b-0002"
CASE_ID = re.compile(r"[0-9a-f]{32}")
PLAN_ID = re.compile(r"[0-9a-f]{64}")
CASE_KEYS = {
    "case_id",
    "scenario",
    "state",
    "synthetic",
    "order",
    "disruption",
    "plan",
    "approval",
    "execution",
    "created_at",
    "updated_at",
    "reason",
    "decision", "result", "audit", "history", "revision",
}
PLAN_KEYS = {
    "plan_id",
    "actions",
    "margin",
    "on_time",
    "new_spend",
    "checks",
    "cost_delta",
    "costs",
    "policy_sha256",
    "snapshot_sha256",
    "prepared_at",
}
ACTION_KEYS = {"kind", "document", "summary", "quantity", "before", "after"}
EXECUTION_KEYS = {"status", "readback", "verifier", "writes"}
CHECK_KEYS = {"code", "label", "status", "actual", "threshold", "source"}
CHECK_STATUSES = {"pass", "warn", "block", "unknown"}
ASK_KEYS = {"answer", "mode", "citations", "evidence_refs", "case_context"}
CITATION_KEYS = {"field", "label", "value"}
CITATION_FIELDS = {
    "order.ref",
    "order.product",
    "order.units",
    "order.customer",
    "order.due_days",
    "order.due_days_max",
    "disruption.vendor",
    "plan.new_spend",
    "plan.margin",
    "plan.actions",
    "plan.policy_sha256",
    "execution.status",
    "execution.verifier.status",
    "execution.verifier.score",
    "execution.writes",
    "state",
    "reason",
}


def make_settings(root: Path, *, deployment: str = "local") -> Settings:
    return create_settings(
        {
            "FORGEFLOW_DATA_ROOT": str(root / "runs"),
            "FORGEFLOW_APPROVALS_ROOT": str(root / "approvals"),
            "FORGEFLOW_SESSION_SECRET": SECRET,
            "FORGEFLOW_INVITE_CODES": f"{INVITE_A},{INVITE_B}",
            "FORGEFLOW_SECURE_COOKIE": "true",
            "FORGEFLOW_DEPLOYMENT": deployment,
            "FORGEFLOW_PUBLIC_RUNS": "*",
        }
    )


@contextmanager
def client_for(
    root: Path,
    executor: FakeProductExecutor | None,
    *,
    deployment: str = "local",
) -> Iterator[tuple[TestClient, Settings]]:
    settings = make_settings(root, deployment=deployment)
    app = create_app(settings, product_executor=executor)
    with TestClient(app, base_url="https://testserver") as client:
        yield client, settings


def sign_in(client: TestClient, code: str = INVITE_A) -> str:
    response = client.post("/api/auth/session", json={"invite_code": code})
    assert response.status_code == 201, response.text
    return response.json()["uid"]


def create_case(client: TestClient, scenario: str = "A") -> dict:
    response = client.post("/api/product/cases", json={"scenario": scenario})
    assert response.status_code == 201, response.text
    return response.json()


def case_path(settings: Settings, case_id: str) -> Path:
    assert settings.approvals_root is not None
    return settings.approvals_root / "product-cases" / f"{case_id}.json"


def approve(client: TestClient, case_id: str, plan_id: str):
    return client.post(
        f"/api/product/cases/{case_id}/approve", json={"plan_id": plan_id}
    )


def ask(client: TestClient, case_id: str, question: str):
    return client.post(
        f"/api/product/cases/{case_id}/ask", json={"question": question}
    )


def assert_case_answer(body: dict, case: dict) -> None:
    """Check the deterministic CaseFile answer envelope and its citations."""
    assert set(body) == ASK_KEYS
    assert body["case_context"] == {
        "schema_version": 1, "case_id": case["case_id"], "state": case["state"],
        "version": case["revision"]["version"],
        "plan_id": (case.get("plan") or {}).get("plan_id"),
        "comparison_id": None, "selection_revision": None, "selection_pending": False,
        "updated_at": case["updated_at"], "outcome": case["result"]["outcome"],
    }
    assert body["mode"] == "case"
    assert isinstance(body["answer"], str) and body["answer"]
    assert body["citations"]
    serialized = json.dumps(body, ensure_ascii=False)
    assert "uid" not in serialized
    assert "/Users" not in serialized
    assert ".json" not in serialized
    assert "product-cases" not in serialized
    cited: set[str] = set()
    for citation in body["citations"]:
        assert set(citation) == CITATION_KEYS
        assert citation["field"] in CITATION_FIELDS
        assert isinstance(citation["label"], str) and citation["label"]
        assert citation["value"] is None or isinstance(
            citation["value"], (str, int, float)
        )
        assert not isinstance(citation["value"], bool)
        assert citation["value"] == citation_value(case, citation["field"])
        cited.add(citation["field"])
    assert cited


def citation_value(case: dict, field: str) -> object:
    if field == "plan.actions":
        return len(case["plan"]["actions"]) if case.get("plan") else None
    if field == "execution.writes":
        execution = case.get("execution") or {}
        writes = execution.get("writes")
        return len(writes) if isinstance(writes, list) else None
    current: object = case
    for part in field.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def test_create_prepares_a_case_and_awaits_approval(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, settings):
        sign_in(client)
        case = create_case(client)

        assert set(case) == CASE_KEYS
        assert CASE_ID.fullmatch(case["case_id"])
        assert case["scenario"] == "A"
        assert case["state"] == AWAITING_APPROVAL
        assert case["synthetic"] is True
        assert case["approval"] is None
        assert case["execution"] is None
        assert case["reason"] is None
        assert set(case["order"]) == {"ref", "product", "units", "due_days", "due_days_max", "customer"}
        assert set(case["disruption"]) == {"kind", "vendor"}
        assert case["order"]["units"] > 0
        assert case["order"]["due_days"] > 0

        plan = case["plan"]
        assert set(plan) == PLAN_KEYS
        assert PLAN_ID.fullmatch(plan["plan_id"])
        assert [action["kind"] for action in plan["actions"]] == [
            "cancel_po",
            "create_po",
            "adjust_mo",
        ]
        assert all(set(action) == ACTION_KEYS for action in plan["actions"])
        assert plan["on_time"] is True
        assert plan["margin"] > 0
        assert plan["new_spend"] == 420.0
        assert plan["cost_delta"] is None
        assert plan["costs"] is None
        assert plan["checks"] == [
            {
                "code": "margin",
                "label": "Margin floor",
                "status": "pass",
                "actual": "31.4%",
                "threshold": "27.5%",
                "source": "synthetic scenario A",
            },
            {
                "code": "demand",
                "label": "Demand coverage",
                "status": "pass",
                "actual": f"{case['order']['units']}/{case['order']['units']} units",
                "threshold": f"{case['order']['units']} units",
                "source": "synthetic scenario A",
            },
            {
                "code": "budget_policy",
                "label": "Budget & policy",
                "status": "pass",
                "actual": "synthetic budget and policy check",
                "threshold": "synthetic budget and policy",
                "source": "synthetic executor",
            },
            {
                "code": "on_time",
                "label": "On-time delivery",
                "status": "pass",
                "actual": "planned",
                "threshold": "customer due date",
                "source": "synthetic scenario A",
            },
        ]
        assert all(set(check) == CHECK_KEYS for check in plan["checks"])
        assert {check["status"] for check in plan["checks"]} <= CHECK_STATUSES
        assert PLAN_ID.fullmatch(plan["policy_sha256"])
        assert PLAN_ID.fullmatch(plan["snapshot_sha256"])
        assert plan["prepared_at"]
        assert fake.prepare_calls == 1
        assert fake.apply_calls == 0
        assert case_path(settings, case["case_id"]).is_file()

        stored = json.loads(case_path(settings, case["case_id"]).read_text())
        assert stored["uid"]
        assert "uid" not in case


def test_approve_applies_the_exact_plan_once(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        uid = sign_in(client)
        case = create_case(client)
        plan_id = case["plan"]["plan_id"]

        response = approve(client, case["case_id"], plan_id)
        assert response.status_code == 200, response.text
        approved = response.json()

        assert approved["state"] == SUCCEEDED
        assert approved["reason"] is None
        assert approved["plan"] == case["plan"]
        assert approved["approval"]["expires_at"]
        assert "nonce" not in approved["approval"]
        assert "approver" not in approved["approval"]
        assert uid not in json.dumps(approved)
        assert set(approved["execution"]) == EXECUTION_KEYS
        assert approved["execution"]["status"] == "succeeded"
        assert approved["execution"]["verifier"]["status"] == "passed"
        assert len(approved["execution"]["writes"]) == 3
        assert fake.apply_calls == 1
        assert fake.applied_plan_ids == [plan_id]
        assert fake.last_apply_plan["plan_id"] == plan_id


def test_no_apply_happens_before_approval(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)

        assert fake.apply_calls == 0
        prepared = client.post(f"/api/product/cases/{case['case_id']}/prepare")
        assert prepared.status_code == 200
        assert prepared.json()["state"] == AWAITING_APPROVAL
        assert fake.apply_calls == 0


def test_prepare_is_idempotent_and_only_calls_the_executor_once(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)
        plan_id = case["plan"]["plan_id"]

        first = client.post(f"/api/product/cases/{case['case_id']}/prepare")
        second = client.post(f"/api/product/cases/{case['case_id']}/prepare")
        assert first.status_code == second.status_code == 200
        assert first.json()["plan"]["plan_id"] == plan_id
        assert second.json()["plan"]["plan_id"] == plan_id
        assert fake.prepare_calls == 1
        assert fake.apply_calls == 0


def test_approve_before_a_plan_is_rejected(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    service = ProductCaseService(tmp_path / "approvals", fake)
    case = service.create("uid-owner-0001", "A")

    with pytest.raises(ProductCaseError) as error:
        service.approve(case["case_id"], "uid-owner-0001", "f" * 64)
    assert error.value.code == "plan_not_prepared"
    assert error.value.status == 409
    assert fake.apply_calls == 0

    rejected = service.reject(case["case_id"], "uid-owner-0001")
    assert rejected["state"] == REJECTED
    prepared = service.create("uid-owner-0001", "A")
    service.prepare(prepared["case_id"], "uid-owner-0001")
    assert service.reject(prepared["case_id"], "uid-owner-0001")["state"] == REJECTED
    assert error.value.status == 409


def test_unknown_plan_is_rejected_before_apply(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)

        response = approve(client, case["case_id"], "f" * 64)
        assert response.status_code == 409
        assert response.json()["detail"] == "plan_mismatch"
        assert fake.apply_calls == 0
        detail = client.get(f"/api/product/cases/{case['case_id']}").json()
        assert detail["state"] == AWAITING_APPROVAL

        accepted = approve(client, case["case_id"], case["plan"]["plan_id"])
        assert accepted.status_code == 200
        assert accepted.json()["state"] == SUCCEEDED
        assert fake.apply_calls == 1


def test_repeating_the_same_approval_does_not_apply_twice(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)
        plan_id = case["plan"]["plan_id"]

        first = approve(client, case["case_id"], plan_id)
        second = approve(client, case["case_id"], plan_id)
        assert first.status_code == second.status_code == 200
        assert second.json() == first.json()
        assert fake.apply_calls == 1


def test_a_different_plan_cannot_override_an_applied_plan(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        first = create_case(client)
        second = create_case(client)
        assert first["plan"]["plan_id"] != second["plan"]["plan_id"]

        assert approve(client, first["case_id"], first["plan"]["plan_id"]).status_code == 200
        conflict = approve(client, first["case_id"], second["plan"]["plan_id"])
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "plan_already_applied"
        assert fake.apply_calls == 1
        detail = client.get(f"/api/product/cases/{first['case_id']}").json()
        assert detail["state"] == SUCCEEDED
        assert detail["plan"]["plan_id"] == first["plan"]["plan_id"]


def test_cases_are_owner_isolated_in_list_and_detail(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        uid_a = sign_in(client, INVITE_A)
        case_a = create_case(client)
        client.cookies.clear()
        uid_b = sign_in(client, INVITE_B)
        case_b = create_case(client)
        assert uid_a != uid_b

        listed = client.get("/api/product/cases").json()
        assert [item["case_id"] for item in listed] == [case_b["case_id"]]
        assert client.get(f"/api/product/cases/{case_a['case_id']}").status_code == 404
        assert (
            client.post(f"/api/product/cases/{case_a['case_id']}/prepare").status_code
            == 404
        )
        assert (
            approve(client, case_a["case_id"], case_a["plan"]["plan_id"]).status_code
            == 404
        )
        assert fake.apply_calls == 0

        client.cookies.clear()
        sign_in(client, INVITE_A)
        listed = client.get("/api/product/cases").json()
        assert [item["case_id"] for item in listed] == [case_a["case_id"]]
        assert client.get(f"/api/product/cases/{case_a['case_id']}").status_code == 200


def test_review_deployment_returns_stable_503(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake, deployment="review") as (client, settings):
        assert settings.deployment == "review"
        sign_in(client)
        missing = "f" * 32

        created = client.post("/api/product/cases", json={"scenario": "A"})
        assert created.status_code == 503
        assert created.json()["detail"] == "product_execution_unavailable"
        assert client.get("/api/product/cases").status_code == 200
        assert client.get(f"/api/product/cases/{missing}").status_code == 404
        assert (
            client.post(f"/api/product/cases/{missing}/prepare").status_code == 503
        )
        assert (
            approve(client, missing, "f" * 64).status_code == 503
        )
        assert fake.prepare_calls == 0
        assert fake.apply_calls == 0


def test_api_without_an_executor_returns_stable_503(tmp_path: Path) -> None:
    with client_for(tmp_path, None) as (client, _settings):
        sign_in(client)
        missing = "f" * 32

        created = client.post("/api/product/cases", json={"scenario": "A"})
        assert created.status_code == 503
        assert created.json()["detail"] == "product_execution_unavailable"
        assert client.get("/api/product/cases").status_code == 200
        assert client.get(f"/api/product/cases/{missing}").status_code == 404
        assert (
            client.post(f"/api/product/cases/{missing}/prepare").status_code == 503
        )
        assert approve(client, missing, "f" * 64).status_code == 503


def test_prepare_failure_marks_the_case_failed_without_retry(tmp_path: Path) -> None:
    fake = FakeProductExecutor(fail_prepare=True)
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        response = client.post("/api/product/cases", json={"scenario": "A"})
        assert response.status_code == 201
        case = response.json()
        assert case["state"] == FAILED
        assert case["plan"] is None
        assert case["reason"] == "prepare_failed"
        assert fake.prepare_calls == 1

        retry = client.post(f"/api/product/cases/{case['case_id']}/prepare")
        assert retry.status_code == 200
        assert retry.json()["state"] == FAILED
        assert fake.prepare_calls == 1

        response = approve(client, case["case_id"], "f" * 64)
        assert response.status_code == 409
        assert response.json()["detail"] == "plan_not_prepared"
        assert fake.apply_calls == 0


def test_apply_failure_marks_the_case_failed_and_preserves_the_plan(
    tmp_path: Path,
) -> None:
    fake = FakeProductExecutor(fail_apply=True)
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)
        plan = case["plan"]

        response = approve(client, case["case_id"], plan["plan_id"])
        assert response.status_code == 200
        failed = response.json()
        assert failed["state"] == FAILED
        assert failed["reason"] == "apply_failed"
        assert failed["plan"] == plan
        assert set(failed["execution"]) == EXECUTION_KEYS
        assert failed["execution"]["status"] == "failed"
        assert fake.apply_calls == 1

        repeated = approve(client, case["case_id"], plan["plan_id"])
        assert repeated.status_code == 200
        assert repeated.json()["state"] == FAILED
        assert repeated.json()["plan"] == plan
        assert fake.apply_calls == 1


def test_case_state_survives_a_service_reload(tmp_path: Path) -> None:
    first = FakeProductExecutor()
    service = ProductCaseService(tmp_path / "approvals", first)
    created = service.create("uid-reload-0001", "A")
    prepared = service.prepare(created["case_id"], "uid-reload-0001")
    approved = service.approve(
        created["case_id"], "uid-reload-0001", prepared["plan"]["plan_id"]
    )
    assert approved["state"] == SUCCEEDED

    second = FakeProductExecutor()
    reloaded = ProductCaseService(tmp_path / "approvals", second)
    assert reloaded.get(created["case_id"], "uid-reload-0001") == approved
    assert reloaded.list_for("uid-reload-0001") == [approved]
    assert reloaded.list_for("uid-other-0001") == []
    assert reloaded.prepare(created["case_id"], "uid-reload-0001") == approved
    assert (
        reloaded.approve(
            created["case_id"], "uid-reload-0001", prepared["plan"]["plan_id"]
        )
        == approved
    )
    assert first.apply_calls == 1
    assert second.apply_calls == 0


def test_case_files_and_the_store_are_private_and_atomic(tmp_path: Path) -> None:
    service = ProductCaseService(tmp_path / "approvals", FakeProductExecutor())
    created = service.create("uid-private-0001", "A")
    path = service.path_for(created["case_id"])

    assert path.parent == tmp_path / "approvals" / "product-cases"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".*.tmp"))
    assert json.loads(path.read_text())["uid"] == "uid-private-0001"

    service.prepare(created["case_id"], "uid-private-0001")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".*.tmp"))


def test_an_expired_approval_returns_423_and_marks_the_case_expired(
    tmp_path: Path,
) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, settings):
        sign_in(client)
        case = create_case(client)
        path = case_path(settings, case["case_id"])
        record = json.loads(path.read_text())
        record["state"] = AWAITING_APPROVAL
        record["approval"] = {
            "nonce": "a" * 32,
            "expires_at": "2000-01-01T00:00:00+00:00",
            "plan_id": case["plan"]["plan_id"],
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

        response = approve(client, case["case_id"], case["plan"]["plan_id"])
        assert response.status_code == 423
        assert response.json()["detail"] == "approval_expired"
        assert fake.apply_calls == 0

        detail = client.get(f"/api/product/cases/{case['case_id']}").json()
        assert detail["state"] == EXPIRED
        assert detail["reason"] == "approval_expired"


def test_product_routes_require_authentication(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        missing = "f" * 32
        assert client.get("/api/product/cases").status_code == 401
        assert (
            client.post("/api/product/cases", json={"scenario": "A"}).status_code
            == 401
        )
        assert client.get(f"/api/product/cases/{missing}").status_code == 401
        assert (
            client.post(f"/api/product/cases/{missing}/prepare").status_code == 401
        )
        assert approve(client, missing, "f" * 64).status_code == 401
        assert fake.prepare_calls == 0


def test_unknown_and_malformed_case_ids_are_not_found(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        for case_id in ("f" * 32, "not-a-case-id"):
            assert client.get(f"/api/product/cases/{case_id}").status_code == 404
            assert (
                client.post(f"/api/product/cases/{case_id}/prepare").status_code
                == 404
            )
            assert approve(client, case_id, "f" * 64).status_code == 404


def test_unsupported_scenario_is_rejected(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        response = client.post("/api/product/cases", json={"scenario": "B"})
        assert response.status_code == 409
        assert response.json()["detail"] == "product_scenario_unavailable"
        assert fake.prepare_calls == 0


def test_capabilities_report_the_injected_product_executor(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        body = client.get("/api/capabilities").json()
        assert body["product_approval"] is True
        assert body["live_prepare"] is False
        assert body["live_execute"] is False
        assert body["reason"] == "synthetic_executor"
        assert body["product_scenarios"] == [{
            "id": "A", "task": "2280_medium_repair_plan_medium",
            "label": "Supplier cancellation", "can_create": True,
        }]


class _InlineExecutor:
    """Minimal executor used to pin plan normalization edge cases."""

    live = False

    def __init__(self, plan: dict) -> None:
        self.plan = plan

    def prepare(self, case: dict) -> dict:
        return json.loads(json.dumps(self.plan))

    def apply(self, case: dict, plan: dict) -> dict:
        raise AssertionError("apply must not be called")


def test_legacy_plans_normalize_to_null_new_spend_and_empty_checks(
    tmp_path: Path,
) -> None:
    legacy_plan = {
        "plan_id": "a" * 64,
        "actions": [],
        "margin": 30.0,
        "on_time": True,
        "cost_delta": 12.5,
    }
    service = ProductCaseService(tmp_path / "approvals", _InlineExecutor(legacy_plan))
    created = service.create("uid-legacy-0001", "A")
    prepared = service.prepare(created["case_id"], "uid-legacy-0001")

    assert set(prepared["plan"]) == PLAN_KEYS
    assert prepared["plan"]["new_spend"] is None
    assert prepared["plan"]["checks"] == []
    assert prepared["plan"]["costs"] is None
    assert prepared["plan"]["cost_delta"] == 12.5


def test_new_spend_only_plans_leave_unmeasured_cost_delta_unknown(tmp_path: Path) -> None:
    new_plan = {
        "plan_id": "b" * 64,
        "actions": [],
        "margin": 30.0,
        "on_time": True,
        "new_spend": 77.25,
    }
    service = ProductCaseService(tmp_path / "approvals", _InlineExecutor(new_plan))
    created = service.create("uid-new-spend-0001", "A")
    prepared = service.prepare(created["case_id"], "uid-new-spend-0001")

    assert prepared["plan"]["new_spend"] == 77.25
    assert prepared["plan"]["cost_delta"] is None
    assert prepared["plan"]["checks"] == []


def test_an_invalid_check_status_fails_preparation(tmp_path: Path) -> None:
    bad_plan = {
        "plan_id": "c" * 64,
        "actions": [],
        "margin": 30.0,
        "on_time": True,
        "new_spend": 1.0,
        "checks": [
            {
                "code": "margin",
                "label": "Margin floor",
                "status": "maybe",
                "actual": "30%",
                "threshold": "27.5%",
                "source": "test",
            }
        ],
    }
    service = ProductCaseService(tmp_path / "approvals", _InlineExecutor(bad_plan))
    created = service.create("uid-invalid-check-0001", "A")
    prepared = service.prepare(created["case_id"], "uid-invalid-check-0001")
    assert prepared["state"] == FAILED
    assert prepared["reason"] == "invalid_plan"
    assert prepared["plan"] is None


def test_ask_answers_impact_plan_and_execution_from_the_case_file(
    tmp_path: Path,
) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)
        case_id = case["case_id"]

        impact = ask(client, case_id, "What is the impact of this supplier disruption?")
        assert impact.status_code == 200, impact.text
        impact_body = impact.json()
        assert_case_answer(impact_body, case)
        assert "SO-1..5" in impact_body["answer"]
        assert "50 units" in impact_body["answer"]
        assert "Stonewall Components" in impact_body["answer"]
        assert {c["field"] for c in impact_body["citations"]} == {
            "order.ref",
            "order.product",
            "order.units",
            "order.customer",
            "order.due_days",
            "order.due_days_max",
            "disruption.vendor",
        }

        plan = ask(
            client, case_id, "What does the plan change and which policy binds it?"
        )
        assert plan.status_code == 200, plan.text
        plan_body = plan.json()
        assert_case_answer(plan_body, case)
        assert "$420.00" in plan_body["answer"]
        assert "31.4%" in plan_body["answer"]
        assert "Historical margin" in plan_body["answer"]
        assert "27.5%" not in plan_body["answer"]
        assert "policy" in plan_body["answer"]
        assert {c["field"] for c in plan_body["citations"]} == {
            "plan.new_spend",
            "plan.margin",
            "plan.actions",
            "plan.policy_sha256",
        }

        before = ask(client, case_id, "Did execution pass the verifier?")
        assert before.status_code == 200, before.text
        before_body = before.json()
        assert_case_answer(before_body, case)
        assert "waiting for your decision" in before_body["answer"]
        assert "no verifier result" in before_body["answer"]
        assert before_body["citations"] == [{
            "field": "state",
            "label": "Case state",
            "value": "awaiting_approval",
        }]

        approved = approve(client, case_id, case["plan"]["plan_id"])
        assert approved.status_code == 200, approved.text
        approved_case = approved.json()

        after = ask(client, case_id, "Did execution pass the verifier?")
        assert after.status_code == 200, after.text
        after_body = after.json()
        assert_case_answer(after_body, approved_case)
        assert "succeeded" in after_body["answer"]
        assert "passed" in after_body["answer"]
        assert "3 simulated document changes" in after_body["answer"]
        assert "document entries are not ERP transactions" in after_body["answer"]
        assert {c["field"] for c in after_body["citations"]} == {
            "execution.status",
            "execution.verifier.status",
            "execution.writes",
            "state",
        }

        detail = client.get(f"/api/product/cases/{case_id}").json()
        repeated = ask(client, case_id, "Did execution pass the verifier?")
        assert repeated.json() == after_body
        assert client.get(f"/api/product/cases/{case_id}").json() == detail
        assert fake.prepare_calls == 1
        assert fake.apply_calls == 1


def test_ask_answers_in_chinese_and_keeps_citation_labels_localized(
    tmp_path: Path,
) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)
        case_id = case["case_id"]

        impact = ask(client, case_id, "这个中断的影响是什么？")
        assert impact.status_code == 200, impact.text
        impact_body = impact.json()
        assert_case_answer(impact_body, case)
        assert "全案规划范围 SO-1..5" in impact_body["answer"]
        assert "直接关联订单和失效供给数量未知" in impact_body["answer"]
        assert "50 件" in impact_body["answer"]
        assert "Stonewall Components" in impact_body["answer"]
        assert {c["label"] for c in impact_body["citations"]} >= {
            "订单", "产品", "数量", "客户", "剩余天数", "供应商"
        }

        plan = ask(client, case_id, "计划会改什么，政策是什么？")
        assert plan.status_code == 200, plan.text
        plan_body = plan.json()
        assert_case_answer(plan_body, case)
        assert "方案规划成本" in plan_body["answer"]
        assert "$420.00" in plan_body["answer"]
        assert "毛利率" in plan_body["answer"]
        assert "预算/政策：通过" in plan_body["answer"]

        approved = approve(client, case_id, case["plan"]["plan_id"])
        assert approved.status_code == 200, approved.text
        execution = ask(client, case_id, "执行和验收结果如何？")
        assert execution.status_code == 200, execution.text
        execution_body = execution.json()
        assert_case_answer(execution_body, approved.json())
        assert "执行结束" in execution_body["answer"]
        assert "成功" in execution_body["answer"]
        assert "通过" in execution_body["answer"]
        assert {c["label"] for c in execution_body["citations"]} >= {
            "执行状态", "验收", "单据变更记录数", "案件状态"
        }


def test_ask_unknown_question_falls_back_to_the_case_state(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)
        case_id = case["case_id"]

        english = ask(client, case_id, "How do I bake bread?")
        assert english.status_code == 200, english.text
        english_body = english.json()
        assert_case_answer(english_body, case)
        assert "three deterministic questions" in english_body["answer"]
        assert "impact, plan and policy" in english_body["answer"]
        assert english_body["citations"][0]["field"] == "state"

        chinese = ask(client, case_id, "今天天气怎么样？")
        assert chinese.status_code == 200, chinese.text
        chinese_body = chinese.json()
        assert_case_answer(chinese_body, case)
        assert "三类问题" in chinese_body["answer"]
        assert "影响、计划与政策" in chinese_body["answer"]
        assert fake.prepare_calls == 1
        assert fake.apply_calls == 0


def test_ask_is_state_appropriate_when_plan_preparation_failed(
    tmp_path: Path,
) -> None:
    fake = FakeProductExecutor(fail_prepare=True)
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client)
        case = create_case(client)
        assert case["state"] == FAILED
        case_id = case["case_id"]

        plan = ask(client, case_id, "What does the plan change and which policy binds it?")
        assert plan.status_code == 200, plan.text
        plan_body = plan.json()
        assert_case_answer(plan_body, case)
        assert "plan preparation failed" in plan_body["answer"]
        assert {c["field"] for c in plan_body["citations"]} == {"state", "reason"}

        execution = ask(client, case_id, "Did execution pass the verifier?")
        assert execution.status_code == 200, execution.text
        execution_body = execution.json()
        assert_case_answer(execution_body, case)
        assert "plan preparation failed" in execution_body["answer"]
        assert {c["field"] for c in execution_body["citations"]} == {"state", "reason"}
        assert fake.apply_calls == 0


def test_ask_is_owner_isolated_and_validates_case_ids(tmp_path: Path) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        sign_in(client, INVITE_A)
        case_a = create_case(client)
        client.cookies.clear()
        sign_in(client, INVITE_B)

        for case_id in (case_a["case_id"], "f" * 32, "not-a-case-id"):
            response = ask(client, case_id, "What is the impact?")
            assert response.status_code == 404
            assert response.json()["detail"] == "case_not_found"

        client.cookies.clear()
        sign_in(client, INVITE_A)
        assert ask(
            client, case_a["case_id"], "What is the impact?"
        ).status_code == 200
        assert fake.prepare_calls == 1
        assert fake.apply_calls == 0


def test_ask_requires_authentication_and_validates_question_length(
    tmp_path: Path,
) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake) as (client, _settings):
        missing = "f" * 32
        assert ask(client, missing, "What is the impact?").status_code == 401

        sign_in(client)
        case = create_case(client)
        case_id = case["case_id"]
        for question in ("", "x", "x" * 501):
            response = ask(client, case_id, question)
            assert response.status_code == 422, response.text
        assert ask(client, case_id, "hi").status_code == 200
        assert ask(client, case_id, "x" * 500).status_code == 200
        body = client.post(f"/api/product/cases/{case_id}/ask", json={})
        assert body.status_code == 422


def test_ask_returns_stable_503_when_product_execution_is_unavailable(
    tmp_path: Path,
) -> None:
    fake = FakeProductExecutor()
    with client_for(tmp_path, fake, deployment="review") as (client, _settings):
        sign_in(client)
        missing = "f" * 32
        response = ask(client, missing, "What is the impact?")
        assert response.status_code == 404
        assert response.json()["detail"] == "case_not_found"
        assert fake.prepare_calls == 0
        assert fake.apply_calls == 0

    with client_for(tmp_path, None) as (client, _settings):
        sign_in(client)
        missing = "f" * 32
        response = ask(client, missing, "What is the impact?")
        assert response.status_code == 404
        assert response.json()["detail"] == "case_not_found"
