from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from forgeflow.api import dsh_sdk
from forgeflow.api.dsh_harness import MAX_KNOWLEDGE_PASSAGES, DshHarness, DshUnavailable


class LaunchInspection(BaseModel):
    model_config = ConfigDict(extra="allow")


@pytest.fixture
def synthetic_sdk_cli(tmp_path):
    """A local JSON-RPC peer, launched by the real locked SDK; no provider/ERP."""
    path = tmp_path / "synthetic cli.py"
    path.write_text('''import json, os, sys
initialized = None
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        initialized = request["params"]
    result = {"argv": sys.argv[1:], "cwd": os.getcwd(), "initialized": initialized,
              "home": os.environ.get("DSH_HOME"),
              "permission_mode": os.environ.get("DSH_PERMISSION_MODE"),
              "evidence": os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE")} if method == "inspect" else {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    if method == "shutdown":
        break
''')
    return path


def test_pinned_sdk_supports_explicit_launch_argv():
    from importlib.metadata import version
    from inspect import signature

    from deepseek_harness import DeepSeekHarness

    assert version("deepseek-harness-sdk") == "0.2.1rc2"
    assert "_launch_args" in signature(DeepSeekHarness).parameters


def test_shared_sdk_runtime_starts_in_parallel_without_publishing_executables(tmp_path, synthetic_sdk_cli):
    from concurrent.futures import ThreadPoolExecutor

    def start(index):
        patch = tmp_path / f"owner {index}.json"
        patch.write_text("[]")
        harness = dsh_sdk.SdkDshHarness(cli=synthetic_sdk_cli, patch=patch,
            home=tmp_path / "shared home", workspace=tmp_path, node=sys.executable,
            model="synthetic-model", reasoning_effort="low")
        harness.evidence_path = tmp_path / f"scope-{index}.json"
        try:
            runtime = harness._runtime()
            # This is the real DeepSeekHarness/HarnessClient initialization and
            # subprocess, not a replacement constructor or subprocess mock.
            view = runtime.client.request("inspect", None, response_model=LaunchInspection).model_dump()
            assert view["argv"] == ["--profile", "sdk", "--patch", str(patch.resolve())]
            assert view["home"] == str((tmp_path / "shared home").resolve())
            assert view["cwd"] == str(tmp_path.resolve())
            assert view["permission_mode"] == "read-only"
            assert view["evidence"] == str(harness.evidence_path)
            assert view["initialized"] == {"cwd": str(tmp_path.resolve()), "provider": harness.provider,
                                            "model": "synthetic-model", "reasoningEffort": "low"}
            assert runtime.client.config.request_timeout_seconds == harness.timeout
            return index
        finally:
            harness.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(start, range(32))) == list(range(32))
    assert not (tmp_path / "dsh-sdk-wrapper.sh").exists()
    assert not list(tmp_path.glob(".dsh-wrapper-*"))


def test_sdk_runtime_reads_cli_without_executing_a_writable_script(tmp_path, synthetic_sdk_cli):
    # Linux rejects execve(script) with ETXTBSY while any process retains a
    # writer. An interpreter may read the same source; its executable is stable.
    descriptor = os.open(synthetic_sdk_cli, os.O_WRONLY)
    harness = dsh_sdk.SdkDshHarness(cli=synthetic_sdk_cli, patch=tmp_path / "patch.json",
                                  home=tmp_path / "home", workspace=tmp_path, node=sys.executable)
    try:
        view = harness._runtime().client.request("inspect", None, response_model=LaunchInspection)
        assert view.model_dump()["permission_mode"] == "read-only"
    finally:
        harness.close()
        os.close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="requires Unix symlink")
def test_sdk_runtime_ignores_legacy_launcher_symlink(tmp_path, synthetic_sdk_cli):
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("preserve")
    legacy = tmp_path / "dsh-sdk-wrapper.sh"
    legacy.symlink_to(unrelated)
    harness = dsh_sdk.SdkDshHarness(cli=synthetic_sdk_cli, patch=tmp_path / "patch.json",
                                  home=tmp_path / "home", workspace=tmp_path, node=sys.executable)
    try:
        harness._runtime()
        assert legacy.is_symlink() and unrelated.read_text() == "preserve"
    finally:
        harness.close()


