"""DeepSeek Harness Python SDK adapter for ForgeFlow.

The SDK owns a persistent JSON-RPC runtime, so sessions can use explicit ids
and survive an API process restart. The legacy one-shot CLI adapter remains
available when the SDK/runtime is not installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .answer_text import clean_answer
from .dsh_harness import (
    MAX_ACTIVITY,
    DshReply,
    DshUnavailable,
    _answer_sources,
    _finish_tool_step,
    _knowledge_passages,
    _result_payload,
    _result_text,
    _short,
    _tool_step,
)
from .model_settings import assistant_model


def _session_id_for(key: str) -> str:
    """Stable DSH session id for one owner-scoped conversation key."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"session-{digest}"


def _isolated_sdk_client(config, launch_args):
    """Keep SDK transport/lifecycle, replace only its ambient-env spawn policy.

    SDK 0.2.1rc2 HarnessClient.start merges config.env into os.environ. That is
    unsuitable for an evidence-only child. The installed version and real
    initialize/shutdown behavior are pinned by subprocess regression tests.
    No global environment mutation or subprocess monkeypatch is involved.
    """
    from importlib.metadata import version

    from deepseek_harness.client import HarnessClient

    if version("deepseek-harness-sdk") != "0.2.1rc2":
        raise DshUnavailable("dsh_sdk_version_unsupported")

    class IsolatedHarnessClient(HarnessClient):
        def start(self):
            if self._proc is not None:
                return
            with self._lock:
                self._session_parents.clear()
            self._proc = subprocess.Popen(
                list(launch_args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                cwd=None if self.config.cwd is None else str(Path(self.config.cwd).resolve()),
                env=dict(self.config.env or {}), bufsize=1)
            try:
                self._start_reader_thread()
                self._start_stderr_thread()
            except Exception:
                self.close()
                raise

    return IsolatedHarnessClient(config, _launch_args=launch_args)


def _events_to_view(events: list[dict[str, Any]]) -> tuple[
        str, list[dict[str, Any]], str | None, list[dict[str, Any]], list[str]]:
    """Map SDK session events to ForgeFlow's bounded activity/answer contract."""
    activity: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
    passages: list[dict[str, Any]] = []
    tool_citations: list[str] = []
    final = ""
    session_id: str | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            content = message.get("content")
            if not isinstance(content, list):
                continue
            texts = [
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            if texts:
                # Only the last assistant message is the final answer; join its
                # text blocks instead of overwriting earlier blocks.
                final = "\n".join(texts)
        elif kind == "tool/call":
            call_id = str(data.get("callId", ""))
            tool = _short(data.get("name"), 40) or "tool"
            arguments = data.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            step = _tool_step(tool, arguments)
            tools[call_id] = step
            activity.append(step)
        elif kind == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            call_id = str(message.get("toolCallId", ""))
            step = tools.get(call_id)
            if step is not None:
                _finish_tool_step(step, {
                    "status": "error" if message.get("isError") is True else "completed",
                    "result": message.get("content"),
                })
                if step["status"] == "done":
                    payload = _result_payload(_result_text(message.get("content")))
                    new_passages, new_citations = _knowledge_passages(
                        str(step.get("kind", "")), payload)
                    passages.extend(new_passages)
                    tool_citations.extend(new_citations)
        elif kind == "session" and isinstance(event.get("sessionId"), str):
            session_id = event["sessionId"]
    # The shared final selector needs all searches to retain a later cited
    # specification. Each tool result is bounded by _knowledge_passages.
    return clean_answer(final), activity[:MAX_ACTIVITY], session_id, passages, tool_citations


class SdkDshHarness:
    """Persistent SDK-backed harness with explicit session ids."""

    def __init__(
        self,
        *,
        cli: Path,
        patch: Path,
        home: Path,
        workspace: Path,
        node: str = "node",
        model: str = "deepseek-flash",
        timeout: float = 180.0,
        sessions_file: Path | None = None,
        queue_timeout: float = 60.0,
        knowledge_root: Path | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.cli = Path(cli)
        self.patch = Path(patch)
        self.home = Path(home)
        self.workspace = Path(workspace)
        self.node = node
        self.model = model
        self.reasoning_effort = (reasoning_effort or "").strip() or None
        self.provider = assistant_model()["provider"]
        self.knowledge_root = Path(knowledge_root) if knowledge_root else self.workspace
        self.timeout = timeout
        self.queue_timeout = queue_timeout
        self.sessions_file = sessions_file or (workspace.parent / "dsh-sdk-sessions.json")
        self._configured: dict[str, str] = self._load()
        self._sessions: dict[str, Any] = {}
        self._harness: Any = None
        self._lock = threading.Lock()
        self._turn_locks: dict[str, threading.Lock] = {}

    supports_turn_cancellation = True

    @classmethod
    def from_env(cls) -> "SdkDshHarness | None":
        if os.environ.get("FORGEFLOW_DSH_SDK", "").lower() not in ("1", "true", "yes"):
            return None
        try:
            import deepseek_harness  # noqa: F401
        except ImportError:
            return None
        if os.environ.get("FORGEFLOW_DSH_ENABLED", "").lower() not in ("1", "true", "yes"):
            return None
        cli = os.environ.get("FORGEFLOW_DSH_CLI", "").strip()
        patch = os.environ.get("FORGEFLOW_DSH_PATCH", "").strip()
        if not cli or not patch:
            return None
        from .dsh_environment import runtime_directories

        home, workspace = runtime_directories(os.environ)
        return cls(
            cli=Path(cli), patch=Path(patch),
            home=home, workspace=workspace,
            node=os.environ.get("FORGEFLOW_DSH_NODE", "node"),
            model=os.environ.get("FORGEFLOW_DSH_MODEL", "deepseek-flash"),
            reasoning_effort=os.environ.get("FORGEFLOW_DSH_REASONING_EFFORT"),
        )

    def with_evidence(self, evidence: Path):
        from .dsh_environment import share_scoped_patch
        from .dsh_permissions import scoped_image_root, scoped_patch
        image_root = scoped_image_root(self.workspace, evidence)
        try:
            patch = scoped_patch(self.patch, evidence, image_root=image_root,
                image_tool_module=self.cli.parent.parent.parent / "dsh-tool-fs/lib/index.js",
                reasoning_effort=self.reasoning_effort)
            share_scoped_patch(patch)
        except (OSError, ValueError):
            raise DshUnavailable("dsh_permissions_unavailable") from None
        scoped = type(self)(
            cli=self.cli, patch=patch, home=self.home, workspace=self.workspace,
            node=self.node, model=self.model, timeout=self.timeout,
            sessions_file=evidence.with_suffix(".sessions.json"),
            knowledge_root=self.knowledge_root,
            reasoning_effort=self.reasoning_effort,
        )
        scoped.evidence_path = evidence
        scoped.image_root = image_root
        scoped._composition_source = self.patch
        return scoped

    def with_model(self, settings):
        from .dsh_permissions import bind_model_harness

        return bind_model_harness(self, settings)

    def close(self) -> None:
        from .dsh_permissions import close_model_harness

        runtime = self._harness
        try:
            if self._harness is not None:
                self._harness.close()
        finally:
            if runtime is not None and hasattr(self, "_owned_model_files"):
                # The SDK keeps its launch environment in two config objects.
                # Reap first; then release our optional credential references.
                for config in (runtime.config, runtime.client.config):
                    if isinstance(config.env, dict):
                        config.env.pop("FORGEFLOW_CHAT_API_KEY", None)
            self._harness = None
            self._sessions.clear()
            close_model_harness(self)

    def configured(self) -> bool:
        return self.cli.is_file() and self.patch.is_file() and self.workspace.is_dir()

    def _load(self) -> dict[str, str]:
        try:
            value = json.loads(self.sessions_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {key: item for key, item in value.items() if isinstance(item, str)}

    def _save(self) -> None:
        self.sessions_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.sessions_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._configured, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.sessions_file)

    def _launch_args(self) -> tuple[str, ...]:
        # The pinned SDK 0.2.1rc2 accepts this explicit argv. Its underscored
        # constructor option is protected by real-SDK startup/version tests.
        # Keep self.node: production uses the installed privilege-reducing
        # launcher here, not a bare node binary. Never execute a writable,
        # per-request script (even atomic rename can hit Linux ETXTBSY when a
        # concurrent fork briefly inherits the temporary writer descriptor).
        return (self.node, str(self.cli.expanduser().resolve()), "--profile", "sdk",
                "--patch", str(self.patch.expanduser().resolve()))

    def _runtime(self):
        if getattr(self, "_model_closed", False):
            raise DshUnavailable("dsh_model_binding_closed")
        if self._harness is not None:
            return self._harness
        from deepseek_harness import DeepSeekHarness

        from .dsh_environment import assistant_environment

        home = self.home.expanduser().resolve()
        env = assistant_environment(os.environ, home=home,
                                    evidence=getattr(self, "evidence_path", ""),
                                    model_api_key=getattr(self, "_model_api_key", None))
        self._harness = DeepSeekHarness(
            _launch_args=self._launch_args(),
            dsh_home=str(home),
            cwd=str(self.workspace),
            runtime_cwd=str(self.workspace),
            profile="sdk",
            patches=(str(self.patch),),
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            env=env,
            request_timeout_seconds=self.timeout,
        )
        self._harness._client = _isolated_sdk_client(
            self._harness.client.config, self._launch_args())
        try:
            self._harness.start()
        except Exception:
            try:
                self._harness.close()
            finally:
                self._harness = None
            raise
        return self._harness

    def _session(self, key: str):
        if getattr(self, "_session_namespace", None):
            key = f"{self._session_namespace}:{key}"
        if hasattr(self, "evidence_path"):
            key = f"{self.evidence_path.stem}:{key}"
        with self._lock:
            cached = self._sessions.get(key)
            if cached is not None:
                return cached
            sid = self._configured.get(key) or _session_id_for(key)
            harness = self._runtime()
            try:
                if key in self._configured:
                    session = harness.resume_session(sid)
                else:
                    session = harness.start_session(sid)
            except Exception:
                session = harness.start_session(sid)
            self._sessions[key] = session
            self._configured[key] = sid
            self._save()
            return session

    def _reply(self, question: str, key: str, on_notification=None, cancel_event=None,
               *, stop_event=None, on_provider_start=None, dispatch_lock=None,
               cleanup_requested=None) -> tuple[DshReply, Any]:
        # The SDK has no per-session interrupt operation. Finish an in-flight
        # turn before admitting another prompt; cancelled queued turns never
        # send. Output cancellation does not claim provider billing stopped.
        def check_cancelled() -> None:
            if any(event is not None and event.is_set() for event in (cancel_event, stop_event)):
                raise DshUnavailable("turn_cancelled")

        check_cancelled()
        with self._lock:
            turn_lock = self._turn_locks.setdefault(key, threading.Lock())
        deadline = time.monotonic() + self.queue_timeout
        while not turn_lock.acquire(timeout=0.1):
            check_cancelled()
            # A queued turn must not wait forever: if the turn ahead of it never
            # finishes, give up before any model call is made.
            if time.monotonic() >= deadline:
                raise DshUnavailable("turn_queue_timeout")
        try:
            check_cancelled()
            session = self._session(key)
            if dispatch_lock is None:
                dispatch_lock = threading.Lock()
            with dispatch_lock:
                # Cleanup takes this same lock before checking whether a
                # provider call has started. Cancellation therefore either
                # wins before dispatch or marks the call as started and waits
                # for it; it cannot fall between the check and the dispatch.
                if cleanup_requested is not None and cleanup_requested.is_set():
                    raise DshUnavailable("turn_cancelled")
                check_cancelled()
                if on_provider_start is not None:
                    on_provider_start()
            result = session.run(question, on_notification=on_notification)
        finally:
            turn_lock.release()
        final, activity, sid, passages, tool_citations = _events_to_view(result.events)
        final = clean_answer(final or result.final_response)
        if not final:
            raise DshUnavailable("dsh_no_final")
        passages, citations = _answer_sources(final, activity, self.knowledge_root,
                                             passages, tool_citations)
        scope = "project" if citations or any(
            word in final.lower() for word in ("forgeflow", "approval", "executor", "verifier")
        ) else "general"
        return DshReply(
            answer=final,
            citations=citations,
            activity=activity,
            session_id=sid or getattr(result, "session_id", None),
            scope=scope,
            passages=passages,
        ), result

    def ask(self, question: str, conversation_id: str | None = None) -> DshReply:
        if not self.configured():
            raise DshUnavailable("dsh_not_configured")
        try:
            reply, _ = self._reply(question, conversation_id or "")
            return reply
        except DshUnavailable:
            raise
        except Exception as error:
            raise DshUnavailable("dsh_failed") from error

    def stream(self, question: str, conversation_id: str | None = None, *, cancel_event=None):
        if not self.configured():
            raise DshUnavailable("dsh_not_configured")
        key = conversation_id or ""
        events: list[dict[str, Any]] = []
        queue_in: queue.Queue = queue.Queue()
        # The worker checks both events directly. No extra thread needs to wait
        # for an external cancellation that may never arrive after completion.
        stop = threading.Event()
        provider_started = threading.Event()
        cleanup_requested = threading.Event()
        dispatch_lock = threading.Lock()

        def on_notification(notification) -> None:
            if stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
                return
            payload = notification.payload
            event = payload.get("event")
            if not isinstance(event, dict):
                return
            events.append(event)
            queue_in.put(("trace", events))

        def worker() -> None:
            try:
                reply, result = self._reply(question, key, on_notification=on_notification,
                                            cancel_event=cancel_event, stop_event=stop,
                                            on_provider_start=provider_started.set,
                                            dispatch_lock=dispatch_lock,
                                            cleanup_requested=cleanup_requested)
                queue_in.put(("final", reply))
            except Exception as error:  # noqa: BLE001
                queue_in.put(("error", error))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        last_activity: list[dict[str, Any]] = []
        last_event_time = time.monotonic()
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return
                try:
                    kind, value = queue_in.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - last_event_time >= self.timeout:
                        raise DshUnavailable("dsh_timeout") from None
                    continue
                last_event_time = time.monotonic()
                if kind == "trace":
                    _, activity, *_ = _events_to_view(value)
                    if activity != last_activity:
                        last_activity = activity
                        yield {"type": "trace", "activity": activity}
                elif kind == "final":
                    reply = value
                    yield {
                        "type": "final", "found": True, "mode": "harness",
                        "answer": reply.answer, "citations": reply.citations,
                        "passages": reply.passages, "activity": reply.activity,
                        "scope": reply.scope, "session_id": reply.session_id,
                        "model": self.model, "provider": self.provider,
                    }
                    return
                else:
                    raise value if isinstance(value, DshUnavailable) else DshUnavailable("dsh_failed")
        finally:
            # Output cancellation must not release the execution slot while the
            # provider call is still running. Wait for the inner worker to end.
            stop.set()
            cleanup_requested.set()
            with dispatch_lock:
                started = provider_started.is_set()
            # Optional-key runtimes are closed by their request's finally block.
            # Also wait for an in-progress initialize so it cannot publish a
            # late subprocess after that close clears its files/credential.
            if (started or hasattr(self, "_owned_model_files")) and thread.is_alive():
                thread.join()
