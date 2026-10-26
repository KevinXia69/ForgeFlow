"""Local-only adapter for a DeepSeek Harness headless sidecar.

The harness owns the agent loop, tool use, context compaction and session
memory. ForgeFlow owns the read-only project workspace and the business system
of record. This module only starts a headless DSH process, parses its JSON
events, and maps them onto ForgeFlow's answer/activity envelope.

No credential, raw stderr, or provider payload is returned to the client.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .case_qa import PLANNED_COST_CONTEXT
from .evidence_budget import turn_budget

MAX_ACTIVITY = 40
MAX_LABEL = 1600
MAX_RESULT_SUMMARY = 200
MAX_RESULT_PARSE = 64_000
MCP_PREFIX = "mcp__forgeflow__"
MCP_TOOL_LABELS = {
    "case_brief": "case",
    "get_case": "case",
    "list_cases": "case",
    "get_run": "run",
    "list_runs": "run",
    "search_evidence": "evidence search",
}
_OK_RESULT_STATUS = {"ok", "success", "completed", "done", "available", "passed"}
CITATION = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]+\.(?:py|tsx|ts|md|json))(?::(?P<line>\d+))?"
)


class DshUnavailable(RuntimeError):
    """The sidecar is not configured or did not produce a final answer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DshReply:
    answer: str
    citations: list[str]
    activity: list[dict[str, Any]]
    session_id: str | None
    scope: str


def _short(value: object, limit: int = MAX_LABEL) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _tool_input(value: object) -> str:
    """Return a short, human-readable preview of a tool call's arguments.

    The original file/pattern/command/description precedence is unchanged.
    Case, run, evidence-query, path and read-offset arguments extend it so the
    read-only evidence MCP and file tools produce recognisable rows instead of
    empty ones. ``offset`` is rendered as ``path:offset`` beside a path, or as
    ``offset N`` when it is the only argument.
    """
    if not isinstance(value, dict):
        return ""
    offset = value.get("offset")
    offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else None

    file_path = value.get("file_path")
    if isinstance(file_path, str) and file_path:
        return _short(f"{file_path}:{offset}" if offset is not None else file_path)

    pattern = value.get("pattern")
    if isinstance(pattern, str) and pattern:
        path = value.get("path")
        if isinstance(path, str) and path:
            return _short(f"{pattern} in {path}")
        return _short(pattern)

    for key in ("command", "description", "case_id", "run_id", "query", "path"):
        item = value.get(key)
        if isinstance(item, str) and item:
            if key == "path" and offset is not None:
                return _short(f"{item}:{offset}")
            return _short(item)

    return f"offset {offset}" if offset is not None else ""


def _mcp_label(tool: str) -> str | None:
    """Map a ForgeFlow MCP tool name onto its user-facing action label.

    The raw tool name stays on the activity step's ``kind`` for debugging; this
    label only replaces the machine name in the visible row.
    """
    if not tool.startswith(MCP_PREFIX):
        return None
    name = tool[len(MCP_PREFIX):]
    if name in MCP_TOOL_LABELS:
        return MCP_TOOL_LABELS[name]
    return name.replace("_", " ").strip() or "forgeflow"


def _tool_label(tool: str, detail: str) -> str:
    human = _mcp_label(tool)
    if human is not None:
        return f"{human} · {detail}" if detail else human
    return f"{tool} · {detail}" if detail else tool


def _first_line(value: object) -> str:
    """Return the first non-empty, whitespace-normalised line, bounded."""
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        text = " ".join(line.split())
        if text:
            return _short(text, MAX_RESULT_SUMMARY)
    return ""


