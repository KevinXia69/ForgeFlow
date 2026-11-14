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
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.knowledge.citation_metadata import source_metadata

from .case_qa import PLANNED_COST_CONTEXT
from .model_settings import assistant_model

MAX_ACTIVITY = 40
MAX_LABEL = 1600
MAX_RESULT_SUMMARY = 200
MAX_RESULT_PARSE = 64_000
MAX_KNOWLEDGE_PASSAGES = 24
MAX_SOURCE_CITATIONS = 32
MCP_PREFIX = "mcp__forgeflow__"
MCP_TOOL_LABELS = {
    "workspace_summary": "workspace summary",
    "case_brief": "case",
    "get_case": "case",
    "list_cases": "case",
    "get_run": "run",
    "list_runs": "run",
    "search_evidence": "evidence search",
}
TOOL_LABELS = {"web_search": "web search", "web_fetch": "web fetch", "read_image": "read image"}
_OK_RESULT_STATUS = {"ok", "success", "completed", "done", "available", "passed"}
CITATION = re.compile(
    r"(?P<path>[A-Za-z0-9_./-]+\.(?:py|tsx|ts|md|json))(?::(?P<line>\d+))?"
)
WEB_URL = re.compile(r"https?://[^\s<>()\[\]{}]+")


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
    passages: list[dict[str, Any]] = field(default_factory=list)


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
    if tool == "read_image":
        return "attached image"
    if tool in TOOL_LABELS:
        return detail
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
    for key in ("count", "returned", "matched", "total_cases"):
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
        # DSH may coalesce the evidence and its model-only field guide into one block.
        return value.removesuffix("\n" + PLANNED_COST_CONTEXT)
    if isinstance(value, list):
        parts = [
            _result_text(item["text"]) for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
            and item.get("text") != PLANNED_COST_CONTEXT
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
        f"{MCP_PREFIX}list_runs", f"{MCP_PREFIX}workspace_summary",
    } and isinstance(payload, (dict, list)):
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) <= 12_000:
            return {"payload": payload}
    return {}