def test_deterministic_session_id_is_stable_and_key_scoped() -> None:
    first = dsh_sdk._session_id_for("uid-a:conv-1")
    assert first == dsh_sdk._session_id_for("uid-a:conv-1")
    assert first != dsh_sdk._session_id_for("uid-b:conv-1")
    assert first.startswith("session-") and len(first) == 40


def test_sdk_events_map_to_bounded_activity_and_final_text() -> None:
    final, activity, session_id, passages, citations = dsh_sdk._events_to_view([
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
    assert passages == [] and citations == []


def test_project_knowledge_passages_are_propagated() -> None:
    payload = json.dumps({
        "found": True,
        "passages": [{"path": "docs/TECHNICAL_GUIDE.md", "start_line": 7,
                      "end_line": 7, "text": "DSH runs the read-only loop."}],
        "citations": ["docs/TECHNICAL_GUIDE.md:7-7"],
    })
    final, activity, session_id, passages, citations = dsh_sdk._events_to_view([
        {"type": "tool/call", "data": {"callId": "k1",
         "name": "mcp__forgeflow__project_knowledge", "arguments": "{}"}},
        {"type": "tool/result", "data": {"message": {"toolCallId": "k1", "isError": False,
         "content": [{"type": "text", "text": payload}]}}},
        {"type": "assistant/message", "data": {"message": {"content": [
            {"type": "text", "text": "DSH is the read-only loop."},
        ]}}},
    ])
    assert activity[0]["status"] == "done"
    assert passages == [{"path": "docs/TECHNICAL_GUIDE.md", "start_line": 7,
                         "end_line": 7, "text": "DSH runs the read-only loop."}]
    assert citations == ["docs/TECHNICAL_GUIDE.md:7-7"]


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


def test_scoped_sdk_distinguishes_home_and_case_without_extra_model_turn(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from forgeflow.api.app import _case_question_prompt, _home_question_prompt
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
        _home_question_prompt("What is this project?"),
        _case_question_prompt({"plan": {"new_spend": 133394.07}}, "Explain this amount."),
    ):
        harness._reply(question, "same-chat")
    assert len(sent) == 2
    assert PLANNED_COST_CONTEXT not in sent[0]
    assert sent[0].endswith("Question: What is this project?")
    assert sent[1].count(PLANNED_COST_CONTEXT) == 1


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


def test_sdk_reply_merges_tool_citations_and_passages(tmp_path) -> None:
    payload = json.dumps({
        "found": True,
        "passages": [{"path": "docs/TECHNICAL_GUIDE.md", "start_line": 2,
                      "end_line": 2, "text": "DSH runs the read-only loop."}],
        "citations": ["docs/TECHNICAL_GUIDE.md:2-2"],
    })
    result = SimpleNamespace(
        events=[
            {"type": "tool/call", "data": {"callId": "k1",
             "name": "mcp__forgeflow__project_knowledge", "arguments": "{}"}},
            {"type": "tool/result", "data": {"message": {"toolCallId": "k1", "isError": False,
             "content": [{"type": "text", "text": payload}]}}},
            {"type": "assistant/message", "data": {"message": {"content": [
                {"type": "text", "text": "DSH is the read-only loop."},
            ]}}},
        ],
        final_response="DSH is the read-only loop.",
        session_id="s-1",
    )

    class Session:
        def run(self, question, on_notification=None):
            return result

    harness = dsh_sdk.SdkDshHarness(
        cli=tmp_path / "cli", patch=tmp_path / "patch", home=tmp_path,
        workspace=tmp_path, knowledge_root=tmp_path)
    harness._session = lambda key: Session()
    reply, _ = harness._reply("question", "key")
    assert reply.citations == ["docs/TECHNICAL_GUIDE.md:2-2"]
    assert reply.passages == [{"path": "docs/TECHNICAL_GUIDE.md", "start_line": 2,
                               "end_line": 2, "text": "DSH runs the read-only loop."}]


def _sdk_source(name, version=1):
    return {"path": f"document://reference/{name}.md", "start_line": 1, "end_line": 4,
            "document_id": name, "document_version": version, "locator": "md:l1",
            "text": f"Captured {name} revision {version}", "source_metadata": {
                "title": f"Reference {name} revision {version}",
                "source_version": f"revision {version}",
                "original_url": f"https://example.org/{name}.pdf",
                "material_class": "industry_reference"}}


@pytest.mark.parametrize("stream", [False, True])
def test_sdk_and_cli_share_bounded_multisearch_sources_and_captured_versions(tmp_path, monkeypatch, stream):
    """The real SDK reply/stream path must not lose a later cited specification."""
    sdk_events, cli_events = [], []
    searches = [
        ("first", [_sdk_source(f"source-{index}") for index in range(20)], False),
        ("second", [_sdk_source(f"source-{index}") for index in range(20, 40)], False),
        ("versions", [_sdk_source("revision", 1), _sdk_source("revision", 3)], False),
        ("failed", [_sdk_source("failed-source")], True),
    ]
    for call_id, passages, failed in searches:
        payload = {"passages": passages,
                   "citations": [f"{row['path']}#{row['locator']}" for row in passages]}
        sdk_events += [
            {"type": "tool/call", "data": {"callId": call_id,
             "name": "mcp__forgeflow__project_knowledge", "arguments": "{}"}},
            {"type": "tool/result", "data": {"message": {"toolCallId": call_id,
             "isError": failed, "content": [{"type": "text", "text": json.dumps(payload)}]}}},
        ]
        cli_events += [
            {"type": "tool_call", "callId": call_id, "tool": "mcp__forgeflow__project_knowledge", "input": {}},
            {"type": "tool_result", "callId": call_id, "status": "error" if failed else "completed",
             "result": json.dumps(payload)},
        ]
    final = "Compare https://example.org/source-39.pdf with revision.md; retain each captured edition."
    sdk_events.append({"type": "assistant/message", "data": {"message": {
        "content": [{"type": "text", "text": final}]}}})
    cli_events.append({"type": "final", "text": final})
    result = SimpleNamespace(events=sdk_events, final_response=final, session_id="synthetic")

    class Session:
        def run(self, question, **kwargs):
            return result

    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path,
                                  workspace=tmp_path, knowledge_root=tmp_path)
    monkeypatch.setattr(harness, "_session", lambda _key: Session())
    monkeypatch.setattr(harness, "configured", lambda: True)
    if stream:
        event = list(harness.stream("Compare captured specifications", "test"))[-1]
        passages, citations = event["passages"], event["citations"]
    else:
        reply, _ = harness._reply("Compare captured specifications", "test")
        passages, citations = reply.passages, reply.citations

    cli = DshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path,
                     workspace=tmp_path, knowledge_root=tmp_path)
    cli_reply = cli._parse("\n".join(json.dumps(event) for event in cli_events))
    assert passages == cli_reply.passages and citations == cli_reply.citations
    assert len(passages) == MAX_KNOWLEDGE_PASSAGES
    assert passages[0]["document_id"] == "source-39"
    editions = [row for row in passages if row["document_id"] == "revision"]
    assert {row["document_version"] for row in editions} == {1, 3}
    for row in editions:
        assert row["text"].endswith(f"revision {row['document_version']}")
        assert row["source_metadata"]["source_version"] == f"revision {row['document_version']}"
    assert "document://reference/source-39.md#md:l1" in citations
    assert "failed-source" not in json.dumps({"passages": passages, "citations": citations})


