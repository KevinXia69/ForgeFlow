"""Short-lived host capability for the DSH knowledge tool process."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class CapabilityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeCapability:
    tenant_id: str
    scope_id: str
    expires_at: int
    generation: str | None = None


def _encode(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def mint_capability(secret: str, *, tenant_id: str, scope_id: str,
                    generation: str | None = None, ttl_seconds: int = 300) -> str:
    if not secret or not tenant_id or not scope_id:
        raise CapabilityError("capability_fields_required")
    expires = int(time.time()) + max(30, int(ttl_seconds))
    raw_payload = {"tenant": tenant_id, "scope": scope_id, "exp": expires}
    if generation:
        raw_payload["generation"] = generation
    payload = _encode(raw_payload)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_capability(secret: str, token: str) -> KnowledgeCapability:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise CapabilityError("capability_invalid")
        raw = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expires = int(raw["exp"])
        tenant = str(raw["tenant"])
        scope = str(raw["scope"])
        generation = raw.get("generation")
        generation = str(generation) if generation else None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise CapabilityError("capability_invalid") from None
    if expires < int(time.time()):
        raise CapabilityError("capability_expired")
    return KnowledgeCapability(tenant, scope, expires, generation)
