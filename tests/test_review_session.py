"""Local review-session boundaries with synthetic records and temporary operation stores."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.api.app import create_app
from forgeflow.api.settings import Settings
from forgeflow.investigation.cases import CaseError
from forgeflow.investigation.fixtures import FixtureConnection, development_cases
from forgeflow.investigation.review_session import ReviewSession, event_from_text
from forgeflow.investigation.rules import run_rules
from forgeflow.investigation.sources import OdooSource
from forgeflow.recovery.storage import atomic_json, read_json


class Environment:
    def __init__(self, records):
        self.identity = "generation-one"
        self.connection = FixtureConnection(records)

    def generation(self):
        return self.identity

    def source_factory(self, contract):
        return OdooSource(self.connection, contract)

    def connection_factory(self):
        return self.connection

    def currency_probe(self, _template):
        return {"currency": "USD"}


class Product:
    def __init__(self, root):
        self.root = root
        self.bindings = []

    def create_from_handoff(self, uid, binding):
        assert binding["owner"] == uid
        self.bindings.append(binding)

    def prepare(self, case_id, uid):
        assert any(b["product_case_id"] == case_id and b["owner"] == uid for b in self.bindings)
        return {"state": "awaiting_approval"}


def make(tmp_path, bundle=None, *, fixed=None, expiry=None):
    fixture = next(c for c in development_cases() if c.case_id == "dev-03-offer-identity")
    event = event_from_text("Fixture old vendor cancelled supply.")
    notice = {
        "notice_id": "supplier-record",
        "source": "registered-fixture",
        "event_sha256": event["sha256"],
        "purchase_line_id": 1,
        "text": "Fixture old vendor cancelled purchase order PO-CANCEL.",
    }
    contract = replace(fixture.policy, expires_at_utc=expiry) if expiry else fixture.policy
    worker = SimpleNamespace(
        image="offline", config=SimpleNamespace(model="none", base_url="none"), bundle=bundle
    )
    arguments = dict(
        event=event,
        notices=[notice],
        environment=Environment(fixture.records),
        worker=worker,
        contract=contract,
        product=Product(tmp_path / "product"),
        fixed_investigation=fixed,
    )
    return ReviewSession(tmp_path / "session", **arguments), arguments


def investigate(session):
    return {
        "operation_id": "initial",
        "kind": "investigate",
        "event_sha256": session.event["sha256"],
        "registration_sha256": session.view("owner")["registration_sha256"],
        "text": "Which commitment was cancelled?",
    }


def clarify(session):
    return {
        "operation_id": "reply",
        "kind": "clarify",
        "event_sha256": session.event["sha256"],
        "registration_sha256": session.view("owner")["registration_sha256"],
        "previous": "initial",
        "notice_id": "supplier-record",
        "purchase_line_id": 1,
        "text": "Use the registered notice.",
    }


def run_and_wait(session, intent, monkeypatch, before_worker=None):
    complete = Event()
    original = session._run

    def watched(*args):
        try:
            if before_worker:
                before_worker()
            original(*args)
        finally:
            complete.set()

    with monkeypatch.context() as patch:
        patch.setattr(session, "_run", watched)
        session.submit("owner", intent)
        assert complete.wait(15)
    return session.get("owner", intent["operation_id"])


def test_duplicate_threads_and_restart_never_redispatch_or_switch_owner(tmp_path, monkeypatch):
    session, arguments = make(tmp_path)
    entered, release, finished = Event(), Event(), Event()
    counts = []

    def deferred(uid, path, intent, grant):
        counts.append(intent["operation_id"])
        entered.set()
        try:
            assert release.wait(8)
            atomic_json(
                path / "result.json",
                {"status": "failed", "detail": "example", "usage": {k: 0 for k in grant}},
            )
        finally:
            finished.set()

    monkeypatch.setattr(session, "_run", deferred)
    request = investigate(session)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, again = [
                p.result()
                for p in [pool.submit(session.submit, "owner", request) for _ in range(2)]
            ]
        assert entered.wait(5)
        assert first["status"] == again["status"] == "running"
        with pytest.raises(CaseError, match="operation_input_conflict"):
            session.submit("owner", {**request, "text": "New request with reused identity."})
        restarted = ReviewSession(tmp_path / "session", **arguments)
        assert restarted.submit("owner", request)["status"] == "unknown"
        with pytest.raises(CaseError, match="review_turn_unresolved"):
            restarted.submit("owner", {**request, "operation_id": "another"})
        for read_or_submit in [
            lambda: session.view("other"),
            lambda: session.get("other", "initial"),
            lambda: restarted.submit("other", request),
        ]:
            with pytest.raises(CaseError, match="review_workspace_unavailable") as error:
                read_or_submit()
            assert error.value.status == 403
    finally:
        release.set()
        assert finished.wait(5)
    assert restarted.submit("owner", request)["detail"] == "example"
    assert counts == ["initial"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [("expiry", "review_policy_expired"), ("generation", "environment_generation_changed")],
)
def test_admission_rejects_expired_or_replaced_registration_before_any_dispatch(
    tmp_path, change, reason
):
    session, _ = make(tmp_path, expiry="2001-01-01T00:00:00+00:00" if change == "expiry" else None)
    if change == "generation":
        session.environment.identity = "different"
    with pytest.raises(CaseError, match=reason):
        session.submit("owner", investigate(session))
    assert not (session.root / "session.json").exists()
    assert not (session.root / "turns").exists()


def test_http_requires_session_and_forbids_claiming_a_notice_or_business_authority_in_payload(
    tmp_path,
):
    session, _ = make(tmp_path)
    settings = Settings(
        data_root=tmp_path / "data",
        approvals_root=tmp_path / "approvals",
        session_secret="s" * 32,
        invite_codes=("local-review-invite",),
        secure_cookie=False,
    )
    app = create_app(settings, review_session_factory=lambda _: session)
    with TestClient(
        app, base_url="http://localhost", headers={"origin": "http://localhost"}
    ) as client:
        assert client.get("/api/config").json()["workbenches"] == {"investigation": False, "recovery_review": True}
        root = "/api/recovery-review/session"
        retry_url = root + "/retry"
        retry_body = {
            "operation_id": "retry",
            "previous": "initial",
            "event_sha256": session.event["sha256"],
            "registration_sha256": session.view("owner")["registration_sha256"],
        }
        assert client.post(retry_url, json=retry_body).status_code == 401
        assert client.get(root).status_code == 401
        assert (
            client.post(
                "/api/auth/session", json={"invite_code": "local-review-invite"}
            ).status_code
            == 201
        )
        current = client.get(root).json()
        assert current["notices"] == []
        retry_body["registration_sha256"] = current["registration_sha256"]

        for extra in (
            {"text": "A new question"},
            {"notice_id": "a different source"},
            {"approval": True},
        ):
            assert client.post(retry_url, json={**retry_body, **extra}).status_code == 422
        assert (
            client.post(retry_url, json=retry_body).json()["detail"] == "review_retry_unavailable"
        )
        request = clarify(session)
        body = {k: v for k, v in request.items() if k not in {"kind", "text"}}
        body["registration_sha256"] = current["registration_sha256"]
        for extra in ({"text": "This is what the supplier said."}, {"approval": True}):
            assert client.post(root + "/clarify", json={**body, **extra}).status_code == 422
        assert (
            client.post(root + "/clarify", json=body).json()["detail"]
            == "review_clarification_stale"
        )
        assert not (session.root / "turns").exists()


def fixed(prompt, dispatch):
    return run_rules(prompt.splitlines()[0].removeprefix("Registered supplier notice: "), dispatch)


def test_open_ambiguity_forbids_early_drafts_and_requires_matching_registered_evidence(
    tmp_path, real_bundle, monkeypatch
):
    def try_draft(prompt, dispatch):
        for action in ("validate", "prepare"):
            assert dispatch(action, {}) == {
                "status": "tool_error",
                "code": "clarification_required",
            }
        fixed(prompt, dispatch)

    session, _ = make(tmp_path, real_bundle, fixed=try_draft)
    result = run_and_wait(session, investigate(session), monkeypatch)
    assert result["status"] == "awaiting_clarification"
    assert sorted(row["purchase_line_id"] for row in result["commitments"]) == ["1", "2"]
    assert all(row["purchase_evidence"] and row["line_evidence"] for row in result["commitments"])
    assert read_json(session._path("initial") / "store/investigation/state.json")["drafts"] == []
    for change, reason in [
        ({"notice_id": "rumor"}, "review_notice_unverified"),
        ({"purchase_line_id": 2}, "review_notice_line_mismatch"),
        ({"previous": "wrong-turn"}, "review_clarification_stale"),
    ]:
        with pytest.raises(CaseError, match=reason):
            session.submit("owner", {**clarify(session), **change})
    assert session.view("owner")["turns"] == [result]
    assert not session.product.bindings
    assert session.environment.connection.write_attempts == []


@pytest.mark.parametrize("stage", ["worker", "validate", "prepare", "handoff"])
def test_registered_supply_line_that_is_replaced_after_admission_cannot_reach_handoff(
    tmp_path, real_bundle, monkeypatch, stage
):
    replaced = []

    def replace_line():
        if replaced:
            return
        # A different row now backs the same named PO, product, units and price.
        rows = session.environment.connection.records
        rows["purchase.order.line"][0]["id"] = 99
        rows["purchase.order"][0]["order_line"] = [99]
        rows["stock.move"][0]["purchase_line_id"] = 99
        replaced.append(True)

    def investigation(prompt, dispatch):
        if "cancelled purchase order PO-CANCEL." not in prompt:
            return fixed(prompt, dispatch)

        def dispatch_with_source_update(action, payload):
            if action == stage:
                replace_line()
            return dispatch(action, payload)

        result = fixed(prompt, dispatch_with_source_update)
        if stage == "handoff":
            replace_line()
        return result

    session, _ = make(tmp_path, real_bundle, fixed=investigation)
    assert (
        run_and_wait(session, investigate(session), monkeypatch)["status"]
        == "awaiting_clarification"
    )
    result = run_and_wait(
        session,
        clarify(session),
        monkeypatch,
        before_worker=replace_line if stage == "worker" else None,
    )
    assert replaced and result["status"] != "completed"
    assert session.product.bindings == []
    assert session.environment.connection.write_attempts == []


@pytest.mark.parametrize(("bound_id", "prepare_dispatched"), [(1, False), ("1", False), (1, True)])
def test_fact_query_accepts_the_bound_record_in_both_supported_id_forms(
    tmp_path, real_bundle, monkeypatch, bound_id, prepare_dispatched
):
    tool_results = []

    def check_record(_prompt, dispatch):
        for other in (True, 1.0, 2, "2", "01"):
            denied = dispatch(
                "query", {"kind": "record", "arguments": {"entity": "sales_order", "id": other}}
            )
            assert denied.get("code") == "bound_order_record_required"
        for action in ("validate", "prepare"):
            assert dispatch(action, {})["code"] == "read_only_review"
        tool_results.append(
            dispatch(
                "query", {"kind": "record", "arguments": {"entity": "sales_order", "id": bound_id}}
            )
        )

    session, _ = make(tmp_path, real_bundle, fixed=check_record)
    held = []

    def get(case_id, uid):
        return (
            {"handoff": {"review_profile": "recovery-review-v1", "generation": session.generation}}
            if uid == "owner" and case_id == "my-case"
            else None
        )

    def check(case_id, uid, operation_id, **kwargs):
        assert held and (case_id, uid) == ("my-case", "owner")
        assert kwargs.get("return_prepare") is True
        return {
            "reviews": [
                {
                    "operation_id": operation_id,
                    "status": "corrected" if prepare_dispatched else "retained",
                    "evidence": {"id": 1, "current_value": "2026-01-09 12:00:00"},
                }
            ]
        }, prepare_dispatched

    monkeypatch.setattr(session.product, "get", get, raising=False)
    monkeypatch.setattr(
        session.product,
        "open_review",
        lambda *args, **kwargs: held.append((args, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(session.product, "check_review", check, raising=False)
    intent = {
        "kind": "fact",
        "operation_id": "check-due",
        "case_id": "my-case",
        "order_id": 1,
        "plan_id": "a" * 64,
        "expected_version": 1,
        "text": "Check this order's due date.",
    }
    result = run_and_wait(session, intent, monkeypatch)
    assert tool_results and tool_results[0]["records"][0]["id"] == "1"
    assert result["status"] == "completed" and result["observation"]["record"]["id"] == "1"
    assert result["usage"]["prepares"] == (1 if prepare_dispatched else 0)
    assert len(held) == 1 and session.product.bindings == []
    assert session.environment.connection.write_attempts == []


def test_cross_case_fact_does_not_seize_unrelated_product_or_open_objection(tmp_path, monkeypatch):
    session, _ = make(tmp_path)
    monkeypatch.setattr(
        session.product,
        "get",
        lambda case, uid: {
            "handoff": {"review_profile": "recovery-review-v1", "generation": "other-generation"}
        },
        raising=False,
    )
    req = {
        "operation_id": "check",
        "kind": "fact",
        "case_id": "other-case",
        "order_id": 1,
        "plan_id": "b" * 64,
        "expected_version": 1,
        "text": "Check the other case.",
    }
    with pytest.raises(CaseError, match="review_case_mismatch"):
        session.submit("owner", req)
    assert not (session.root / "turns").exists()


def test_expiry_after_admission_blocks_handoff_even_when_the_investigator_finishes(
    tmp_path, real_bundle, monkeypatch
):
    # Use a day transition in the host's injected test clock, independent of system time.
    from datetime import UTC, datetime

    from forgeflow.investigation import review_session

    class Clock(datetime):
        day = 1

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, cls.day, tzinfo=UTC)

    def run_then_expire(prompt, dispatch):
        fixed(prompt, dispatch)
        Clock.day = 3

    monkeypatch.setattr(review_session, "datetime", Clock)
    session, _ = make(
        tmp_path, real_bundle, fixed=run_then_expire, expiry="2026-01-02T00:00:00+00:00"
    )
    result = run_and_wait(session, investigate(session), monkeypatch)
    assert result["status"] == "failed" and result["detail"] == "review_policy_expired"
    assert session.product.bindings == []


def test_registered_notice_advances_only_matching_scope_without_granting_execution(
    tmp_path, real_bundle, monkeypatch
):
    session, _ = make(tmp_path, real_bundle, fixed=fixed)
    initial = run_and_wait(session, investigate(session), monkeypatch)
    assert initial["status"] == "awaiting_clarification" and not session.product.bindings
    result = run_and_wait(session, clarify(session), monkeypatch)
    assert result["status"] == "completed"
    assert [r["purchase_line_id"] for r in result["commitments"]] == ["1"]
    [binding] = session.product.bindings
    assert binding["contract"] == session.contract.payload()
    assert binding["request"]["disruption"]["offer_key"] == "V-OLD|FIN|1|60.0|1"
    assert not session.environment.connection.write_attempts
    assert session.submit("owner", clarify(session)) == result
    assert session.product.bindings == [binding]


def test_interrupted_model_keeps_unknown_usage_and_cannot_retry_with_fresh_id(
    tmp_path, real_bundle, monkeypatch
):
    session, _ = make(tmp_path, real_bundle)
    calls = []

    def lost_response(**submission):
        calls.append(submission["binding"]["operation"])
        raise TimeoutError

    monkeypatch.setattr(session.worker, "_model", lost_response, raising=False)
    once = run_and_wait(session, investigate(session), monkeypatch)
    assert once["status"] == "unknown" and once["usage"] is None
    assert session.submit("owner", investigate(session)) == once
    with pytest.raises(CaseError, match="review_turn_unresolved"):
        session.submit("owner", {**investigate(session), "operation_id": "try-again"})
    with pytest.raises(CaseError, match="review_turn_unresolved"):
        session.submit("owner", retry(session, "initial", "try-again"))
    assert calls == ["initial"] and session.product.bindings == []


def retry(session, previous, op):
    return {
        "kind": "retry",
        "operation_id": op,
        "previous": previous,
        "event_sha256": session.event["sha256"],
        "registration_sha256": session.view("owner")["registration_sha256"],
    }


@pytest.mark.parametrize("kind", ["investigate", "clarify"])
def test_preflight_retry_replays_the_original_bound_turn_with_shared_budget(
    tmp_path, real_bundle, monkeypatch, kind
):
    from forgeflow.investigation import review_session
    from forgeflow.product.executor import ProductExecutionError

    calls = []

    def counted(prompt, dispatch):
        calls.append(prompt)
        return fixed(prompt, dispatch)

    session, arguments = make(tmp_path, real_bundle, fixed=counted)
    if kind == "clarify":
        assert (
            run_and_wait(session, investigate(session), monkeypatch)["status"]
            == "awaiting_clarification"
        )
    original = investigate(session) if kind == "investigate" else clarify(session)
    original_op = original["operation_id"]
    tick = [1.0]
    monkeypatch.setattr(review_session, "time", SimpleNamespace(monotonic=lambda: tick[0]))
    monkeypatch.setattr(
        review_session,
        "CaseLimits",
        lambda: SimpleNamespace(
            tokens=6_000_000, seconds=100, queries=240, validations=72, prepares=9
        ),
    )

    def unavailable(_):
        tick[0] += 7
        raise ProductExecutionError("environment_read_unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(session.environment, "source_factory", unavailable)
        failed = run_and_wait(session, original, monkeypatch)
    assert failed["dispatch_state"] == "not_dispatched" and failed["retries_remaining"] == 2
    assert failed["usage"]["seconds"] == 7 and failed["usage"]["tokens"] == 0
    assert len(calls) == (kind == "clarify")
    restarted = ReviewSession(session.root, **arguments)
    assert restarted.submit("owner", original) == failed
    request = retry(session, original_op, "resume")
    with pytest.raises(CaseError, match="review_event_mismatch"):
        restarted.submit("owner", {**request, "event_sha256": "0" * 64})
    with pytest.raises(CaseError, match="review_workspace_unavailable"):
        restarted.submit("stranger", request)
    if kind == "clarify":
        with pytest.raises(CaseError, match="review_clarification_stale"):
            restarted.submit("owner", {**original, "operation_id": "fresh"})
    resumed = run_and_wait(restarted, request, monkeypatch)
    assert resumed["status"] == ("awaiting_clarification" if kind == "investigate" else "completed")
    assert restarted.get("owner", original_op) == failed
    assert len(calls) == 1 + (kind == "clarify")
    assert resumed["retry_of"] == original_op
    saved = read_json(restarted._path("resume") / "intent.json")
    assert saved == {
        **{key: value for key, value in original.items() if key != "registration_sha256"},
        "operation_id": "resume",
        "retry_of": original_op,
        "retry_count": 1,
    }
    why = read_json(restarted._path("resume") / "model-task.json")
    assert why["grant"]["seconds"] == 100 - sum(
        t["usage"]["seconds"] for t in restarted.view("owner")["turns"][:-1]
    )
    assert restarted.submit("owner", request) == resumed
    for previous in [original_op, "resume"]:
        with pytest.raises(CaseError, match="review_retry_unavailable"):
            restarted.submit("owner", retry(restarted, previous, "another"))


def test_concurrent_retry_preserves_original_attempts_and_exhausts_bound(tmp_path, monkeypatch):
    session, _ = make(tmp_path)
    entered, release, done = Event(), Event(), Event()
    failing_reads = []

    def unavailable(_):
        failing_reads.append(1)
        if len(failing_reads) == 2:
            entered.set()
            assert release.wait(8)
        raise TimeoutError

    monkeypatch.setattr(session.environment, "source_factory", unavailable)
    failed = run_and_wait(session, investigate(session), monkeypatch)
    assert failed["dispatch_state"] == "not_dispatched"
    request = retry(session, "initial", "resume")
    original_run = session._run

    def waitable(*args):
        try:
            original_run(*args)
        finally:
            done.set()

    with monkeypatch.context() as patch:
        patch.setattr(session, "_run", waitable)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                requests = [pool.submit(session.submit, "owner", request) for _ in range(2)]
                assert all(f.result()["status"] == "running" for f in requests)
            assert entered.wait(5)
            with pytest.raises(CaseError, match="review_turn_unresolved"):
                session.submit("owner", retry(session, "initial", "race"))
        finally:
            release.set()
            assert done.wait(5)
    assert session.get("owner", "resume")["retries_remaining"] == 1
    twice = run_and_wait(session, retry(session, "resume", "last"), monkeypatch)
    assert twice["retries_remaining"] == 0
    for previous in ["initial", "resume", "last"]:
        with pytest.raises(CaseError, match="review_retry_unavailable"):
            session.submit("owner", retry(session, previous, "extra"))
    assert [t["operation_id"] for t in session.view("owner")["turns"]] == [
        "initial",
        "resume",
        "last",
    ]
    assert len(failing_reads) == 3


@pytest.mark.parametrize("boundary", ["model", "before_read", "return_read"])
@pytest.mark.parametrize("change", ["expiry", "generation"])
def test_authority_checked_at_model_and_observation_boundaries(
    tmp_path, real_bundle, monkeypatch, boundary, change
):
    from datetime import UTC, datetime

    from forgeflow.investigation import review_session

    class Clock(datetime):
        day = 1

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, cls.day, tzinfo=UTC)

    def invalidate():
        if change == "generation":
            session.environment.identity = "changed"
        else:
            Clock.day = 3

    model_calls, reads, responses = [], [], []
    request = {"kind": "record", "arguments": {"entity": "sales_order", "id": 1}}

    def observer(_prompt, dispatch):
        model_calls.append(True)
        if boundary == "before_read":
            invalidate()
        responses.append(dispatch("query", request))

    session, _ = make(tmp_path, real_bundle, fixed=observer, expiry="2026-01-02T00:00:00+00:00")
    monkeypatch.setattr(review_session, "datetime", Clock)
    write = review_session.atomic_json

    def write_and_invalidate(path, *args, **kwargs):
        result = write(path, *args, **kwargs)
        if path.name == "model-task.json" and boundary == "model":
            invalidate()
        return result

    monkeypatch.setattr(review_session, "atomic_json", write_and_invalidate)
    query = OdooSource.query

    def query_and_invalidate(*args, **kwargs):
        result = query(*args, **kwargs)
        if model_calls:
            reads.append(True)
            if boundary == "return_read":
                invalidate()
        return result

    monkeypatch.setattr(OdooSource, "query", query_and_invalidate)
    failed = run_and_wait(session, investigate(session), monkeypatch)
    code = "review_policy_expired" if change == "expiry" else "environment_generation_changed"
    assert failed["status"] == "failed" and failed["detail"] == code
    assert "dispatch_state" not in failed
    assert bool(model_calls) == (boundary != "model")
    assert bool(reads) == (boundary == "return_read")
    assert responses == ([] if boundary == "model" else [{"status": "tool_error", "code": code}])
    assert not session.product.bindings


@pytest.mark.parametrize("clarified", [False, True], ids=["explicit", "clarified"])
@pytest.mark.parametrize("shared_offer", [False, True], ids=["distinct-offer", "shared-offer"])
def test_one_notified_line_never_expands_to_other_purchases(
    tmp_path, real_bundle, monkeypatch, clarified, shared_offer
):
    session, arguments = make(tmp_path / "setup", real_bundle, fixed=fixed)
    if not clarified:
        arguments.update(event=event_from_text(arguments["notices"][0]["text"]), notices=[])
        session = ReviewSession(tmp_path / "explicit", **arguments)
    connection = session.environment.connection
    if shared_offer:
        lines = connection.records["purchase.order.line"]
        lines[1]["price_unit"] = lines[0]["price_unit"]
    outcome = run_and_wait(session, investigate(session), monkeypatch)
    op = "initial"
    if clarified:
        assert outcome["status"] == "awaiting_clarification" and not session.product.bindings
        outcome = run_and_wait(session, clarify(session), monkeypatch)
        op = "reply"
    store = session._path(op) / "store"
    if shared_offer:
        assert outcome["detail"] == "shared_offer_cancellation_unsupported"
        assert not (store / "investigation/review.json").exists()
        assert not session.product.bindings
    else:
        assert outcome["status"] == "completed", outcome
        review = read_json(store / "investigation/review.json")
        (binding,) = session.product.bindings
        disruption = binding["request"]["disruption"]
        assert review["request"] == binding["request"]
        assert (
            disruption["purchase_order_id"] == binding["confirmed_supply"]["purchase_order_id"] == 1
        )
        assert (
            disruption["purchase_line_id"] == binding["confirmed_supply"]["purchase_line_id"] == 1
        )
        plan = store / review["plan_id"]
        assert read_json(plan / "blueprint.json")["repair_context"][
            "broken_purchase_order_refs"
        ] == ["PO-CANCEL"]
        operations = read_json(plan / "change-set.json")["operations"]
        assert [
            o["target"]["id"]
            for o in operations
            if o["action"] == "cancel" and o["kind"] == "purchase_order"
        ] == [1]
    assert connection.write_attempts == []


@pytest.mark.parametrize("probe", ["generation_probe", "after_preflight", "before_dispatch"])
def test_transient_generation_probe_after_admission_can_retry_same_turn(
    tmp_path, real_bundle, monkeypatch, probe
):
    from forgeflow.investigation import review_session
    from forgeflow.product.executor import ProductExecutionError

    calls = []

    def worker(prompt, dispatch):
        calls.append(prompt)
        return fixed(prompt, dispatch)

    session, arguments = make(tmp_path, real_bundle, fixed=worker)
    unavailable = False
    generation = session.environment.generation

    def check():
        if unavailable:
            raise ProductExecutionError("environment_generation_unavailable")
        return generation()

    def poison():
        nonlocal unavailable
        unavailable = True

    commitments = review_session.commitments

    def read(*args):
        result = commitments(*args)
        if probe == "after_preflight":
            poison()
        return result

    save = review_session.atomic_json

    def save_task(path, *args, **kwargs):
        result = save(path, *args, **kwargs)
        if probe == "before_dispatch" and path.name == "model-task.json":
            poison()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(session.environment, "generation", check)
        patch.setattr(review_session, "commitments", read)
        patch.setattr(review_session, "atomic_json", save_task)
        result = run_and_wait(
            session,
            investigate(session),
            patch,
            before_worker=poison if probe == "generation_probe" else None,
        )
    assert result["detail"] == "environment_generation_unavailable"
    assert result["dispatch_state"] == "not_dispatched" and result["usage"]["tokens"] == 0
    assert not calls
    restarted = ReviewSession(session.root, **arguments)
    request = retry(restarted, "initial", "resume")
    restarted.environment.identity = "another-generation"
    with pytest.raises(CaseError, match="environment_generation_changed"):
        restarted.submit("owner", request)
    restarted.environment.identity = session.generation
    resumed = run_and_wait(restarted, request, monkeypatch)
    assert resumed["status"] == "awaiting_clarification" and len(calls) == 1
    assert restarted.get("owner", "initial") == result


def test_generation_probe_failure_after_dispatch_does_not_enable_retry(
    tmp_path, real_bundle, monkeypatch
):
    from forgeflow.product.executor import ProductExecutionError

    def unavailable():
        raise ProductExecutionError("environment_generation_unavailable")

    def worker(prompt, dispatch):
        fixed(prompt, dispatch)
        monkeypatch.setattr(session.environment, "generation", unavailable)

    session, _ = make(tmp_path, real_bundle, fixed=worker)
    result = run_and_wait(session, investigate(session), monkeypatch)
    assert (
        result["detail"] == "environment_generation_unavailable" and "dispatch_state" not in result
    )
    monkeypatch.setattr(session.environment, "generation", lambda: session.generation)
    with pytest.raises(CaseError, match="review_retry_unavailable"):
        session.submit("owner", retry(session, "initial", "retry"))


def test_same_notice_in_new_workspace_rejects_stale_and_unbound_requests(
    tmp_path, real_bundle, monkeypatch
):
    from forgeflow.recovery.storage import digest

    old, arguments = make(tmp_path / "first", real_bundle, fixed=fixed)
    old_request = investigate(old)
    arguments["environment"].identity = "new ERP"
    current = ReviewSession(tmp_path / "second", **arguments)
    fresh = investigate(current)
    assert fresh["event_sha256"] == old_request["event_sha256"]
    assert fresh["registration_sha256"] != old_request["registration_sha256"]
    unaffiliated = current.view("another-user")["registration_sha256"]
    assert unaffiliated != fresh["registration_sha256"]
    legacy = {key: value for key, value in fresh.items() if key != "registration_sha256"}
    for intent in (old_request, legacy, {**fresh, "registration_sha256": unaffiliated}):
        with pytest.raises(CaseError, match="review_registration_changed"):
            current.submit("owner", intent)
    for kind in ("clarify", "retry"):
        with pytest.raises(CaseError, match="review_registration_changed"):
            current.submit("owner", {**old_request, "kind": kind, "previous": "prior"})
    assert not (current.root / "session.json").exists() and not (current.root / "turns").exists()
    assert run_and_wait(current, fresh, monkeypatch)["status"] == "awaiting_clarification"
    # Reopening the same registered workspace preserves the original turn and
    # the input hash of a client that sent its request before binding was required.
    restarted = ReviewSession(current.root, **arguments)
    assert restarted.submit("owner", legacy) == restarted.get("owner", "initial")
    assert restarted.submit("owner", fresh) == restarted.get("owner", "initial")
    assert digest(read_json(restarted._path("initial") / "intent.json")) == digest(legacy)
