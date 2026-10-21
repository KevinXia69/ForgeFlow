from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, requests: list[dict]) -> list[dict]:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({
        "schema_version": 1, "scope_id": "host-scoped",
        "cases": [{"case_id": "case-1", "state": "awaiting_approval",
                   "order": {"product": "Widget", "units": 2}}], "runs": [],
    }), encoding="utf-8")
    env = {
        "FORGEFLOW_ASSISTANT_EVIDENCE": str(evidence),
        "PYTHONPATH": str(ROOT / "src"),
    }
    payload = "\n".join(json.dumps(item) for item in requests) + "\n"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/assistant_mcp.py")],
        input=payload, text=True, capture_output=True, env=env, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_mcp_lists_cases_without_owner_uid(tmp_path: Path):
    responses = _run(tmp_path, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "list_cases", "arguments": {},
        }},
    ])
    tools = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert tools == {"list_cases", "case_brief", "get_case", "list_runs", "get_run", "search_evidence"}
    payload = json.loads(responses[2]["result"]["content"][0]["text"])
    assert payload[0]["case_id"] == "case-1"
    assert "uid" not in payload[0]
    assert "private" not in responses[2]["result"]["content"][0]["text"]


def test_mcp_exposes_no_write_tool(tmp_path: Path):
    responses = _run(tmp_path, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])
    names = {tool["name"] for tool in responses[0]["result"]["tools"]}
    assert not names & {"approve", "apply", "write", "execute", "approve_case"}


def test_case_brief_is_bounded_and_omits_owner(tmp_path: Path):
    responses = _run(tmp_path, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "case_brief", "arguments": {"case_id": "case-1"},
        }},
    ])
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["case_id"] == "case-1"
    assert payload["state"] == "awaiting_approval"
    assert payload["order"]["product"] == "Widget"
    assert "uid" not in payload
    assert "private" not in responses[0]["result"]["content"][0]["text"]
