#!/usr/bin/env python3
"""Minimal read-only MCP server for ForgeFlow case/run evidence.

The DSH sidecar mounts this over stdio. It intentionally exposes no write,
approval, or executor method. Outputs are bounded and redact private owner/run
location fields.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE", "")
MAX_TEXT = 12000
SECRET_KEYS = {"uid", "approver", "trial_uri", "workspace_root", "trials_dir"}

TOOLS = [
    {
        "name": "list_cases",
        "description": "List ForgeFlow product cases with state, product, disruption and plan summary.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "case_brief",
        "description": "Read a bounded, public fact sheet for one ForgeFlow case: state, order, plan, checks, approval and execution evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_case",
        "description": "Read one ForgeFlow case, including plan, checks, approval and execution evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_runs",
        "description": "List recorded evaluation runs with task, state and verifier summary.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_run",
        "description": "Read one recorded run's manifest and verifier reward.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_evidence",
        "description": "Search case and run evidence text for a term.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


def _safe(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe(item, depth + 1)
            for key, item in value.items()
            if key not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [_safe(item, depth + 1) for item in value]
    if isinstance(value, str):
        return value[:MAX_TEXT]
    return value


def _json_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _snapshot() -> dict[str, Any]:
    # This path is injected by the authenticated host, never a tool argument.
    if not EVIDENCE:
        raise ValueError("evidence_scope_unavailable")
    path = Path(EVIDENCE)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("evidence_scope_unavailable")
    value = _json_file(path)
    if not value or value.get("schema_version") != 1 or not value.get("scope_id"):
        raise ValueError("evidence_scope_unavailable")
    return value


def _call(name: str, arguments: dict[str, Any]) -> Any:
    spec = next((tool for tool in TOOLS if tool["name"] == name), None)
    if spec is None:
        raise ValueError("unknown_tool")
    schema = spec["inputSchema"]
    if set(arguments) - set(schema["properties"]) or any(
        key not in arguments for key in schema.get("required", [])
    ) or any(not isinstance(value, str) for value in arguments.values()):
        raise ValueError("invalid_tool_arguments")
    snapshot = _snapshot()
    cases = snapshot.get("cases", [])
    runs = snapshot.get("runs", [])
    if name == "list_cases":
        return _safe(cases)
    if name == "list_runs":
        return _safe(runs)
    if name in ("get_case", "case_brief"):
        case = next((item for item in cases if item.get("case_id") == arguments["case_id"]), None)
        if case is None:
            raise ValueError("case_not_found")
        return _safe(case)
    if name == "get_run":
        run = next((item for item in snapshot.get("run_details", runs) if item.get("run_id") == arguments["run_id"]), None)
        if run is None:
            raise ValueError("run_not_found")
        return _safe(run)
    needle = arguments["query"].strip().lower()
    if not needle:
        return []
    hits = []
    for kind, items, id_key in (("case", cases, "case_id"), ("run", runs, "run_id")):
        for item in items:
            text = json.dumps(_safe(item), ensure_ascii=False)
            if needle in text.lower():
                hits.append({"kind": kind, "id": item.get(id_key), "excerpt": text[:500]})
    return hits[:20]


def _response(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "forgeflow-evidence", "version": "0.1"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        try:
            value = _call(str(params.get("name", "")), params.get("arguments") if isinstance(params.get("arguments"), dict) else {})
        except (ValueError, OSError) as error:
            return {
                "jsonrpc": "2.0", "id": request_id,
                "result": {"content": [{"type": "text", "text": str(error) if isinstance(error, ValueError) else "evidence_scope_unavailable"}], "isError": True},
            }
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False},
        }
    return {
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": -32601, "message": "method_not_found"},
    }


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = _response(request) if isinstance(request, dict) else None
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse_error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
