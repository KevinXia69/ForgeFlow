#!/usr/bin/env python3
"""Minimal read-only MCP server for ForgeFlow case/run evidence.

The DSH sidecar mounts this over stdio. It intentionally exposes no write,
approval, or executor method. Outputs are bounded and redact private owner/run
location fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE", "")
MAX_TEXT = 12000
MAX_RESULT_BYTES = 32 * 1024
MAX_PAGE_ITEMS = 50
SECRET_KEYS = {"uid", "approver", "trial_uri", "workspace_root", "trials_dir",
               "api_key", "token", "secret", "invite_code", "session_secret", "nonce",
               "executor_binding", "executor_grant", "owner_sha256", "_owner_sha256"}
SECTIONS = ("actions", "preserved_documents", "costs", "execution", "readback", "verification")
PAGE_PROPERTIES = {"offset": {"type": "integer", "minimum": 0},
                   "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_ITEMS}}

TOOLS = [
    {
        "name": "list_cases",
        "description": "List ForgeFlow product cases with state, product, disruption and plan summary.",
        "inputSchema": {"type": "object", "properties": PAGE_PROPERTIES, "additionalProperties": False},
    },
    {
        "name": "case_brief",
        "description": "Read a small public summary of case state, order and plan. Quantities and document details may be omitted; use get_case sections to answer questions about those details.",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_case",
        "description": "Read the fixed case snapshot. Choose a section to page through action quantities, preserved documents, costs, execution, readback or verification. Large unsectioned results explicitly omit detail; missing summary fields are not proof that records are absent.",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"},
                           "section": {"type": "string", "enum": list(SECTIONS)},
                           **PAGE_PROPERTIES},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_runs",
        "description": "List recorded evaluation runs with task, state and verifier summary.",
        "inputSchema": {"type": "object", "properties": PAGE_PROPERTIES, "additionalProperties": False},
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
            if key.lower() not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [_safe(item, depth + 1) for item in value]
    if isinstance(value, str):
        return value
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
    if "content_revision" in value:
        revision = value["content_revision"]
        body = {key: item for key, item in value.items() if key != "content_revision"}
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(encoded).hexdigest() != revision:
            raise ValueError("evidence_content_changed")
        guard_name = f"{value['scope_id']}.scope.json"
        if value.get("scope_guard") != guard_name or Path(guard_name).name != guard_name:
            raise ValueError("evidence_scope_unavailable")
        guard = path.parent / guard_name
        current = None if guard.is_symlink() else _json_file(guard)
        if not current or current.get("scope_id") != value["scope_id"] or current.get("scope_key") != value.get("scope_key"):
            raise ValueError("evidence_scope_changed")
    return value


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _page(items: Any, arguments: dict[str, Any], *, field: str, metadata=None) -> dict[str, Any]:
    base = {**(metadata or {}), "field": field, "items": [], "offset": arguments.get("offset", 0)}
    if not isinstance(items, list):
        return {**base, "coverage": "not_recorded", "total": None, "complete": False, "next_offset": None}
    offset, limit = arguments.get("offset", 0), arguments.get("limit", MAX_PAGE_ITEMS)
    result = {**base, "coverage": "available", "total": len(items), "complete": False, "next_offset": None}
    for index, item in enumerate(items[offset:offset + limit], start=offset):
        projected = _safe(item)
        candidate = {**result, "items": [*result["items"], {"field": f"{field}.{index}", "value": projected}]}
        if _size(candidate) > MAX_RESULT_BYTES - 1024:
            break
        result = candidate
    next_offset = offset + len(result["items"])
    result["complete"] = next_offset >= len(items)
    result["next_offset"] = None if result["complete"] else next_offset
    if not result["items"] and offset < len(items):
        result.update(coverage="item_too_large", next_offset=None)
    return result


def _brief(case: dict[str, Any]) -> dict[str, Any]:
    result = {key: _safe(case[key]) for key in ("case_id", "state", "reason", "updated_at", "order", "disruption", "revision", "result") if key in case}
    plan = case.get("plan")
    if isinstance(plan, dict):
        result["plan"] = {key: _safe(plan[key]) for key in ("plan_id", "objective", "new_spend", "cost_delta", "margin", "on_time") if key in plan}
        result["plan"]["action_count"] = len(plan["actions"]) if isinstance(plan.get("actions"), list) else None
    result["omitted_sections"] = list(SECTIONS)
    result["detail_tool"] = "get_case"
    if _size(result) > 6000:
        result = {key: value for key, value in result.items() if key in ("case_id", "state", "updated_at", "plan", "omitted_sections", "detail_tool")}
    if _size(result) > 6000:
        result = {key: value for key, value in result.items() if key != "plan"}
    if _size(result) > 6000:
        raise ValueError("evidence_summary_too_large")
    return result


def _case_section(case: dict[str, Any], section: str, arguments: dict[str, Any], snapshot) -> dict[str, Any]:
    plan = case.get("plan") if isinstance(case.get("plan"), dict) else {}
    costs = plan.get("costs") if isinstance(plan.get("costs"), dict) else {}
    execution = case.get("execution") if isinstance(case.get("execution"), dict) else {}
    readback = execution.get("readback") if isinstance(execution.get("readback"), dict) else {}
    verifier = execution.get("verifier") if isinstance(execution.get("verifier"), dict) else {}
    revision = case.get("revision") if isinstance(case.get("revision"), dict) else {}
    metadata = {"case_id": case.get("case_id"), "plan_id": plan.get("plan_id"),
                "version": revision.get("version"),
                "content_revision": snapshot.get("content_revision"), "section": section}
    if section == "actions":
        return _page(plan.get("actions"), arguments, field="plan.actions", metadata=metadata)
    if section == "preserved_documents":
        # Page original indices, then mark kept rows without inventing compact indices.
        rows = costs.get("lines")
        selected = [{**row, "source_field": f"plan.costs.lines.{i}"} for i, row in enumerate(rows or [])
                    if isinstance(row, dict) and row.get("action") == "keep"] if isinstance(rows, list) else None
        return _page(selected, arguments, field="preserved_documents", metadata=metadata)
    if section == "costs":
        metadata["summary"] = _safe({key: value for key, value in costs.items() if key not in ("lines", "documents")})
        return _page(costs.get("lines"), arguments, field="plan.costs.lines", metadata=metadata)
    if section == "execution":
        metadata["status"] = execution.get("status")
        return _page(execution.get("writes"), arguments, field="execution.writes", metadata=metadata)
    if section == "readback":
        metadata["summary"] = _safe({key: value for key, value in readback.items() if key != "records"})
        return _page(readback.get("records"), arguments, field="execution.readback.records", metadata=metadata)
    metadata["summary"] = _safe({key: value for key, value in verifier.items() if key != "checks"})
    return _page(verifier.get("checks"), arguments, field="execution.verifier.checks", metadata=metadata)


def _call(name: str, arguments: dict[str, Any]) -> Any:
    spec = next((tool for tool in TOOLS if tool["name"] == name), None)
    if spec is None:
        raise ValueError("unknown_tool")
    schema = spec["inputSchema"]
    if set(arguments) - set(schema["properties"]) or any(
        key not in arguments for key in schema.get("required", [])
    ):
        raise ValueError("invalid_tool_arguments")
    for key, value in arguments.items():
        prop = schema["properties"][key]
        if prop["type"] == "string" and (not isinstance(value, str) or len(value) > 500):
            raise ValueError("invalid_tool_arguments")
        if prop["type"] == "integer" and (type(value) is not int or value < prop.get("minimum", 0) or value > prop.get("maximum", 1000000)):
            raise ValueError("invalid_tool_arguments")
        if "enum" in prop and value not in prop["enum"]:
            raise ValueError("invalid_tool_arguments")
    if name == "get_case" and any(key in arguments for key in ("offset", "limit")) and "section" not in arguments:
        raise ValueError("section_required_for_pagination")
    snapshot = _snapshot()
    cases = snapshot.get("cases", [])
    runs = snapshot.get("runs", [])
    if name == "list_cases":
        return _page(cases, arguments, field="cases") if arguments or _size(cases) > MAX_RESULT_BYTES else _safe(cases)
    if name == "list_runs":
        return _page(runs, arguments, field="runs") if arguments or _size(runs) > MAX_RESULT_BYTES else _safe(runs)
    if name in ("get_case", "case_brief"):
        case = next((item for item in cases if item.get("case_id") == arguments["case_id"]), None)
        if case is None:
            raise ValueError("case_not_found")
        if name == "case_brief":
            return _brief(case)
        if "section" in arguments:
            return _case_section(case, arguments["section"], arguments, snapshot)
        if _size(case) > MAX_RESULT_BYTES:
            return {**_brief(case), "coverage": "omitted", "complete": False,
                    "content_revision": snapshot.get("content_revision")}
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


def _budget(*, call: bool = False, size: int = 0, nonce: str | None = None) -> str | None:
    """Charge an existing host lease; the model cannot create or reset it."""
    import fcntl

    snapshot = _snapshot()
    if "content_revision" not in snapshot:
        return None  # Compatibility for standalone, host-scoped legacy fixtures.
    path = Path(EVIDENCE).with_suffix(".budget.json")
    if path.is_symlink() or not path.is_file():
        raise ValueError("evidence_turn_unavailable")
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            value = json.load(handle)
            if not value.get("active") or value["expires_at"] <= time.time() or nonce is not None and value["nonce"] != nonce:
                raise ValueError("evidence_turn_unavailable")
            calls, total = value["calls"] + int(call), value["bytes"] + size
            if calls > min(value["max_calls"], 12) or total > min(value["max_bytes"], 128 * 1024):
                raise ValueError("evidence_budget_exhausted")
            value.update(calls=calls, bytes=total)
            handle.seek(0)
            json.dump(value, handle, separators=(",", ":"))
            handle.truncate()
            handle.flush()
            return value["nonce"]
        except (KeyError, TypeError):
            raise ValueError("evidence_turn_unavailable") from None


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
            nonce = _budget(call=True)
            value = _call(str(params.get("name", "")), params.get("arguments") if isinstance(params.get("arguments"), dict) else {})
            if _size(value) > MAX_RESULT_BYTES:
                raise ValueError("evidence_result_too_large_use_section_pagination")
            _budget(size=_size(value), nonce=nonce)
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
