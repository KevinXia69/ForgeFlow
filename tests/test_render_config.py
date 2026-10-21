"""Static checks for the Render Blueprint and the review deployment runbook.

PyYAML is used when importable. The literal checks always run, so the same
invariants are covered in environments where PyYAML is absent.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only when PyYAML is absent
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "render.yaml"
DOC = ROOT / "docs/deploy-render.md"
BUILDER = ROOT / "scripts" / "build_demo_runs.py"

VALUES = {
    "FORGEFLOW_DEPLOYMENT": "review",
    "FORGEFLOW_DATA_ROOT": "/app/demo-runs",
    "FORGEFLOW_APPROVALS_ROOT": "/tmp/approvals",
    "FORGEFLOW_KNOWLEDGE_ROOT": "/app",
    "FORGEFLOW_SECURE_COOKIE": "true",
    "FORGEFLOW_RUNNER_ENABLED": "false",
}
GENERATED_KEY = "FORGEFLOW_SESSION_SECRET"
PROMPTED_KEY = "FORGEFLOW_INVITE_CODES"
ENV_KEYS = tuple(VALUES) + (GENERATED_KEY, PROMPTED_KEY)
SECRET_PATTERN = re.compile(
    r"(?:[A-Za-z0-9_-]{40,}|[A-Za-z0-9+/]{40,}={0,2}|[0-9a-fA-F]{32,})"
)


def _service() -> dict | None:
    """Return the one service mapping, or None when PyYAML is unavailable."""
    if yaml is None:
        return None
    document = yaml.safe_load(RENDER.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    services = document["services"]
    assert isinstance(services, list) and len(services) == 1
    service = services[0]
    assert isinstance(service, dict)
    return service


def test_render_yaml_parses_as_one_web_service():
    text = RENDER.read_text(encoding="utf-8")
    assert re.search(r"(?m)^services:\s*$", text)
    assert re.search(r"(?m)^\s+-\s+type:\s+web\s*$", text)
    assert text.count("type: web") == 1
    service = _service()
    if service is not None:
        assert service["type"] == "web"
        assert service["name"] == "forgeflow"


def test_render_service_uses_docker_and_the_required_settings():
    text = RENDER.read_text(encoding="utf-8")
    for literal in (
        "name: forgeflow",
        "runtime: docker",
        "dockerfilePath: ./deploy/Dockerfile.api",
        "dockerContext: .",
        "healthCheckPath: /api/health",
        "plan: starter",
        "region: singapore",
        "autoDeployTrigger: 'off'",
    ):
        assert literal in text
    # The image CMD reads PORT itself; a custom command broke Render parsing.
    assert "dockerCommand" not in text

    service = _service()
    if service is None:
        return
    assert service["name"] == "forgeflow"
    assert service["runtime"] == "docker"
    assert service["dockerfilePath"] == "./deploy/Dockerfile.api"
    assert service["dockerContext"] == "."
    assert service["healthCheckPath"] == "/api/health"
    assert service["plan"] == "starter"
    assert service["region"] == "singapore"
    assert service["autoDeployTrigger"] == "off"
    assert "dockerCommand" not in service


def test_render_env_vars_are_exact_and_secret_free():
    text = RENDER.read_text(encoding="utf-8")
    keys = re.findall(r"(?m)^\s+- key: (FORGEFLOW_[A-Z_]+)\s*$", text)
    assert set(keys) == set(ENV_KEYS)
    for key, value in VALUES.items():
        pattern = (
            rf"(?m)^\s+- key: {key}\s*\n"
            rf"\s+value: ['\"]?{re.escape(value)}['\"]?\s*$"
        )
        assert re.search(pattern, text), f"missing env value for {key}"
    assert re.search(
        rf"(?m)^\s+- key: {GENERATED_KEY}\s*\n\s+generateValue: true\s*$", text
    )
    assert re.search(rf"(?m)^\s+- key: {PROMPTED_KEY}\s*\n\s+sync: false\s*$", text)

    service = _service()
    if service is not None:
        env_vars = service["envVars"]
        assert isinstance(env_vars, list)
        by_key = {item["key"]: item for item in env_vars}
        assert set(by_key) == set(ENV_KEYS)
        for key, value in VALUES.items():
            assert by_key[key]["value"] == value
        assert by_key[GENERATED_KEY]["generateValue"] is True
        assert "value" not in by_key[GENERATED_KEY]
        assert by_key[PROMPTED_KEY]["sync"] is False
        assert "value" not in by_key[PROMPTED_KEY]

    assert "sk-" not in text.lower()
    assert SECRET_PATTERN.search(text) is None
    assert "wayraft" not in text.lower()
    assert "private" in text.lower()
    assert "github app" in text.lower()


def test_render_yaml_has_no_hardcoded_secret_or_invite_value():
    text = RENDER.read_text(encoding="utf-8")
    assert "FORGEFLOW_INVITE_CODES" in text
    assert "generateValue: true" in text
    assert "sync: false" in text
    assert re.search(
        r"(?im)^\s+-\s+key:\s+\S*SECRET\S*\s*\n\s+value:", text
    ) is None
    assert re.search(
        r"(?im)^\s+-\s+key:\s+\S*INVITE\S*\s*\n\s+value:", text
    ) is None


def test_doc_covers_new_service_github_app_domain_and_review_mode():
    text = DOC.read_text(encoding="utf-8")
    lower = text.lower()
    for phrase in (
        "private github repository",
        "github app",
        "new service",
        "cname",
        "custom domain",
        "review",
        "tls",
        "suspend",
        "delete",
        "auto deploy",
    ):
        assert phrase in lower, f"deployment doc is missing {phrase!r}"
    assert "KevinXia69/ForgeFlow" in text


def test_doc_lists_exact_env_values_and_write_protection():
    text = DOC.read_text(encoding="utf-8")
    for key, value in VALUES.items():
        assert f"`{key}`" in text
        assert f"`{value}`" in text
    assert f"`{GENERATED_KEY}`" in text
    assert f"`{PROMPTED_KEY}`" in text
    assert "generateValue" in text
    assert "runner_disabled" in text
    assert "503" in text
    assert "ephemeral" in text.lower()
    assert SECRET_PATTERN.search(text) is None


def test_doc_notes_domain_region_and_no_wayraft_reuse():
    text = DOC.read_text(encoding="utf-8")
    lower = text.lower()
    assert "forgeflow.onrender.com" in text
    assert "may already be taken" in lower
    assert "custom domain is the stable" in lower
    assert "region cannot be changed" in lower or "cannot be changed later" in lower

    mentions = [
        line for line in text.splitlines() if "wayraft.onrender.com" in line.lower()
    ]
    assert mentions, "doc should explicitly warn about the old WayRaft URL"
    for line in mentions:
        assert any(marker in line.lower() for marker in ("not", "never", "old")), (
            f"WayRaft URL is not mentioned in a do-not-reuse context: {line!r}"
        )


def test_builder_has_the_allowlist_constants_and_no_forbidden_copy_pattern():
    text = BUILDER.read_text(encoding="utf-8")
    for constant in ("FIXED_ALLOWLIST", "TRIAL_ALLOWLIST", "RECOVERY_ALLOWLIST"):
        assert constant in text
    for fragment in (
        "manifest.json",
        "summary.json",
        "attestation/verifier.json",
        "attestation/verifier.sig",
        "result.json",
        "verifier/reward.json",
        "verifier/reward.txt",
        "harbor/job/*/agent/recovery/**",
    ):
        assert fragment in text
    assert "pi.txt" not in text
    assert "provider-request-policy" not in text
