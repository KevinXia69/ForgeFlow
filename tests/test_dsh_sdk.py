from __future__ import annotations

import pytest

from forgeflow.api import dsh_sdk
from forgeflow.api.dsh_harness import DshUnavailable


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


def test_stream_timeout_keeps_a_queued_turn_from_reaching_the_model(tmp_path):
    """The stream may give up before the queue deadline; that must still stop the send.

    With a stream timeout shorter than the queue deadline the caller sees
    dsh_timeout while the worker is still queued. Releasing the lock later must
    not turn that into a model call nobody asked for.
    """
    import threading
    import time
    from types import SimpleNamespace

    calls = []
    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path,
                                     workspace=tmp_path, timeout=0.3, queue_timeout=5.0)
    harness.configured = lambda: True

    class Session:
        def run(self, question, **_kwargs):
            calls.append(question)
            return SimpleNamespace(events=[{"type": "final", "text": question}],
                                   final_response=question, session_id="synthetic")

    harness._session = lambda _key: Session()
    lock = harness._turn_locks.setdefault("busy-chat", threading.Lock())
    lock.acquire()
    try:
        with pytest.raises(DshUnavailable, match="dsh_timeout"):
            list(harness.stream("queued", "busy-chat"))
        lock.release()
        time.sleep(0.5)
        assert calls == []
    finally:
        if lock.locked():
            lock.release()
