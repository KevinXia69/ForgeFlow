"""Stable fresh request identities for API tests that replay the same turn."""
import hashlib
import time

_ISSUED_AT = int(time.time() * 1000)


def turn_id(label: str) -> str:
    return f"t1_{_ISSUED_AT}_{hashlib.sha256(label.encode()).hexdigest()[:32]}"
