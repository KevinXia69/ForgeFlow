"""HTTP admission and cancellation lifetime without a model or ERP service."""
from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api_stream_turns import events, routes
from test_api_stream_turns import streaming_app as streaming_app
from test_product_cases import INVITE_B, client_for, create_case, make_settings, sign_in
from turn_helpers import turn_id

from forgeflow.api import stream_hub, turn_identity
from forgeflow.api.app import create_app
from forgeflow.api.turn_identity import InvalidTurnIdentity, new_turn_expiry
from forgeflow.product import FakeProductExecutor


def identity(created: float, label: str = "one") -> str:
    return f"t1_{int(created * 1000):013d}_{hashlib.sha256(label.encode()).hexdigest()[:32]}"


@pytest.mark.parametrize("offset,error", [(-3600, "turn_expired"), (61, "turn_timestamp_in_future")])
def test_identity_has_fixed_age_and_bounded_clock_skew(monkeypatch, offset, error):
    monkeypatch.setattr(turn_identity, "time", SimpleNamespace(time=lambda: 1800000000.0))
    with pytest.raises(InvalidTurnIdentity, match=error):
        new_turn_expiry(identity(1800000000 + offset))
    fresh = identity(1800000000)
    assert new_turn_expiry(fresh) == 1800003600
    assert new_turn_expiry(identity(1800000060)) == 1800003660


@pytest.mark.parametrize("kind", ["knowledge", "case"])
@pytest.mark.parametrize("value,code,detail", [
    ("legacy-uuid", 410, "turn_id_upgrade_required"),
    (identity(time.time() - 7200), 410, "turn_expired"),
    (identity(time.time() + 86400), 400, "turn_timestamp_in_future"),
])
def test_unknown_invalid_id_never_allocates_runtime(streaming_app, monkeypatch, kind, value, code, detail):
    client, calls, _, _, case_id = streaming_app
    stream, cancel = routes(kind, case_id)
    monkeypatch.setattr(client.app.state.dsh_harness, "with_evidence",
                        lambda _: pytest.fail("invalid identity allocated a runtime"))
    body = {"conversation_id": "invalid", "turn_id": value}
    for endpoint, payload in ((cancel, body), (stream, {**body, "question": "original verification?"})):
        response = client.post(endpoint, json=payload)
        assert response.status_code == code, response.text
        assert response.json()["detail"] == detail
    assert calls == []
    assert client.app.state.assistant_hub._jobs == {}


@pytest.mark.parametrize("kind", ["knowledge", "case"])
def test_legacy_existing_record_can_replay_and_stop_but_not_create(streaming_app, kind):
    client, calls, _, release, case_id = streaming_app
    stream, cancel = routes(kind, case_id)
    release.set()
    body = {"conversation_id": "legacy", "turn_id": turn_id("legacy-source"), "question": "saved answer"}
    original = client.post(stream, json=body)
    assert original.status_code == 200
    hub = client.app.state.assistant_hub
    source = next(iter(hub._jobs.values())).snapshot()
    legacy = "saved-by-previous-client"
    source["key"] = source["key"].rsplit(":turn:", 1)[0] + ":turn:" + hashlib.sha256(legacy.encode()).hexdigest()
    hub._journal.save(source)
    body["turn_id"] = legacy
    replay = client.post(stream, json={**body, "resume": True})
    assert replay.status_code == 200, replay.text
    assert events(replay) == events(original)
    assert client.post(cancel, json={"conversation_id": "legacy", "turn_id": legacy}).json()["cancelled"]
    assert client.post(stream, json=body).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["knowledge", "case"])
def test_stateless_legacy_stream_still_works(streaming_app, kind):
    client, calls, _, release, case_id = streaming_app
    release.set()
    stream, _ = routes(kind, case_id)
    response = client.post(stream, json={"question": "saved answer"})
    assert response.status_code == 200
    assert events(response)[-1]["type"] == "final"
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["knowledge", "case"])
def test_expired_cancel_cannot_start_again_after_cleanup_and_restart(tmp_path, monkeypatch, kind):
    now = [time.time()]
    clock = SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0])
    monkeypatch.setattr(turn_identity, "time", clock)
    monkeypatch.setattr(stream_hub, "time", clock)
    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: None)
    root = tmp_path.resolve()
    with client_for(root, FakeProductExecutor()) as (client, settings):
        sign_in(client)
        case = create_case(client)
        stream, cancel = routes(kind, case["case_id"])
        body = {"conversation_id": "expired", "turn_id": identity(now[0])}
        assert client.post(cancel, json=body).status_code == 200
        assert client.post(stream, json={**body, "question": "execute?"}).status_code == 409
        now[0] += 3601
        # Only Stop traffic is needed to clean both memory and journal.
        assert client.post(cancel, json={**body, "turn_id": identity(now[0], "next")}).status_code == 200
        assert len(list((settings.approvals_root / "turn-journal").glob("*.json"))) == 1
        assert client.post(stream, json={**body, "question": "execute?"}).status_code == 410
    with client_for(root, FakeProductExecutor()) as (client, _):
        sign_in(client)
        response = client.post(stream, json={**body, "question": "execute?"})
        assert response.status_code == 410
        assert response.json()["detail"] == "turn_expired"
        assert client.post(cancel, json=body).status_code == 410


@pytest.mark.parametrize("kind", ["knowledge", "case"])
def test_api_cancel_quota_is_explicit_and_duplicate_is_idempotent(streaming_app, kind):
    client, calls, _, _, case_id = streaming_app
    _, cancel = routes(kind, case_id)
    hub = client.app.state.assistant_hub
    hub._max_early_cancellations_per_owner = 1
    body = {"conversation_id": "quota", "turn_id": turn_id("quota-one")}
    assert client.post(cancel, json=body).status_code == 200
    denied = client.post(cancel, json={**body, "turn_id": turn_id("quota-two")})
    assert denied.status_code == 503
    assert denied.json()["detail"] == "cancel_capacity"
    assert client.post(cancel, json=body).json()["cancelled"]
    assert len(hub._jobs) == 1
    assert calls == []


@pytest.mark.parametrize("kind", ["knowledge", "case"])
def test_hosted_unknown_cancel_is_budgeted_but_existing_stop_is_always_admitted(tmp_path, monkeypatch, kind):
    requested = []
    deny = [False]

    class Limiter:
        def retry_after(self, key, *, limit):
            requested.append((key, limit))
            return 23 if deny[0] and key.startswith(("product:", "cancel:")) else 0

    monkeypatch.setattr("forgeflow.api.app.FixedWindowLimiter", Limiter)
    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: None)
    settings = replace(make_settings(tmp_path.resolve()), deployment="hosted", public_runs=(),
                       trusted_hosts=("testserver",), environment_generation="a" * 32)
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()),
                    base_url="https://testserver") as client:
        uid = sign_in(client)
        case = create_case(client)
        _, cancel = routes(kind, case["case_id"])
        body = {"conversation_id": "budget", "turn_id": turn_id("budget-one")}
        assert client.post(cancel, json=body).status_code == 200
        assert (f"cancel:{uid}", 30) in requested and ("cancel:global", 120) in requested
        deny[0] = True
        requested.clear()
        assert client.post(cancel, json=body).status_code == 200
        assert not any(key.startswith(("product:", "cancel:")) for key, _ in requested)
        response = client.post(cancel, json={**body, "turn_id": turn_id("budget-two")})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "23"
        if kind == "case":
            client.cookies.clear()
            sign_in(client, INVITE_B)
            assert client.post(cancel, json=body).status_code == 404
