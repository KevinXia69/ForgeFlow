"""Public optional model choices preserve the ready-to-use platform and isolation."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.dsh_harness import DshReply
from forgeflow.api.model_settings import build_settings
from forgeflow.api.settings import create_settings

INVITE = "synthetic-model-invite-001"
KEY = "synthetic-browser-model-key"
MODEL = {
    "profile": "anthropic",
    "provider": "anthropic",
    "api": "anthropic-messages",
    "base_url": "https://api.anthropic.com",
    "model": "claude-sonnet-4-6",
    "api_key": KEY,
    "image_input": True,
}


def make_app(tmp_path, monkeypatch):
    settings = create_settings(
        {
            "FORGEFLOW_DEPLOYMENT": "hosted",
            "FORGEFLOW_SESSION_SECRET": "synthetic-session-secret-000000000001",
            "FORGEFLOW_INVITE_CODES": INVITE,
            "FORGEFLOW_TRUSTED_HOSTS": "testserver",
            "FORGEFLOW_ENVIRONMENT_GENERATION": "a" * 32,
            "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
            "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
            "FORGEFLOW_KNOWLEDGE_ROOT": str(tmp_path),
        }
    )
    calls = []

    class Harness:
        model = "deepseek-flash"
        provider = "deepseek"

        def with_evidence(self, path):
            self.evidence_scope_path = path
            return self

        def with_model(self, model):
            calls.append(("select", model.api, model.provider, model.api_key))
            child = Harness()
            child.model = model.model
            child.provider = model.provider
            child.evidence_scope_path = self.evidence_scope_path
            return child

        def ask(self, prompt, key):
            calls.append(("ask", self.provider, prompt))
            return DshReply(self.provider, [], [], None, "project")

        def stream(self, prompt, key):
            calls.append(("stream", self.provider, prompt))
            yield {"type": "final", "answer": self.provider, "citations": [], "mode": "harness"}

        def close(self):
            calls.append(("close", self.provider))

    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: Harness())
    platform = build_settings(
        "deepseek_flash",
        "https://api.deepseek.com/v1",
        "deepseek-flash",
        "synthetic-platform-model-key",
    )
    monkeypatch.setattr("forgeflow.api.app.platform_settings", lambda: platform)
    return create_app(settings), calls


def client_for(app):
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/auth/session", json={"invite_code": INVITE}).status_code == 201
    return client


def test_public_default_needs_no_key_and_optional_override_does_not_change_it(
    tmp_path, monkeypatch
):
    app, calls = make_app(tmp_path, monkeypatch)
    with client_for(app) as client:
        view = client.get("/api/settings/model").json()
        assert (
            view["model"] == "deepseek-flash"
            and view["configured"] is True
            and view["can_override"] is True
        )
        assert "synthetic-platform-model-key" not in json.dumps(view)
        for model, expected in [(None, "deepseek"), (MODEL, "anthropic"), (None, "deepseek")]:
            response = client.post(
                "/api/assistant/answer",
                json={
                    "question": "Hello provider",
                    "lang": "en",
                    **({"model": model} if model else {}),
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["answer"] == expected
    assert [row[:2] for row in calls if row[0] == "close"] == [("close", "anthropic")]
    assert not (tmp_path / "approvals/model-settings.json").exists()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()


def test_stream_override_replays_journal_without_recreating_credential_child(tmp_path, monkeypatch):
    app, calls = make_app(tmp_path, monkeypatch)
    with client_for(app) as client:
        body = {
            "question": "Hello provider",
            "lang": "en",
            "model": MODEL,
            "conversation_id": "chat1",
            "turn_id": "turn1",
            "history": [{"role": "user", "content": "Earlier question"}],
        }
        first = client.post("/api/assistant/stream", json=body)
        assert first.status_code == 200, first.text
        assert "anthropic" in first.text
        replay = client.post("/api/assistant/stream", json={**body, "resume": True})
        assert replay.status_code == 200 and "anthropic" in replay.text
        changed = client.post(
            "/api/assistant/stream",
            json={**body, "resume": True, "model": {**MODEL, "api_key": KEY + "-changed"}},
        )
        assert changed.status_code == 409
    assert sum(row[0] == "select" for row in calls) == 1
    assert sum(row[0] == "close" for row in calls) == 1
    assert "Earlier question" in next(row[2] for row in calls if row[0] == "stream")
    assert all(KEY.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "https://127.0.0.1",
        "http://api.anthropic.com",
        "https://api.anthropic.com.evil.invalid",
        "https://api.anthropic.com:8080",
        "https://api.anthropic.com@evil.invalid",
        "https://api.anthropic.com/?key=synthetic",
    ],
)
def test_hosted_choices_do_not_open_internal_or_arbitrary_proxy(tmp_path, monkeypatch, url):
    app, calls = make_app(tmp_path, monkeypatch)
    with client_for(app) as client:
        response = client.post(
            "/api/assistant/stream",
            json={"question": "Hello provider", "model": {**MODEL, "base_url": url}},
        )
        assert response.status_code == 403
    assert not calls


def test_invalid_protocol_fails_before_request_child(tmp_path, monkeypatch):
    app, calls = make_app(tmp_path, monkeypatch)
    with client_for(app) as client:
        response = client.post(
            "/api/assistant/answer",
            json={"question": "Hello provider", "model": {**MODEL, "api": "unknown"}},
        )
        assert response.status_code == 400
    assert not calls


@pytest.mark.parametrize("stream", [False, True])
def test_case_chat_uses_optional_connection_and_releases_it(tmp_path, monkeypatch, stream):
    app, calls = make_app(tmp_path, monkeypatch)
    case = {"case_id": "synthetic-case", "scenario": "A", "state": "prepared"}
    monkeypatch.setattr("forgeflow.api.app.ReadOnlyEvidenceProjection.get_case", lambda *_: case)
    monkeypatch.setattr(
        "forgeflow.api.app.answer_case_question",
        lambda *_args, **_kwargs: {"answer": "case facts", "answer_basis": "case_record"},
    )
    monkeypatch.setattr(
        "forgeflow.api.app.answer_context", lambda value: {"case_id": value["case_id"]}
    )
    monkeypatch.setattr("forgeflow.api.app._case_question_prompt", lambda _case, question: question)
    with client_for(app) as client:
        route = "/api/product/cases/synthetic-case/ask" + ("/stream" if stream else "")
        body = {
            "question": "Explain this case",
            "conversation_id": "casechat",
            "model": MODEL,
            "history": [{"role": "user", "content": "Earlier case question"}],
        }
        response = client.post(route, json=body)
        assert response.status_code == 200, response.text
        assert "anthropic" in response.text
        assert "synthetic-case" in response.text
    assert [row[0] for row in calls].count("select") == 1
    assert [row[0] for row in calls].count("close") == 1
    assert "Earlier case question" in next(row[2] for row in calls if row[0] in {"ask", "stream"})


def test_public_connection_check_requires_a_real_tool_round_trip(tmp_path, monkeypatch):
    from forgeflow.model_transport import TransportError

    app, calls = make_app(tmp_path, monkeypatch)
    probes = []

    def probe(model):
        probes.append((model.provider, model.api, model.api_key))

    monkeypatch.setattr("forgeflow.model_transport.ModelTransport.probe_tools", probe)
    with client_for(app) as client:
        success = client.post("/api/settings/model/test", json={"model": MODEL})
        assert success.json()["tools"] is True
        assert probes == [("anthropic", "anthropic-messages", KEY)]

        def unsupported(_):
            raise TransportError("model_tools_unsupported")

        monkeypatch.setattr("forgeflow.model_transport.ModelTransport.probe_tools", unsupported)
        failed = client.post("/api/settings/model/test", json={"model": MODEL})
        assert failed.json() == {"ok": False, "error": "model_tools_unsupported"}
        assert KEY not in failed.text
    assert not calls
