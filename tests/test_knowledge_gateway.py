import pytest

from forgeflow.knowledge.gateway import CapabilityError, mint_capability, verify_capability


def test_capability_is_short_lived_and_tenant_bound() -> None:
    token = mint_capability("secret" * 8, tenant_id="tenant-a", scope_id="scope-a", ttl_seconds=60)
    capability = verify_capability("secret" * 8, token)
    assert capability.tenant_id == "tenant-a" and capability.scope_id == "scope-a"
    with pytest.raises(CapabilityError):
        verify_capability("other" * 8, token)
