"""Attachment authorization and upload-to-provider contract, without live providers."""
from __future__ import annotations

import json
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from forgeflow.accounts import AccountStore
from forgeflow.api.app import _attachment_store, create_app
from forgeflow.api.dsh_harness import DshReply
from forgeflow.api.model_settings import ModelSettingsStore, build_settings
from forgeflow.api.settings import create_settings

INVITES = ("attachment-owner-one", "attachment-owner-two")
MODEL = {"profile": "custom", "base_url": "https://api.example.com/v1",
         "model": "test-model", "api_key": "synthetic-test-key"}
MARKER = "PRIVATE-DOCX-REFERENCE-68291"


@pytest.fixture(autouse=True)
def isolated_attachment_storage(monkeypatch):
    monkeypatch.delenv("FORGEFLOW_ATTACHMENTS_ROOT", raising=False)


def settings_for(tmp_path, deployment="local"):
    return create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_KNOWLEDGE_ROOT": str(tmp_path / "knowledge"),
        "FORGEFLOW_SESSION_SECRET": "attachment-session-secret-0123456789",
        "FORGEFLOW_INVITE_CODES": ",".join(INVITES),
        "FORGEFLOW_SECURE_COOKIE": "false",
        "FORGEFLOW_DEPLOYMENT": deployment,
        "FORGEFLOW_REVIEW_MODEL_HOSTS": "api.example.com",
    })


def login(client, index=0):
    client.cookies.clear()
    response = client.post("/api/auth/session", json={"invite_code": INVITES[index]})
    assert response.status_code == 201, response.text
    return response.json()["uid"]


