from __future__ import annotations

import pytest

from forgeflow.api import dsh_sdk
from forgeflow.api.dsh_harness import DshUnavailable


@pytest.mark.skipif(__import__("os").name != "posix", reason="requires Unix launcher")
def test_shared_sdk_wrapper_remains_executable_during_parallel_owner_startup(tmp_path):
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    def start(index):
        harness = dsh_sdk.SdkDshHarness(cli=Path("%s"), patch=tmp_path,
            home=tmp_path / "home", workspace=tmp_path, node="/usr/bin/printf")
        result = subprocess.run([str(harness._wrapper()), str(index)],
                                capture_output=True, text=True, check=True, timeout=5)
        return result.stdout

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(start, range(32))) == [str(index) for index in range(32)]


@pytest.mark.skipif(__import__("os").name != "posix", reason="requires Unix symlink")
def test_sdk_wrapper_publication_does_not_follow_existing_symlink(tmp_path):
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    (tmp_path / "dsh-sdk-wrapper.sh").symlink_to(unrelated)
    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path,
                                  home=tmp_path / "home", workspace=tmp_path)
    result = harness._wrapper()
    assert not result.is_symlink()
    assert unrelated.read_text() == "preserve"


def test_deterministic_session_id_is_stable_and_key_scoped() -> None:
    first = dsh_sdk._session_id_for("uid-a:conv-1")
    assert first == dsh_sdk._session_id_for("uid-a:conv-1")
    assert first != dsh_sdk._session_id_for("uid-b:conv-1")
    assert first.startswith("session-") and len(first) == 40


def test_sdk_events_map_to_bounded_activity_and_final_text() -> None:
    final, activity, session_id = dsh_sdk._events_to_view([
        {"type": "tool/call", "data": {"callId": "c1", "name": "mcp__forgeflow__case_brief", "arguments": '{"case_id":"case-1"}'}},
        {"type": "tool/result", "data": {"message": {"toolCallId": "c1", "isError": False, "content": [{"type": "text", "text": "ok"}]}}},
        {"type": "assistant/message", "data": {"message": {"content": [
            {"type": "reasoning", "text": "checking"},
            {"type": "text", "text": "The case is awaiting approval."},
        ]}}},
    ])
    assert final == "The case is awaiting approval."
    assert activity[0]["label"].startswith("case")
    assert activity[0]["status"] == "done"
    assert len(activity) == 1
    assert "checking" not in str(activity)
    assert session_id is None


def test_sdk_adapter_requires_opt_in_and_configuration(monkeypatch) -> None:
    monkeypatch.delenv("FORGEFLOW_DSH_SDK", raising=False)
    assert dsh_sdk.SdkDshHarness.from_env() is None
    with pytest.raises(DshUnavailable):
        dsh_sdk.SdkDshHarness(
            cli=__import__("pathlib").Path("/nonexistent"),
            patch=__import__("pathlib").Path("/nonexistent"),
            home=__import__("pathlib").Path("/tmp"),
            workspace=__import__("pathlib").Path("/tmp"),
        ).ask("x")


def test_factory_prefers_sdk_when_opted_in(tmp_path, monkeypatch) -> None:
    import importlib.util

    from forgeflow.api.dsh_harness import DshHarness
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    patch = tmp_path / "patch.yml"
    patch.write_text("[]\n")
    cli = tmp_path / "bin.js"
    cli.write_text("")
    monkeypatch.setenv("FORGEFLOW_DSH_ENABLED", "1")
    monkeypatch.setenv("FORGEFLOW_DSH_SDK", "1")
    monkeypatch.setenv("FORGEFLOW_DSH_CLI", str(cli))
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", str(patch))
    monkeypatch.setenv("FORGEFLOW_DSH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FORGEFLOW_DSH_WORKSPACE", str(workspace))
    if importlib.util.find_spec("deepseek_harness") is None:
        return
    assert isinstance(DshHarness.from_env(), dsh_sdk.SdkDshHarness)


