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
import shlex
import threading
import time
from pathlib import Path
from typing import Any

from .case_qa import PLANNED_COST_CONTEXT
from .dsh_harness import (
    MAX_ACTIVITY,
    DshReply,
    DshUnavailable,
    _citations,
    _finish_tool_step,
    _short,
    _tool_step,
)


def _session_id_for(key: str) -> str:
    """Stable DSH session id for one owner-scoped conversation key."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"session-{digest}"


def _events_to_view(events: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str | None]:
    """Map SDK session events to ForgeFlow's bounded activity/answer contract."""
    activity: list[dict[str, Any]] = []
    tools: dict[str, dict[str, Any]] = {}
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
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    final = block["text"]
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
        elif kind == "session" and isinstance(event.get("sessionId"), str):
            session_id = event["sessionId"]
    return final, activity[:MAX_ACTIVITY], session_id


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
        model: str = "nvidia/Nemotron-3.5-Super-VL",
        timeout: float = 180.0,
        sessions_file: Path | None = None,
        queue_timeout: float = 60.0,
    ) -> None:
        self.cli = Path(cli)
        self.patch = Path(patch)
        self.home = Path(home)
        self.workspace = Path(workspace)
        self.node = node
        self.model = model
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
        return cls(
            cli=Path(cli), patch=Path(patch),
            home=Path(os.environ.get("FORGEFLOW_DSH_HOME", ".runtime/dsh-home")),
            workspace=Path(os.environ.get("FORGEFLOW_DSH_WORKSPACE", ".runtime/assistant-workspace")),
            node=os.environ.get("FORGEFLOW_DSH_NODE", "node"),
            model=os.environ.get("FORGEFLOW_DSH_MODEL", "nvidia/Nemotron-3.5-Super-VL"),
        )

    def with_evidence(self, evidence: Path):
        from .dsh_permissions import scoped_patch
        try:
            patch = scoped_patch(self.patch, evidence)
        except (OSError, ValueError):
            raise DshUnavailable("dsh_permissions_unavailable") from None
        scoped = type(self)(
            cli=self.cli, patch=patch, home=self.home, workspace=self.workspace,
            node=self.node, model=self.model, timeout=self.timeout,
            sessions_file=evidence.with_suffix(".sessions.json"),
        )
        scoped.evidence_path = evidence
        return scoped

    def close(self) -> None:
        if self._harness is not None:
            self._harness.close()
            self._harness = None
        self._sessions.clear()

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

    def _wrapper(self) -> Path:
        path = self.home.parent / "dsh-sdk-wrapper.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '#!/bin/sh\nexec ' + shlex.quote(self.node) + " " + shlex.quote(str(self.cli)) + ' "$@"\n',
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def _runtime(self):
        if self._harness is not None:
            return self._harness
        from deepseek_harness import DeepSeekHarness

        env = dict(os.environ)
        env["DSH_HOME"] = str(self.home)
        env["DSH_PERMISSION_MODE"] = "read-only"
        env.pop("FORGEFLOW_ASSISTANT_CASES", None)
        env.pop("FORGEFLOW_ASSISTANT_RUNS", None)
        env["FORGEFLOW_ASSISTANT_EVIDENCE"] = str(getattr(self, "evidence_path", ""))
        self._harness = DeepSeekHarness(
            dsh_home=str(self.home),
            cwd=str(self.workspace),
            runtime_cwd=str(self.workspace),
            dsh_bin=str(self._wrapper()),
            profile="sdk",
            patches=(str(self.patch),),
            provider="nebius",
            model=self.model,
            env=env,
            request_timeout_seconds=self.timeout,
        )
        self._harness.start()
        return self._harness

    def _session(self, key: str):
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
               *, stop_event=None) -> tuple[DshReply, Any]:
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
            prompt = question
            if hasattr(self, "evidence_path") and PLANNED_COST_CONTEXT not in prompt:
                prompt = f"ForgeFlow evidence field meaning: {PLANNED_COST_CONTEXT}\n\nQuestion: {question}"
            check_cancelled()
            result = session.run(prompt, on_notification=on_notification)
        finally:
            turn_lock.release()
        final, activity, sid = _events_to_view(result.events)
        final = final or result.final_response
        if not final:
            raise DshUnavailable("dsh_no_final")
        citations = _citations(final, self.workspace)
        for step in activity:
            for value in (step.get("label"), step.get("detail")):
                for found in _citations(str(value or ""), self.workspace):
                    if found not in citations:
                        citations.append(found)
        scope = "project" if citations or any(
            word in final.lower() for word in ("forgeflow", "approval", "executor", "verifier")
        ) else "general"
        return DshReply(
            answer=final,
            citations=citations[:5],
            activity=activity,
            session_id=sid or getattr(result, "session_id", None),
            scope=scope,
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
                                            cancel_event=cancel_event, stop_event=stop)
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
                    _, activity, _ = _events_to_view(value)
                    if activity != last_activity:
                        last_activity = activity
                        yield {"type": "trace", "activity": activity}
                elif kind == "final":
                    reply = value
                    yield {
                        "type": "final", "found": True, "mode": "harness",
                        "answer": reply.answer, "citations": reply.citations,
                        "passages": [], "activity": reply.activity,
                        "scope": reply.scope, "session_id": reply.session_id,
                    }
                    return
                else:
                    raise value if isinstance(value, DshUnavailable) else DshUnavailable("dsh_failed")
        finally:
            # Also stop queued work when a consumer closes the generator.
            stop.set()