def test_final_assistant_message_joins_all_text_blocks():
    from forgeflow.api.dsh_sdk import _events_to_view

    events = [
        {"type": "assistant/message", "data": {"message": {"content": [
            {"type": "text", "text": "intermediate note"},
        ]}}},
        {"type": "tool/call", "data": {"callId": "k", "name": "project_knowledge", "arguments": "{}"}},
        {"type": "tool/result", "data": {"message": {"toolCallId": "k", "isError": False,
            "content": [{"type": "text", "text": "{}"}]}}},
        {"type": "assistant/message", "data": {"message": {"content": [
            {"type": "text", "text": "计划成本为600元。"},
            {"type": "text", "text": "这只是计划，尚未执行。"},
        ]}}},
    ]
    final, _activity, _sid, _passages, _citations = _events_to_view(events)
    assert final == "计划成本为600元。\n这只是计划，尚未执行。"
    assert "intermediate note" not in final


def test_stream_cancellation_waits_for_the_inner_provider_worker(tmp_path):
    import threading
    import time
    from types import SimpleNamespace

    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()

    cli = tmp_path / "cli"
    patch = tmp_path / "patch"
    cli.write_text("")
    patch.write_text("[]")
    harness = dsh_sdk.SdkDshHarness(cli=cli, patch=patch, home=tmp_path, workspace=tmp_path)

    class Session:
        def run(self, question, **_kwargs):
            entered.set()
            release.wait(5)
            return SimpleNamespace(events=[], final_response="late", session_id=None)

    harness._session = lambda key: Session()
    stream = harness.stream("question", "key", cancel_event=cancel)
    consumer = threading.Thread(target=lambda: list(stream), daemon=True)
    consumer.start()
    assert entered.wait(2)
    cancel.set()
    time.sleep(0.2)
    assert consumer.is_alive()
    release.set()
    consumer.join(2)
    assert not consumer.is_alive()