def test_cancelled_queued_turn_never_sends_to_busy_sdk_session(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Session:
        def run(self, question, **_kwargs):
            calls.append(question)
            entered.set()
            release.wait(2)
            return SimpleNamespace(events=[], final_response=question, session_id='synthetic')

    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    monkeypatch.setattr(harness, '_session', lambda _key: Session())
    with ThreadPoolExecutor() as pool:
        first = pool.submit(harness._reply, 'first', 'same-chat')
        assert entered.wait(2)
        cancelled.set()
        second = pool.submit(harness._reply, 'cancelled second', 'same-chat', cancel_event=cancelled)
        with pytest.raises(DshUnavailable, match='turn_cancelled'):
            second.result(timeout=2)
        release.set()
        assert first.result(timeout=2)[0].answer == 'first'
    assert calls == ['first']


def test_scoped_sdk_carries_cost_semantics_once_without_extra_model_turn(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from forgeflow.api.app import _case_question_prompt
    from forgeflow.api.case_qa import PLANNED_COST_CONTEXT

    sent = []

    class Session:
        def run(self, question, **_kwargs):
            sent.append(question)
            return SimpleNamespace(events=[], final_response="Recorded proposal.", session_id="test")

    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    harness.evidence_path = tmp_path / "scoped-evidence.json"
    monkeypatch.setattr(harness, "_session", lambda _key: Session())
    for question in (
        "What does the current case cost?",
        _case_question_prompt({"plan": {"new_spend": 133394.07}}, "Explain this amount."),
    ):
        harness._reply(question, "same-chat")
    assert len(sent) == 2
    assert all(prompt.count(PLANNED_COST_CONTEXT) == 1 for prompt in sent)
    assert sent[0].endswith("Question: What does the current case cost?")


def test_queued_turn_gives_up_before_any_model_call(tmp_path, monkeypatch):
    """A turn stuck behind a busy session must not wait forever and then send.

    The queue used to be unbounded: if the turn ahead never finished, the queued
    turn waited silently and, the moment the lock was released, called the model
    even though the caller had long gone.
    """
    import threading
    from types import SimpleNamespace

    calls = []
    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path,
                                     workspace=tmp_path, queue_timeout=0.3)

    class Session:
        def run(self, question, **_kwargs):
            calls.append(question)
            return SimpleNamespace(events=[], final_response=question, session_id="synthetic")

    monkeypatch.setattr(harness, "_session", lambda _key: Session())
    lock = harness._turn_locks.setdefault("busy-chat", threading.Lock())
    lock.acquire()
    try:
        with pytest.raises(DshUnavailable, match="turn_queue_timeout"):
            harness._reply("queued", "busy-chat", cancel_event=threading.Event())
    finally:
        lock.release()
    assert calls == []


@pytest.fixture
def sdk_threads(monkeypatch):
    """Track only this adapter's threads, without counting unrelated workers."""
    import threading
    from types import SimpleNamespace

    threads = []

    def tracked_thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(dsh_sdk, "threading", SimpleNamespace(
        Thread=tracked_thread, Event=threading.Event, Lock=threading.Lock,
    ))
    return threads


@pytest.mark.parametrize("outcome", ["success", "error", "timeout"])
def test_stream_completion_releases_threads(tmp_path, monkeypatch, sdk_threads, outcome):
    """Repeated requests must not retain a waiter on the caller's unset event.

    Timeout also covers releasing a busy session after the stream has failed:
    the queued worker must exit without making a late model call.
    """
    import threading
    from types import SimpleNamespace

    cancellations = []
    calls = []
    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path,
                                  workspace=tmp_path, timeout=0.01, queue_timeout=5.0)
    monkeypatch.setattr(harness, "configured", lambda: True)

    class Session:
        def run(self, question, **_kwargs):
            calls.append(question)
            if outcome == "error":
                raise RuntimeError("synthetic model error")
            return SimpleNamespace(events=[], final_response=question, session_id="synthetic")

    monkeypatch.setattr(harness, "_session", lambda _key: Session())
    lock = harness._turn_locks.setdefault("busy-chat", threading.Lock())
    try:
        for index in range(3):
            cancelled = threading.Event()
            cancellations.append(cancelled)
            if outcome == "timeout":
                lock.acquire()
            try:
                stream = harness.stream(f"question {index}", "busy-chat", cancel_event=cancelled)
                if outcome == "success":
                    assert list(stream)[-1]["answer"] == f"question {index}"
                else:
                    with pytest.raises(DshUnavailable, match=f"^dsh_{'failed' if outcome == 'error' else 'timeout'}$"):
                        list(stream)
            finally:
                if outcome == "timeout":
                    lock.release()
        for thread in sdk_threads:
            thread.join(timeout=1)
        assert not any(thread.is_alive() for thread in sdk_threads)
        assert all(not cancelled.is_set() for cancelled in cancellations)
        assert calls == ([] if outcome == "timeout" else [f"question {index}" for index in range(3)])
    finally:
        # Release leaked waiters too when this regression runs against old code.
        for cancelled in cancellations:
            cancelled.set()
        for thread in sdk_threads:
            thread.join(timeout=1)


