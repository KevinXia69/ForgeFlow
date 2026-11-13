from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from forgeflow.superagent.execution import _require_current_approval


@pytest.mark.parametrize("expiry", ["", None, "unreadable", "2020-01-01T00:00:00+00:00"])
def test_unverified_or_expired_approval_cannot_be_reused(expiry):
    with pytest.raises(ValueError, match="approval_expir"):
        _require_current_approval(SimpleNamespace(detail={"approval_expires_at": expiry}))


def test_current_approval_can_be_reused():
    _require_current_approval(SimpleNamespace(detail={
        "approval_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}))
