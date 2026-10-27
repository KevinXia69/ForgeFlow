"""An immutable creation time bounds both a turn and its cancellation record."""
from __future__ import annotations

import re
import time

TURN_LIFETIME_SECONDS = 3600
MAX_FUTURE_SKEW_SECONDS = 60
_TURN_ID = re.compile(r"t1_(\d{13})_[0-9a-f]{32}")


class InvalidTurnIdentity(ValueError):
    pass


def new_turn_expiry(turn_id: str) -> float:
    """Validate a new turn, never refresh the expiry of an existing identity.

    Legacy IDs can still replay existing records. They cannot create new turns:
    after their cancellation record expires, their age is otherwise unknowable.
    """
    match = _TURN_ID.fullmatch(turn_id)
    if match is None:
        raise InvalidTurnIdentity("turn_id_upgrade_required")
    created = int(match[1]) / 1000
    now = time.time()
    if created > now + MAX_FUTURE_SKEW_SECONDS:
        raise InvalidTurnIdentity("turn_timestamp_in_future")
    expires_at = created + TURN_LIFETIME_SECONDS
    if expires_at <= now:
        raise InvalidTurnIdentity("turn_expired")
    return expires_at
