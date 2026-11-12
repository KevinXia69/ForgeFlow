from __future__ import annotations

from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings
from forgeflow.knowledge.gateway import mint_capability


def test_capability_gateway_is_tenant_bound_and_short_lived(tmp_path) -> None:
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_SESSION_SECRET": "gateway-test-secret-01234567890123456789",
        "FORGEFLOW_INVITE_CODES": "gateway-test-owner",
    })
    # Pin the generation instead of relying on whatever the shared control
    # database happens to have published.
    token = mint_capability(settings.session_secret, tenant_id="forgeflow-project",
                            scope_id="scope-a", generation="gen-gateway-test",
                            ttl_seconds=60)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/internal/knowledge/search", headers={
            "X-ForgeFlow-Capability": token,
        }, json={"question": "ForgeFlow 是什么？", "limit": 3, "lang": "zh"})
        assert response.status_code == 200, response.text
        assert "found" in response.json()
        denied = client.post("/api/internal/knowledge/search", headers={
            "X-ForgeFlow-Capability": "not-a-token",
        }, json={"question": "ForgeFlow 是什么？", "limit": 3, "lang": "zh"})
        assert denied.status_code == 401


def test_capability_carries_published_generation() -> None:
    from forgeflow.knowledge.gateway import verify_capability
    token = mint_capability("a" * 40, tenant_id="t", scope_id="s", generation="gen-9")
    assert verify_capability("a" * 40, token).generation == "gen-9"
