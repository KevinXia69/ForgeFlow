from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings

SECRET = "test-session-secret-0123456789abcdef"
INVITE = "invite-code-for-tests-0001"


def make_client(tmp_path: Path) -> TestClient:
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_KNOWLEDGE_ROOT": str(tmp_path),
    })
    (tmp_path / "runs").mkdir(exist_ok=True)
    return TestClient(create_app(settings), base_url="https://testserver")


def test_knowledge_requires_auth_and_answers_from_docs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/note.md").write_text(
        "# 权限\n\n只读 key 在 /etc/odoo/api_key.read，写入返回 403。\n"
    )
    with make_client(tmp_path) as client:
        assert client.post("/api/knowledge/answer", json={"question": "api_key.read"}).status_code == 401
        client.post("/api/auth/session", json={"invite_code": INVITE})
        response = client.post("/api/knowledge/answer", json={"question": "api_key.read 403"})
        assert response.status_code == 200
        body = response.json()
        assert body["found"] is True
        assert body["passages"][0]["path"] == "docs/note.md"
        assert body["citations"]


def test_unknown_question_is_not_answered(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/auth/session", json={"invite_code": INVITE})
        response = client.post("/api/knowledge/answer", json={"question": "量子香蕉"})
        assert response.status_code == 200
        assert response.json() == {"found": False, "passages": [], "citations": []}
