"""Notifications interrupt only for the five needs in the v3 policy."""
from __future__ import annotations

from types import SimpleNamespace

from forgeflow.superagent.dispatcher import NOTIFY_NEEDS, Dispatcher


def _dispatcher(calls):
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.notifier = calls.append
    return dispatcher


def _context(need):
    return SimpleNamespace(tenant_id="tenant-a", ticket_id="ticket-1",
                           payload={"need": need, "body": "Please review"})


def test_the_five_interrupting_needs_are_notified() -> None:
    assert NOTIFY_NEEDS == {"fact", "rule", "approval", "failure", "result_unknown"}
    calls = []
    dispatcher = _dispatcher(calls)
    for need in sorted(NOTIFY_NEEDS):
        result = dispatcher._handle_notification(_context(need))
        assert result["status"] == "completed"
    assert len(calls) == len(NOTIFY_NEEDS)


def test_progress_and_unknown_needs_stay_in_the_timeline() -> None:
    calls = []
    dispatcher = _dispatcher(calls)
    for need in ("progress", "retry", None):
        result = dispatcher._handle_notification(_context(need))
        assert result == {"status": "suppressed", "reason": "notification_policy",
                          "need": need}
    assert calls == []
