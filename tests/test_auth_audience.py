from __future__ import annotations

from forgeflow.api.auth import issue_cookie, verify_cookie
from forgeflow.api.settings import create_settings

SETTINGS = create_settings({
    "FORGEFLOW_SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "FORGEFLOW_INVITE_CODES": "invite-code-for-tests-0001",
})


def test_cookie_audience_is_bound_and_replay_rejected():
    cookie = issue_cookie(SETTINGS, "invite-code-for-tests-0001", audience="app.local:8000")
    assert verify_cookie(SETTINGS, cookie, audience="app.local:8000")
    assert verify_cookie(SETTINGS, cookie, audience="other.local:8000") is None
    assert verify_cookie(SETTINGS, cookie) is None


def test_cookie_without_audience_still_works_for_direct_clients():
    cookie = issue_cookie(SETTINGS, "invite-code-for-tests-0001")
    assert verify_cookie(SETTINGS, cookie)
    assert verify_cookie(SETTINGS, cookie, audience="app.local:8000") is None