@pytest.mark.parametrize("cancel_during", ["queue", "session_initialization"])
def test_stream_cancel_before_dispatch_never_calls_model(tmp_path, monkeypatch, sdk_threads, cancel_during):
    import threading
    from types import SimpleNamespace

    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    calls = []
    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    monkeypatch.setattr(harness, "configured", lambda: True)

    class Session:
        def run(self, question, **_kwargs):
            calls.append(question)
            return SimpleNamespace(events=[], final_response=question, session_id="synthetic")

    class BusyLock:
        def acquire(self, *, timeout):
            entered.set()
            return release.wait(timeout)

        def release(self):
            pass

    def session(_key):
        if cancel_during == "session_initialization":
            entered.set()
            assert release.wait(2)
        return Session()

    monkeypatch.setattr(harness, "_session", session)
    if cancel_during == "queue":
        harness._turn_locks["same-chat"] = BusyLock()
    output, errors = [], []

    def consume():
        try:
            output.extend(harness.stream("cancel me", "same-chat", cancel_event=cancelled))
        except DshUnavailable as error:
            errors.append(str(error))

    consumer = threading.Thread(target=consume)
    consumer.start()
    try:
        assert entered.wait(2)
        cancelled.set()
        consumer.join(timeout=2)
        assert not consumer.is_alive()
        release.set()
        for thread in sdk_threads:
            thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in sdk_threads)
        assert calls == []
        assert output == []
        assert errors in ([], ["turn_cancelled"])
    finally:
        cancelled.set()
        release.set()
        consumer.join(timeout=2)
        for thread in sdk_threads:
            thread.join(timeout=2)


def test_closing_stream_stops_notifications_and_releases_threads(tmp_path, monkeypatch, sdk_threads):
    import threading
    from types import SimpleNamespace

    release, cancelled = threading.Event(), threading.Event()
    notifications = []
    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    monkeypatch.setattr(harness, "configured", lambda: True)

    class Session:
        def run(self, question, *, on_notification):
            notifications.append(on_notification)
            on_notification(SimpleNamespace(payload={"event": {
                "type": "tool/call", "data": {"callId": "c1", "name": "case_brief", "arguments": "{}"},
            }}))
            assert release.wait(2)
            return SimpleNamespace(events=[], final_response=question, session_id="synthetic")

    class UnexpectedNotification:
        @property
        def payload(self):
            pytest.fail("a closed stream must ignore subsequent notifications")

    monkeypatch.setattr(harness, "_session", lambda _key: Session())
    stream = harness.stream("close me", "same-chat", cancel_event=cancelled)
    try:
        assert next(stream)["type"] == "trace"
        stream.close()
        notifications[0](UnexpectedNotification())
        assert not cancelled.is_set()
        release.set()
        for thread in sdk_threads:
            thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in sdk_threads)
    finally:
        stream.close()
        cancelled.set()
        release.set()
        for thread in sdk_threads:
            thread.join(timeout=2)