def _knowledge_passages(tool: str, payload: object) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect bounded reviewed-guide passages returned by project_knowledge."""
    if not isinstance(payload, dict) or not str(tool).endswith("project_knowledge"):
        return [], []
    passages: list[dict[str, Any]] = []
    rows = payload.get("passages", [])
    for item in (rows[:MAX_KNOWLEDGE_PASSAGES] if isinstance(rows, list) else []):
        if not isinstance(item, dict):
            continue
        path, start, end, text = (item.get("path"), item.get("start_line"),
                                  item.get("end_line"), item.get("text"))
        if not (isinstance(path, str) and type(start) is int and type(end) is int
                and isinstance(text, str)):
            continue
        passage = {"path": path, "start_line": start, "end_line": end, "text": text[:2000]}
        version = item.get("document_version")
        if type(version) is int and version > 0:
            passage["document_version"] = version
        for metadata_key in ("locator", "document_id"):
            value = item.get(metadata_key)
            if isinstance(value, str) and value:
                passage[metadata_key] = value[:512]
        metadata = source_metadata(item.get("source_metadata"))
        if metadata:
            passage["source_metadata"] = metadata
        passages.append(passage)
    raw_citations = payload.get("citations", [])
    citations = [item for item in raw_citations if isinstance(item, str)][:MAX_SOURCE_CITATIONS] \
        if isinstance(raw_citations, list) else []
    return passages, citations


def _answer_sources(final: str, activity: list[dict], workspace: Path,
                    passages: list[dict], tool_citations: list[str]) -> tuple[list[dict], list[str]]:
    """Retain source diversity across tool calls, without claiming sentence attribution.

    Repeated chunks from the first search must not evict a later specification
    or conflicting source. Explicitly linked sources get first priority, then
    one passage per document/version, then additional passages within the bound.
    """
    unique = []
    keys = set()
    for passage in passages:
        key = (passage["path"], passage.get("document_version"), passage.get("locator"),
               passage["start_line"], passage["end_line"])
        if key not in keys:
            keys.add(key)
            unique.append(passage)

    def explicitly_linked(passage):
        url = passage.get("source_metadata", {}).get("original_url")
        filename = passage["path"].rsplit("/", 1)[-1]
        title = passage.get("source_metadata", {}).get("title", "")
        named = bool(re.search(r"(?<![\w.-])" + re.escape(filename) + r"(?![\w-])", final))
        named = named or bool(len(title) >= 8 and title in final)
        return passage["path"] in final or bool(url and url in final) or named

    ordered = sorted(unique, key=lambda item: not explicitly_linked(item))
    selected, remainder, documents = [], [], set()
    for passage in ordered:
        document = (passage["path"], passage.get("document_version"))
        if document in documents:
            remainder.append(passage)
        else:
            selected.append(passage)
            documents.add(document)
    selected = (selected + remainder)[:MAX_KNOWLEDGE_PASSAGES]
    citations = [f"{item['path']}#{item['locator']}" if item.get("locator") else
                 f"{item['path']}:{item['start_line']}-{item['end_line']}" for item in selected]
    citations += _citations(final, workspace) + _web_citations(final)
    # Managed references must keep their captured passage/version. Other tool
    # references remain available, but are labelled as consulted material in UI.
    citations += [item for item in tool_citations if not item.startswith("document://")]
    for step in activity:
        for value in (step.get("label"), step.get("detail")):
            for found in _citations(str(value or ""), workspace):
                if found in citations or ":" not in found and any(
                        item.startswith(found + ":") for item in citations):
                    continue
                citations.append(found)
    return selected, list(dict.fromkeys(citations))[:MAX_SOURCE_CITATIONS]


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


def _web_citations(value: str) -> list[str]:
    found: list[str] = []
    for match in WEB_URL.finditer(value):
        url = match.group(0).rstrip(".,;:!?)")
        if url not in found:
            found.append(url)
    return found


class DshHarness:
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

        close_model_harness(self)

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
        base.extend(["--", question])
        return base

    def _env(self) -> dict[str, str]:
        from .dsh_environment import assistant_environment

        if getattr(self, "_model_closed", False):
            raise DshUnavailable("dsh_model_binding_closed")
        return assistant_environment(os.environ, home=self.home,
                                     evidence=getattr(self, "evidence_path", ""),
                                     model_api_key=getattr(self, "_model_api_key", None))

    def _parse(self, stdout: str) -> DshReply:
        final = ""
        session_id = None
        activity: list[dict[str, Any]] = []
        tools: dict[str, dict[str, Any]] = {}
        passages: list[dict[str, Any]] = []
        tool_citations: list[str] = []
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
                    if step["status"] == "done":
                        payload = _result_payload(_result_text(event.get("result")))
                        new_passages, new_citations = _knowledge_passages(str(step["kind"]), payload)
                        passages.extend(new_passages)
                        tool_citations.extend(new_citations)
            elif kind in ("text", "final") and isinstance(event.get("text"), str):
                final = event["text"]
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
            activity=activity[:MAX_ACTIVITY],
            session_id=session_id,
            scope=scope,
            passages=passages,
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


    def stream(self, question: str, conversation_id: str | None = None):
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
        passages: list[dict[str, Any]] = []
        tool_citations: list[str] = []
        final = ""
        reply_session = None
        try:
            command = self._command(question, session_id)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.workspace,
                    env=self._env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except OSError:
                raise DshUnavailable("dsh_unavailable") from None
            assert process.stdout is not None
            watchdog = threading.Timer(self.timeout, process.kill)
            watchdog.daemon = True
            watchdog.start()
            for line in process.stdout:
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
                        payload = _result_payload(_result_text(event.get("result")))
                        if step["status"] == "done":
                            new_passages, new_citations = _knowledge_passages(
                                str(step.get("kind", "")), payload)
                            passages.extend(new_passages)
                            tool_citations.extend(new_citations)
                        yield {
                            "type": "trace",
                            "activity": activity[:MAX_ACTIVITY],
                        }
                elif kind in ("text", "final") and isinstance(event.get("text"), str):
                    final = event["text"]
            watchdog.cancel()
            code = process.wait(timeout=5)
            if code != 0:
                raise DshUnavailable("dsh_failed")
            if not final:
                raise DshUnavailable("dsh_no_final")
            passages, citations = _answer_sources(final, activity, self.knowledge_root,
                                                 passages, tool_citations)
            scope = "project" if citations or any(
                word in final.lower()
                for word in ("forgeflow", "approval", "executor", "verifier")
            ) else "general"
            if key and reply_session:
                self._sessions[key] = reply_session
                self._save_sessions()
            yield {
                "type": "final",
                "model": self.model, "provider": self.provider,
                "found": True,
                "mode": "harness",
                "answer": final,
                "citations": citations,
                "passages": passages,
                "activity": activity[:MAX_ACTIVITY],
                "scope": scope,
                "session_id": reply_session,
            }
        finally:
            if "watchdog" in locals():
                watchdog.cancel()
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

    def ask(self, question: str, conversation_id: str | None = None) -> DshReply:
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