def test_cancellation_before_dispatch_never_calls_provider(tmp_path):
    import threading

    entered = threading.Event()
    cleanup = threading.Event()
    lock = threading.Lock()
    calls = []

    class Session:
        def run(self, question, **_kwargs):
            calls.append(question)
            return object()

    harness = dsh_sdk.SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)

    def session(_key):
        entered.set()
        return Session()

    harness._session = session
    lock.acquire()
    errors = []

    def worker():
        try:
            harness._reply("question", "key", dispatch_lock=lock, cleanup_requested=cleanup)
        except DshUnavailable as error:
            errors.append(str(error))

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(2)
    cleanup.set()
    lock.release()
    thread.join(2)
    assert not thread.is_alive()
    assert calls == []
    assert errors == ["turn_cancelled"]


def test_reasoning_effort_is_carried_from_configuration(tmp_path, monkeypatch):
    harness = dsh_sdk.SdkDshHarness(
        cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path,
        reasoning_effort="max",
    )
    assert harness.reasoning_effort == "max"
    monkeypatch.setenv("FORGEFLOW_DSH_SDK", "1")
    monkeypatch.setenv("FORGEFLOW_DSH_ENABLED", "1")
    monkeypatch.setenv("FORGEFLOW_DSH_CLI", str(tmp_path / "cli"))
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", str(tmp_path / "patch"))
    monkeypatch.setenv("FORGEFLOW_DSH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FORGEFLOW_DSH_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("FORGEFLOW_DSH_REASONING_EFFORT", "max")
    import sys

    monkeypatch.setitem(sys.modules, "deepseek_harness", type(sys)("deepseek_harness"))
    loaded = dsh_sdk.SdkDshHarness.from_env()
    assert loaded is not None and loaded.reasoning_effort == "max"


def test_sdk_reply_includes_web_sources_from_final_and_activity(tmp_path) -> None:
    result = SimpleNamespace(
        events=[
            {"type": "assistant/message", "data": {"message": {"content": [
                {"type": "text", "text": "See https://example.com/source for details."},
            ]}}},
        ],
        final_response="See https://example.com/source for details.",
        session_id="s-web",
    )

    class Session:
        def run(self, question, on_notification=None):
            return result

    harness = dsh_sdk.SdkDshHarness(
        cli=tmp_path / "cli", patch=tmp_path / "patch", home=tmp_path,
        workspace=tmp_path, knowledge_root=tmp_path)
    harness._session = lambda key: Session()
    reply, _ = harness._reply("question", "key")
    assert "https://example.com/source" in reply.citations