def _result_payload(value: object) -> object:
    """Parse a bounded JSON result string; leave prose and huge payloads alone."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or len(text) > MAX_RESULT_PARSE or text[0] not in "[{":
        return text
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return text


def _result_error(payload: object) -> str:
    if not isinstance(payload, dict) or "error" not in payload:
        return ""
    error = payload["error"]
    if isinstance(error, dict):
        for key in ("message", "code", "type"):
            text = _first_line(error.get(key))
            if text:
                return text
        return ""
    return _first_line(error)


def _result_status(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("status", "state"):
        value = payload.get(key)
        if isinstance(value, str):
            text = _first_line(value)
            if text and text.casefold() not in _OK_RESULT_STATUS:
                return text
    return ""


def _result_count(payload: object) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in ("count", "returned", "matched"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    for key in ("items", "results", "cases", "runs", "hits", "matches", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _result_summary(event: dict[str, Any]) -> str:
    """Describe one tool result without copying the payload into the activity.

    A non-success status or error text comes first, then
    count/truncated/total metadata. Plain text falls back to its first non-empty
    line, and every form is capped at ``MAX_RESULT_SUMMARY``. The final answer
    and recorded evidence are untouched.
    """
    event_status = event.get("status")
    payload = _result_payload(event.get("result"))
    parts: list[str] = []

    error = _result_error(payload)
    status = _result_status(payload)
    if error:
        parts.append(error if error.casefold().startswith("error") else f"error: {error}")
    elif status:
        parts.append(status)

    count = _result_count(payload)
    if count is not None:
        parts.append(f"{count} item" + ("" if count == 1 else "s"))

    if isinstance(payload, dict):
        if payload.get("truncated") is True:
            parts.append("truncated")
        total = payload.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            parts.append(f"total {total}")
    elif isinstance(payload, str):
        text = _first_line(payload)
        if text:
            parts.append(text)

    if event_status == "error":
        if parts and not parts[0].casefold().startswith("error"):
            parts[0] = f"error: {parts[0]}"
        elif not parts:
            parts.append("error")

    if not parts and isinstance(payload, dict):
        # Last resort: name a few fields, never their values.
        names = [str(key) for key in payload if isinstance(key, str)][:5]
        if names:
            parts.append(" · ".join(names))

    return _short(" · ".join(parts), MAX_RESULT_SUMMARY)


def _result_text(value: object) -> object:
    """Normalize SDK content blocks back to the JSON/text an MCP tool returned."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [
            item.get("text") for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        return _result_text(value["content"])
    return value


def _presentation(tool: str, payload: object) -> dict[str, Any]:
    """Attach bounded evidence structure for DSH SearchBlock/JsonTree."""
    if tool == f"{MCP_PREFIX}search_evidence" and isinstance(payload, list):
        rows = [
            {
                "kind": _short(item.get("kind"), 40) if isinstance(item, dict) else "",
                "id": _short(item.get("id"), 120) if isinstance(item, dict) else "",
                "excerpt": _short(item.get("excerpt"), 220) if isinstance(item, dict) else "",
            }
            for item in payload[:20]
            if isinstance(item, dict)
        ]
        return {"evidence": rows, "total": len(payload)}
    if tool in {
        f"{MCP_PREFIX}case_brief", f"{MCP_PREFIX}get_case",
        f"{MCP_PREFIX}list_cases", f"{MCP_PREFIX}get_run",
        f"{MCP_PREFIX}list_runs",
    } and isinstance(payload, (dict, list)):
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) <= 12_000:
            return {"payload": payload}
    return {}


def _tool_step(tool: str, raw_input: object) -> dict[str, Any]:
    detail = _tool_input(raw_input)
    return {"kind": tool, "label": _tool_label(tool, detail), "status": "active"}


def _finish_tool_step(step: dict[str, Any], event: dict[str, Any]) -> None:
    step["status"] = "done" if event.get("status") == "completed" else "failed"
    payload = _result_payload(_result_text(event.get("result")))
    summary = _result_summary({**event, "result": payload})
    if summary:
        step["detail"] = summary
    step.update(_presentation(str(step.get("kind", "")), payload))


def _citations(value: str, workspace: Path) -> list[str]:
    found: list[str] = []
    for match in CITATION.finditer(value):
        relative = match.group("path").lstrip("./")
        path = workspace / relative
        if path.is_file() and path.resolve().is_relative_to(workspace.resolve()):
            line = match.group("line")
            citation = f"{relative}:{line}" if line else relative
            if citation not in found:
                found.append(citation)
    return found


