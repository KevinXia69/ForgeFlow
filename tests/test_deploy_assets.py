"""Static checks for the container deployment assets under ``deploy/``.

The tests are intentionally dependency-light: PyYAML is used when it is
importable, otherwise a few literal checks cover the same compose invariants.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when PyYAML is absent
    yaml = None

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
DOCKERFILE = DEPLOY / "Dockerfile.api"
COMPOSE = DEPLOY / "docker-compose.yml"
ENV_EXAMPLE = DEPLOY / ".env.example"
README = DEPLOY / "README.md"
ASSETS = (DOCKERFILE, COMPOSE, ENV_EXAMPLE, README)
ENV_KEYS = (
    "FORGEFLOW_DATA_ROOT",
    "FORGEFLOW_APPROVALS_ROOT",
    "FORGEFLOW_SESSION_SECRET",
    "FORGEFLOW_INVITE_CODES",
    "FORGEFLOW_SECURE_COOKIE",
)
TOKEN_PATTERN = re.compile(
    r"(?:[A-Za-z0-9_-]{40,}|[A-Za-z0-9+/]{40,}={0,2}|[0-9a-fA-F]{32,})"
)
HOST_PATH_PATTERN = re.compile(
    r"(?:/Users/|/home/[^/\s]+/|/root/|/private/|/var/folders/|[A-Za-z]:\\Users\\)"
)


def _compose_service() -> dict | None:
    if yaml is None:
        return None
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document["services"]["api"]


def _dotenv_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_dockerfile_uses_unprivileged_user_and_healthcheck():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in text
    assert "USER 10001" in text
    assert "USER root" not in text
    assert "HEALTHCHECK" in text
    assert "/api/health" in text
    assert "EXPOSE 8000" in text
    assert "\nENTRYPOINT " not in text
    assert 'CMD ["forgeflow", "api", "--host", "0.0.0.0"]' in text
    assert "NEBIUS_API_KEY" not in text
    assert "password" not in text.lower()


def test_dockerfile_pins_runtime_dependencies():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip install --no-cache-dir ." in text
    assert "fastapi==0.152.0" in text
    assert "uvicorn[standard]==0.54.0" in text
    assert "cryptography==50.0.2" in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert "PYTHONUNBUFFERED=1" in text


def test_compose_parses_and_builds_from_repo_root():
    text = COMPOSE.read_text(encoding="utf-8")
    if yaml is None:
        assert re.search(r"(?m)^services:\s*$", text)
        assert re.search(r"(?m)^  api:\s*$", text)
        assert "context: .." in text
        assert "dockerfile: deploy/Dockerfile.api" in text
        assert "- .env" in text
        return
    service = _compose_service()
    assert service is not None
    assert service["build"]["context"] == ".."
    assert service["build"]["dockerfile"] == "deploy/Dockerfile.api"
    env_file = service["env_file"]
    env_files = [env_file] if isinstance(env_file, str) else env_file
    assert ".env" in env_files


def test_compose_is_read_only_non_root_and_loopback_only():
    text = COMPOSE.read_text(encoding="utf-8")
    service = _compose_service()
    if service is None:
        assert "read_only: true" in text
        assert 'user: "10001:10001"' in text
        assert "restart: unless-stopped" in text
        assert "127.0.0.1:8000:8000" in text
        assert "tmpfs: /tmp" in text
        assert "healthcheck:" in text
        assert "/api/health" in text
        return
    assert service["read_only"] is True
    assert service["user"] == "10001:10001"
    assert service["restart"] == "unless-stopped"
    assert service["ports"] == ["127.0.0.1:8000:8000"]
    assert service["tmpfs"] == "/tmp"
    assert any("/api/health" in str(part) for part in service["healthcheck"]["test"])


def test_compose_mounts_runs_read_only_and_persists_approvals():
    text = COMPOSE.read_text(encoding="utf-8")
    service = _compose_service()
    if service is None:
        assert "../runs:/data/runs:ro" in text
        assert "forgeflow-approvals:/data/approvals" in text
        assert re.search(r"(?m)^volumes:\s*$", text)
        return
    assert "../runs:/data/runs:ro" in service["volumes"]
    assert "forgeflow-approvals:/data/approvals" in service["volumes"]
    document = yaml.safe_load(text)
    assert isinstance(document, dict)
    assert "forgeflow-approvals" in document["volumes"]


def test_env_example_has_required_keys_and_safe_placeholders():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = _dotenv_values(text)
    assert set(ENV_KEYS) <= set(values)
    assert values["FORGEFLOW_DATA_ROOT"] == "/data/runs"
    assert values["FORGEFLOW_APPROVALS_ROOT"] == "/data/approvals"
    assert values["FORGEFLOW_SECURE_COOKIE"] == "false"
    assert "TLS" in text
    assert "token_urlsafe(48)" in text
    secret = values["FORGEFLOW_SESSION_SECRET"]
    assert secret
    assert any(marker in secret.lower() for marker in ("replace", "change", "generate"))
    assert TOKEN_PATTERN.search(secret) is None
    codes = [code.strip() for code in values["FORGEFLOW_INVITE_CODES"].split(",") if code.strip()]
    assert len(codes) == 2
    assert all(len(code) >= 16 for code in codes)


def test_readme_documents_mounts_rotation_and_runbook():
    text = README.read_text(encoding="utf-8").lower()
    assert "cp .env.example .env" in text
    assert "docker compose up --build" in text
    assert "127.0.0.1:8000" in text
    assert "uv run forgeflow api" in text
    assert "read-only" in text
    assert "/data/runs" in text
    assert "forgeflow-approvals" in text
    assert "/data/approvals" in text
    assert "rotat" in text
    assert "cannot execute erp writes" in text


def test_deploy_assets_have_no_tokens_or_absolute_host_paths():
    for path in ASSETS:
        text = path.read_text(encoding="utf-8")
        token = TOKEN_PATTERN.search(text)
        assert token is None, f"{path.name} contains a token-shaped value: {token.group(0)!r}"
        host_path = HOST_PATH_PATTERN.search(text)
        assert host_path is None, f"{path.name} contains a host path: {host_path.group(0)!r}"