def upload(client, marker=MARKER):
    document = Document()
    document.add_paragraph(marker)
    payload = BytesIO()
    document.save(payload)
    response = client.post("/api/attachments", files={
        "file": ("contract.docx", payload.getvalue(),
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def configured_app(tmp_path, monkeypatch, mode, deployment="local"):
    calls = []

    class Harness:
        def with_evidence(self, _path):
            return self

        def ask(self, prompt, _key):
            calls.append(prompt)
            return DshReply(MARKER, [], [], None, "project")

        def stream(self, prompt, _key):
            calls.append(prompt)
            yield {"type": "final", "answer": MARKER, "citations": [], "mode": "harness"}

    def provider(_settings, body, _timeout, **options):
        calls.append({"body": body, "options": options})
        return {"choices": [{"message": {"content": MARKER}}]}

    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env",
                        lambda: Harness() if mode == "harness" else None)
    monkeypatch.setattr("forgeflow.api.assistant._post", provider)
    monkeypatch.setattr("forgeflow.api.app.platform_settings",
                        lambda: build_settings(**MODEL) if mode == "platform" else None)
    settings = settings_for(tmp_path, deployment)
    if mode == "stored":
        ModelSettingsStore(settings.approvals_root).save(**MODEL)
    return create_app(settings), calls


@pytest.mark.parametrize("deployment", ["local", "review"])
def test_byok_upload_reaches_the_actual_provider_request(tmp_path, monkeypatch, deployment):
    app, calls = configured_app(tmp_path, monkeypatch, "byok", deployment)
    with TestClient(app) as client:
        login(client)
        ref = upload(client)
        response = client.post("/api/assistant/answer", json={
            "question": "What is the document reference?", "attachments": [ref], "model": MODEL,
        })
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "model"
    assert len(calls) == 1
    assert MARKER in json.dumps(calls[0]["body"])
    assert "contract.docx" in json.dumps(calls[0]["body"])
    if deployment == "review":
        assert calls[0]["options"] == {"allow_redirects": False}


@pytest.mark.parametrize("mode,route", [
    ("byok", "answer"), ("stored", "answer"), ("platform", "answer"),
    ("harness", "answer"), ("harness", "stream"), ("retrieval", "answer"),
])
def test_other_principal_cannot_use_known_attachment_reference(tmp_path, monkeypatch, mode, route):
    app, calls = configured_app(tmp_path, monkeypatch, mode)
    with TestClient(app) as client:
        login(client)
        ref = upload(client)
        login(client, 1)
        response = client.post(f"/api/assistant/{route}", json={
            "question": "What is the document reference?", "attachments": [ref],
            **({"model": MODEL} if mode == "byok" else {}),
        })
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "attachment_not_found"
    assert MARKER not in response.text
    assert calls == []


@pytest.mark.parametrize("mode,route", [
    ("stored", "answer"), ("platform", "answer"),
    ("harness", "answer"), ("harness", "stream"),
])
def test_owner_attachment_reaches_every_supported_answer_path(tmp_path, monkeypatch, mode, route):
    app, calls = configured_app(tmp_path, monkeypatch, mode)
    with TestClient(app) as client:
        login(client)
        ref = upload(client)
        response = client.post(f"/api/assistant/{route}", json={
            "question": "What is the document reference?", "attachments": [ref],
        })
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert MARKER in json.dumps(calls[0])


def test_upload_requires_authentication(tmp_path, monkeypatch):
    app, _calls = configured_app(tmp_path, monkeypatch, "byok")
    with TestClient(app) as client:
        response = client.post("/api/attachments", files={"file": ("private.txt", b"private")})
    assert response.status_code == 401


def test_persistent_reference_survives_restart_for_its_owner(tmp_path, monkeypatch):
    app, _calls = configured_app(tmp_path, monkeypatch, "byok")
    with TestClient(app) as client:
        login(client)
        ref = upload(client)
    restarted, calls = configured_app(tmp_path, monkeypatch, "byok")
    with TestClient(restarted) as client:
        login(client)
        response = client.post("/api/assistant/answer", json={
            "question": "What is the document reference?", "attachments": [ref], "model": MODEL,
        })
    assert response.status_code == 200, response.text
    assert MARKER in json.dumps(calls[0]["body"])


def test_separate_attachment_root_works_with_read_only_runs(tmp_path, monkeypatch):
    from forgeflow.attachments import AttachmentRef

    writable = tmp_path / "writable-attachments"
    monkeypatch.setenv("FORGEFLOW_ATTACHMENTS_ROOT", str(writable))
    runs = tmp_path / "runs"
    runs.mkdir()
    runs.chmod(0o555)
    try:
        app, calls = configured_app(tmp_path, monkeypatch, "byok")
        with TestClient(app) as client:
            uid = login(client)
            ref = upload(client)
            store = _attachment_store(runs, uid)
            path = store.host_path(AttachmentRef(**ref))
            assert path.is_relative_to(writable)
            assert list(runs.iterdir()) == []
            path.write_bytes(b"x" * ref["size"])
            response = client.post("/api/assistant/answer", json={
                "question": "What is the document reference?", "attachments": [ref], "model": MODEL,
            })
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "attachment_corrupt"
        assert calls == []
    finally:
        runs.chmod(0o755)


def test_empty_attachment_root_uses_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEFLOW_ATTACHMENTS_ROOT", "  ")
    assert _attachment_store(tmp_path, "owner").root.is_relative_to(tmp_path)


def test_storage_failure_returns_a_safe_service_error(tmp_path, monkeypatch):
    app, _calls = configured_app(tmp_path, monkeypatch, "byok")
    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("occupied")
    monkeypatch.setenv("FORGEFLOW_ATTACHMENTS_ROOT", str(unavailable))
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/attachments", files={"file": ("note.txt", b"private")})
    assert response.status_code == 503
    assert response.json() == {"detail": "attachment_unavailable"}


def test_independent_accounts_in_same_tenant_cannot_share_refs(tmp_path, monkeypatch):
    app, calls = configured_app(tmp_path, monkeypatch, "byok")
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    admin, _secret = accounts.bootstrap(email="admin@example.com", password="Synthetic-pass-1",
                                         tenant_id="one-tenant")
    for email in ("one@example.com", "two@example.com"):
        invite = accounts.invite(admin_principal_id=admin.principal_id, email=email, role="observer")
        accounts.activate(code=invite, password="Synthetic-pass-1")
    # Install the actual account store and its login routes, preserving the
    # provider configuration established above.
    app = create_app(app.state.settings, accounts=accounts)
    with TestClient(app) as client:
        def account_login(email):
            client.cookies.clear()
            response = client.post("/api/accounts/login", json={
                "email": email, "password": "Synthetic-pass-1",
            })
            assert response.status_code == 200, response.text

        account_login("one@example.com")
        ref = upload(client)
        account_login("two@example.com")
        request = {"question": "What is the document reference?", "attachments": [ref], "model": MODEL}
        assert client.post("/api/assistant/answer", json=request).status_code == 404
        assert calls == []
        account_login("one@example.com")
        assert client.post("/api/assistant/answer", json=request).status_code == 200
        assert MARKER in json.dumps(calls[0]["body"])


@pytest.mark.parametrize("changed_input", ["replace", "remove", "add", "text", "history", "lang"])
def test_stream_replay_rejects_changed_attachment_or_context(tmp_path, monkeypatch, changed_input):
    app, calls = configured_app(tmp_path, monkeypatch, "harness")
    with TestClient(app) as client:
        login(client)
        ref = upload(client)
        other_ref = upload(client, "DIFFERENT-DOCUMENT-9918")
        initial = {
            "question": "What is the document reference?", "conversation_id": "attachment-chat",
            "turn_id": "one-turn", "attachments": [] if changed_input == "add" else [ref],
        }
        changed = {**initial}
        if changed_input == "replace":
            changed["attachments"] = [other_ref]
        elif changed_input == "remove":
            changed["attachments"] = []
        elif changed_input == "add":
            changed["attachments"] = [ref]
        elif changed_input == "text":
            initial["attachment_text"] = "original legacy text"
            changed["attachment_text"] = "different legacy text"
        elif changed_input == "history":
            changed["history"] = [{"role": "user", "content": "A different context"}]
        elif changed_input == "lang":
            changed["lang"] = "en"
        first = client.post("/api/assistant/stream", json=initial)
        assert first.status_code == 200, first.text
        repeated = client.post("/api/assistant/stream", json={**initial, "resume": True})
        assert repeated.status_code == 200 and repeated.text == first.text
        rejected = client.post("/api/assistant/stream", json={**changed, "resume": True})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"] == "turn_payload_mismatch"
        assert len(calls) == 1
        fresh = client.post("/api/assistant/stream", json={**changed, "turn_id": "another-turn"})
        assert fresh.status_code == 200, fresh.text
        assert len(calls) == 2