class DshHarness:
    supports_turn_cancellation = True

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
    ) -> None:
        self.cli = Path(cli)
        self.patch = Path(patch)
        self.home = Path(home)
        self.workspace = Path(workspace)
        self.node = node
        self.model = model
        self.timeout = timeout
        self.sessions_file = sessions_file or (workspace.parent / "dsh-sessions.json")
        self._sessions = self._load_sessions()

    @classmethod
    def from_env(cls):
        if os.environ.get("FORGEFLOW_DSH_ENABLED", "").lower() not in ("1", "true", "yes"):
            return None
        # The persistent SDK runtime is preferred when installed; the one-shot
        # CLI remains the portable fallback for review/CI images.
        try:
            from .dsh_sdk import SdkDshHarness
            sdk = SdkDshHarness.from_env()
            if sdk is not None:
                return sdk
        except ImportError:
            pass
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
        from .dsh_environment import share_scoped_patch
        from .dsh_permissions import scoped_patch
        try:
            patch = scoped_patch(self.patch, evidence)
            share_scoped_patch(patch)
        except (OSError, ValueError):
            raise DshUnavailable("dsh_permissions_unavailable") from None
        scoped = type(self)(
            cli=self.cli, patch=patch, home=self.home, workspace=self.workspace,
            node=self.node, model=self.model, timeout=self.timeout,
            sessions_file=evidence.with_suffix(".sessions.json"),
        )
        scoped.evidence_path = evidence
        return scoped

    def configured(self) -> bool:
        return self.cli.is_file() and self.patch.is_file() and self.workspace.is_dir()

    def _load_sessions(self) -> dict[str, str]:
        try:
            value = json.loads(self.sessions_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {key: item for key, item in value.items() if isinstance(item, str)} if isinstance(value, dict) else {}

    def _save_sessions(self) -> None:
        self.sessions_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.sessions_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._sessions, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.sessions_file)

    def _command(self, question: str, session_id: str | None) -> list[str]:
        base = [self.node, str(self.cli), "--profile", "headless", "--patch", str(self.patch), "--json"]
        if session_id:
            base += ["--session-id", session_id]
        if hasattr(self, "evidence_path") and PLANNED_COST_CONTEXT not in question:
            question = f"ForgeFlow evidence field meaning: {PLANNED_COST_CONTEXT}\n\nQuestion: {question}"
        base.extend(["--", question])
        return base

    def _env(self) -> dict[str, str]:
        from .dsh_environment import assistant_environment

        return assistant_environment(os.environ, home=self.home,
                                     evidence=getattr(self, "evidence_path", ""))

    def _parse(self, stdout: str) -> DshReply:
        final = ""
        session_id = None
        activity: list[dict[str, Any]] = []
        tools: dict[str, dict[str, Any]] = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "session" and isinstance(event.get("sessionId"), str):
                session_id = event["sessionId"]
            elif kind == "tool_call":
                call_id = str(event.get("callId", ""))
                tool = _short(event.get("tool"), 40) or "tool"
                step = _tool_step(tool, event.get("input"))
                tools[call_id] = step
                activity.append(step)
            elif kind == "tool_result":
                call_id = str(event.get("callId", ""))
                step = tools.get(call_id)
                if step is not None:
                    _finish_tool_step(step, event)
            elif kind in ("text", "final") and isinstance(event.get("text"), str):
                final = event["text"]
        if not final:
            raise DshUnavailable("dsh_no_final")
        citations: list[str] = _citations(final, self.workspace)
        for step in activity:
            for value in (step.get("label"), step.get("detail")):
                for found in _citations(str(value or ""), self.workspace):
                    if found in citations:
                        continue
                    if ":" not in found and any(
                        item.startswith(found + ":") for item in citations
                    ):
                        continue
                    citations.append(found)
        scope = "project" if citations or any(
            word in final.lower() for word in ("forgeflow", "approval", "executor", "verifier")
        ) else "general"
        return DshReply(
            answer=final,
            citations=citations[:5],
            activity=activity[:MAX_ACTIVITY],
            session_id=session_id,
            scope=scope,
        )

    def _invoke(self, question: str, session_id: str | None):
        try:
            return subprocess.run(
                self._command(question, session_id),
                cwd=self.workspace, env=self._env(),
                capture_output=True, text=True, timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DshUnavailable("dsh_unavailable") from None


    def stream(self, question: str, conversation_id: str | None = None, *, cancel_event=None):
        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise DshUnavailable("turn_cancelled")

        check_cancelled()
        try:
            with turn_budget(getattr(self, "evidence_path", None), timeout=self.timeout,
                             check_cancelled=check_cancelled):
                check_cancelled()
                yield from self._stream_impl(question, conversation_id, check_cancelled=check_cancelled)
        except TimeoutError:
            raise DshUnavailable("turn_queue_timeout") from None
        except (OSError, ValueError):
            raise DshUnavailable("evidence_budget_unavailable") from None

    def _stream_impl(self, question: str, conversation_id: str | None = None, *, check_cancelled=lambda: None):
        """Yield live trace snapshots and a final envelope from DSH.

        The DSH JSON events are written incrementally, so the browser sees
        thinking and tool events while the turn is still running. No raw
        stderr or provider payload crosses this boundary.
        """
        if not self.configured():
            raise DshUnavailable("dsh_not_configured")
        key = conversation_id or ""
        session_id = self._sessions.get(key) if key else None
        process = None
        activity: list[dict[str, Any]] = []
        tools: dict[str, dict[str, Any]] = {}
        final = ""
        reply_session = None
        output_lines = None
        try:
            command = self._command(question, session_id)
            environment = self._env()
            check_cancelled()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.workspace,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except OSError:
                raise DshUnavailable("dsh_unavailable") from None
            assert process.stdout is not None
            deadline = time.monotonic() + self.timeout

            def check_running():
                check_cancelled()
                if time.monotonic() >= deadline:
                    raise DshUnavailable("dsh_timeout")

            def lines():
                # Poll in the consuming worker itself: blocking readline cannot
                # notice cancellation while a CLI process produces no output.
                # No cancellation watcher or permanent waiting thread is needed.
                pending = b""
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        check_running()
                        if not selector.select(min(.1, max(0, deadline - time.monotonic()))):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            if pending:
                                check_running()
                                yield pending.decode("utf-8", errors="replace")
                            return
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            check_running()
                            yield line.decode("utf-8", errors="replace")

            output_lines = lines()
            for line in output_lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "session" and isinstance(event.get("sessionId"), str):
                    reply_session = event["sessionId"]
                elif kind == "tool_call":
                    call_id = str(event.get("callId", ""))
                    tool = _short(event.get("tool"), 40) or "tool"
                    step = _tool_step(tool, event.get("input"))
                    tools[call_id] = step
                    activity.append(step)
                    yield {"type": "trace", "activity": activity[:MAX_ACTIVITY]}
                elif kind == "tool_result":
                    call_id = str(event.get("callId", ""))
                    step = tools.get(call_id)
                    if step is not None:
                        _finish_tool_step(step, event)
                        yield {
                            "type": "trace",
                            "activity": activity[:MAX_ACTIVITY],
                        }
                elif kind in ("text", "final") and isinstance(event.get("text"), str):
                    final = event["text"]
            # A process may close stdout and still be running. Keep checking
            # cancellation and the same deadline while waiting for its exit.
            while True:
                check_running()
                try:
                    code = process.wait(timeout=min(.1, max(0, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
            check_cancelled()
            if code != 0:
                raise DshUnavailable("dsh_failed")
            if not final:
                raise DshUnavailable("dsh_no_final")
            citations: list[str] = _citations(final, self.workspace)
            for step in activity:
                for value in (step.get("label"), step.get("detail")):
                    for found in _citations(str(value or ""), self.workspace):
                        if found not in citations:
                            citations.append(found)
            scope = "project" if citations or any(
                word in final.lower()
                for word in ("forgeflow", "approval", "executor", "verifier")
            ) else "general"
            if key and reply_session:
                self._sessions[key] = reply_session
                self._save_sessions()
            check_cancelled()
            yield {
                "type": "final",
                "found": True,
                "mode": "harness",
                "answer": final,
                "citations": citations[:5],
                "passages": [],
                "activity": activity[:MAX_ACTIVITY],
                "scope": scope,
                "session_id": reply_session,
            }
        finally:
            if output_lines is not None:
                output_lines.close()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process is not None and process.stdout is not None:
                process.stdout.close()

    def ask(self, question: str, conversation_id: str | None = None) -> DshReply:
        try:
            with turn_budget(getattr(self, "evidence_path", None), timeout=self.timeout):
                return self._ask_impl(question, conversation_id)
        except TimeoutError:
            raise DshUnavailable("turn_queue_timeout") from None
        except (OSError, ValueError):
            raise DshUnavailable("evidence_budget_unavailable") from None

    def _ask_impl(self, question: str, conversation_id: str | None = None) -> DshReply:
        if not self.configured():
            raise DshUnavailable("dsh_not_configured")
        key = conversation_id or ""
        session_id = self._sessions.get(key) if key else None
        process = self._invoke(question, session_id)
        if process.returncode != 0 and session_id:
            # A stale mapping must not permanently break a conversation.
            process = self._invoke(question, None)
        if process.returncode != 0:
            raise DshUnavailable("dsh_failed")
        reply = self._parse(process.stdout)
        if key and reply.session_id:
            self._sessions[key] = reply.session_id
            self._save_sessions()
        return reply
