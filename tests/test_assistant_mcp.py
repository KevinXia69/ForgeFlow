from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, requests: list[dict], snapshot: dict | None = None) -> list[dict]:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(snapshot if snapshot is not None else {
        "schema_version": 1, "scope_id": "host-scoped",
        "cases": [{"case_id": "case-1", "state": "awaiting_approval",
                   "order": {"product": "Widget", "units": 2}}], "runs": [],
    }), encoding="utf-8")
    env = {
        "FORGEFLOW_ASSISTANT_EVIDENCE": str(evidence),
        "PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'tests'}",
        "FORGEFLOW_TEST_KNOWLEDGE_ADAPTER": "1",
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
    assert tools == {"workspace_summary", "list_cases", "case_brief", "get_case", "list_runs", "get_run", "search_evidence", "project_knowledge"}
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


def test_project_knowledge_returns_cited_documents_without_case_cost_guidance(tmp_path: Path):
    from forgeflow.knowledge import evidence_lines

    response, = _run(tmp_path, [{
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "project_knowledge",
            "arguments": {"query": "技术栈 前端 后端 数据库", "locale": "zh", "limit": 3},
        },
    }])
    assert not response["result"]["isError"]
    block, = response["result"]["content"]
    result = json.loads(block["text"])
    assert result["found"]
    assert 1 <= len(result["passages"]) <= 3
    assert len(block["text"]) < 12000
    assert "case-1" not in block["text"]
    for passage, citation in zip(result["passages"], result["citations"], strict=True):
        assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]
        assert not passage["path"].startswith("docs/en/")


def test_project_knowledge_infers_english_and_preserves_no_hit(tmp_path: Path):
    responses = _run(tmp_path, [{
        "jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
            "name": "project_knowledge", "arguments": {"query": query},
        },
    } for index, query in enumerate((
        "technical stack frontend backend database",
        "Xylophone marmalade sourdough recipe?",
    ), 1)])
    found, missing = [json.loads(r["result"]["content"][0]["text"]) for r in responses]
    assert found["found"]
    assert found["locale"] == "en"
    assert all(p["path"].startswith("docs/en/") for p in found["passages"])
    assert not missing["found"]
    assert missing["passages"] == []


def test_project_knowledge_rejects_unbounded_or_unscoped_requests(tmp_path: Path):
    invalid = [
        {}, {"query": ""}, {"query": "a" * 501}, {"query": 12},
        {"query": "DSH", "locale": "fr"}, {"query": "DSH", "limit": 0},
        {"query": "DSH", "limit": 6}, {"query": "DSH", "limit": True},
        {"query": "DSH", "root": "/tmp"},
        {"query": "DSH", "path": ".env"},
        {"query": "DSH", "source_paths": ["docs/en/deployment.md"]},
        {"query": "market", "topic": "all"},
        {"query": "market", "topic": "../docs"},
        {"query": "market", "topic": ["product"]},
    ]
    responses = _run(tmp_path, [{
        "jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
            "name": "project_knowledge", "arguments": arguments,
        },
    } for index, arguments in enumerate(invalid)])
    assert all(row["result"]["isError"] for row in responses)
    response, = _run(tmp_path, [{
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "project_knowledge", "arguments": {"query": "DSH"},
        },
    }], snapshot={})
    assert response["result"]["isError"]
    response, = _run(tmp_path, [{
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "project_knowledge", "arguments": {"query": "market", "topic": "product"},
        },
    }], snapshot={})
    assert response["result"]["isError"]


def test_product_topic_returns_only_reviewed_product_sources(tmp_path: Path):
    from forgeflow.knowledge import evidence_lines

    responses = _run(tmp_path, [{
        "jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
            "name": "project_knowledge", "arguments": {
                "query": query, "topic": "product", "limit": 5,
            },
        },
    } for index, query in enumerate((
        "市场需求 行业信号 Deloitte McKinsey", "market demand industry evidence",
    ), 1)])
    for response, guide in zip(responses, (
        "docs/PRODUCT_GUIDE.md", "docs/en/product-guide.md",
    ), strict=True):
        assert not response["result"]["isError"]
        block, = response["result"]["content"]
        result = json.loads(block["text"])
        assert result["found"]
        assert {p["path"] for p in result["passages"]} == {guide}
        assert "case-1" not in block["text"]
        assert len(block["text"]) < 12000
        for passage, citation in zip(result["passages"], result["citations"], strict=True):
            assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]


def test_project_topic_schema_is_fixed_and_keeps_technical_default(tmp_path: Path):
    response, = _run(tmp_path, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    tool = next(t for t in response["result"]["tools"] if t["name"] == "project_knowledge")
    schema = tool["inputSchema"]
    assert schema["properties"]["topic"] == {
        "type": "string", "enum": ["technical", "product", "market"], "default": "technical",
    }
    assert set(schema["properties"]) == {"query", "topic", "locale", "limit"}
    assert schema["additionalProperties"] is False


def test_knowledge_tool_advertises_upload_search_and_evidence_limits(tmp_path: Path):
    response, = _run(tmp_path, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    tool = next(t for t in response["result"]["tools"] if t["name"] == "project_knowledge")
    description = tool["description"]
    assert "uploaded enterprise documents" in description
    assert "current tenant and document ACL" in description
    assert "cite the source" in description
    assert "do not guess" in description
    assert "only from fixed current guides" not in description


def test_hosted_knowledge_tool_returns_uploaded_source_and_preserves_no_hit(tmp_path, monkeypatch):
    import httpx

    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"schema_version": 1, "scope_id": "test-scope"}))
    evidence.with_suffix(".knowledge.json").write_text(json.dumps({
        "url": "http://scoped-gateway", "token": "test-capability",
    }))
    spec = importlib.util.spec_from_file_location("uploaded_knowledge_mcp", ROOT / "scripts/assistant_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.EVIDENCE = str(evidence)
    source = "document://synthetic-fixture.docx"
    query = "What is the verification keyword in synthetic-fixture.docx?"
    passage = {"path": source, "start_line": 1, "end_line": 1, "text": "The keyword is LANTERN-123."}
    calls = []

    def search(url, *, headers, json, timeout, trust_env):
        assert url == "http://scoped-gateway/api/internal/knowledge/search"
        assert headers == {"X-ForgeFlow-Capability": "test-capability"}
        assert set(json) == {"question", "limit", "lang"}
        assert json["limit"] == 2 and json["lang"] == "en"
        assert timeout == 90 and trust_env is False
        calls.append(json["question"])
        found = json["question"] == query
        return httpx.Response(200, json={
            "found": found, "passages": [passage] if found else [],
            "citations": [source + ":1"] if found else [],
        })

    monkeypatch.setattr(module.httpx, "post", search)
    result = module._call("project_knowledge", {"query": query, "topic": "technical", "limit": 2})
    assert result == {"found": True, "passages": [passage], "citations": [source + ":1"]}
    missing = module._call("project_knowledge", {"query": "missing fixture", "limit": 2})
    assert missing == {"found": False, "passages": [], "citations": []}
    assert calls == [query, "missing fixture"]


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid_json", "no_hit"])
def test_hosted_knowledge_errors_reach_activity_without_becoming_no_hits(monkeypatch, failure):
    import httpx

    from forgeflow.api.dsh_sdk import _events_to_view

    spec = importlib.util.spec_from_file_location("knowledge_error_mcp", ROOT / "scripts/assistant_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_snapshot", lambda: {})
    monkeypatch.setattr(module, "_knowledge_gateway", lambda: {
        "url": "http://scoped-gateway", "token": "test-capability",
    })

    def search(*_args, **_kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic slow provider")
        if failure == "http":
            return httpx.Response(503)
        if failure == "invalid_json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"found": False, "passages": [], "citations": []})

    monkeypatch.setattr(module.httpx, "post", search)
    response = module._response({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "project_knowledge", "arguments": {"query": "DigiKey price validity"},
    }})
    result = response["result"]
    assert result["isError"] is (failure != "no_hit")
    payload = json.loads(result["content"][0]["text"])
    assert payload["found"] is False
    assert ("error" in payload) is (failure != "no_hit")
    _, activity, _, passages, citations = _events_to_view([
        {"type": "tool/call", "data": {"callId": "k1",
         "name": "mcp__forgeflow__project_knowledge", "arguments": "{}"}},
        {"type": "tool/result", "data": {"message": {"toolCallId": "k1", **result}}},
    ])
    assert activity[0]["status"] == ("done" if failure == "no_hit" else "failed")
    assert passages == [] and citations == []


def test_scoped_knowledge_timeout_fits_inside_harness_turn(tmp_path):
    from forgeflow.api.dsh_harness import DshHarness
    from forgeflow.api.dsh_permissions import scoped_patch

    base = tmp_path / "base.json"
    base.write_text(json.dumps([{"insert": [{"id": "mcp-forgeflow",
        "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "forgeflow", "transport": "stdio", "toolCallTimeoutMs": 60_000}}]},
        {"id": "web", "config": {"unchanged": True}}]))
    patch = scoped_patch(base, tmp_path / "evidence.json")
    rows = json.loads(patch.read_text())
    assert rows[1] == {"id": "web", "config": {"unchanged": True}}
    mcp_timeout = rows[0]["insert"][0]["config"]["toolCallTimeoutMs"] / 1000
    harness = DshHarness(cli=tmp_path, patch=patch, home=tmp_path, workspace=tmp_path)
    spec = importlib.util.spec_from_file_location("knowledge_budget_mcp", ROOT / "scripts/assistant_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Reproduce a successful 52s backend call without delaying the suite.
    def slow_search(*_args, **kwargs):
        import httpx

        assert 52.42 < kwargs["timeout"] < mcp_timeout < harness.timeout
        return httpx.Response(200, json={"found": True, "passages": [{"path": "document://K09.md"}]})

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(module.httpx, "post", slow_search)
        assert module._gateway_search({"url": "http://gateway", "token": "test"},
                                      "DigiKey pricing", "en", 1)["found"]


def test_market_topic_is_bounded_cited_and_still_requires_host_scope(tmp_path: Path):
    from forgeflow.knowledge import evidence_lines

    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "project_knowledge",
        "arguments": {"query": "industry opportunity", "topic": "market", "limit": 1},
    }}
    response, = _run(tmp_path, [request])
    block, = response["result"]["content"]
    result = json.loads(block["text"])
    passage, = result["passages"]
    assert "Deloitte" in passage["text"] and "McKinsey" in passage["text"]
    assert passage["path"] == "docs/en/product-guide.md"
    assert "\n".join(evidence_lines(ROOT, result["citations"][0])) == passage["text"]
    response, = _run(tmp_path, [request], snapshot={})
    assert response["result"]["isError"]


def test_project_knowledge_excludes_historical_deployment_descriptions(tmp_path: Path):
    response, = _run(tmp_path, [{
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "project_knowledge",
            "arguments": {"query": "web service ERP write deployment readonly", "locale": "en"},
        },
    }])
    result = json.loads(response["result"]["content"][0]["text"])
    assert result["found"]
    assert {p["path"] for p in result["passages"]} == {"docs/en/technical-guide.md"}


def test_case_brief_is_bounded_and_omits_owner(tmp_path: Path):
    responses = _run(tmp_path, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "case_brief", "arguments": {"case_id": "case-1"},
        }},
    ])
    from forgeflow.api.case_qa import PLANNED_COST_CONTEXT

    record, guide = responses[0]["result"]["content"]
    assert guide["annotations"] == {"audience": ["assistant"]}
    assert guide["text"] == PLANNED_COST_CONTEXT
    payload = json.loads(record["text"])
    assert payload["case_id"] == "case-1"
    assert payload["state"] == "awaiting_approval"
    assert payload["order"]["product"] == "Widget"
    assert "uid" not in payload
    assert "private" not in responses[0]["result"]["content"][0]["text"]


def test_workspace_totals_are_independent_of_payload_size_and_page(tmp_path):
    # Two copies of the same plan are two distinct cases. Old snapshots lack home_views.
    result = {"strict_success": True, "verified": True, "evaluation_valid": True}
    states = ["awaiting_approval", "awaiting_approval", "awaiting_selection", "preparing",
              "executing", "verified", "closed"]
    cases = [{"case_id": f"case-{i}", "state": state, "scenario": "A",
              "decision": {"can_approve": False}, "plan": {"notes": "long plan " * 15000}}
             for i, state in enumerate(states)]
    cases[5]["result"] = result
    cases[6]["result"] = {**result, "evaluation_valid": 1}
    snapshot = {"schema_version": 1, "scope_id": "owner", "cases": cases,
                "runs": [{"run_id": "passed", "strict_success": True, "evaluation_valid": True},
                         {"run_id": "also-passed", "strict_success": True, "evaluation_valid": True},
                         {"run_id": "invalid", "strict_success": True, "evaluation_valid": False},
                         {"run_id": "unassessed"}]}

    def request(args):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "workspace_summary", "arguments": args}}

    responses = _run(tmp_path, [request(args) for args in (
        {"limit": 0}, {"limit": 2}, {"limit": 2, "offset": 2}, {"offset": 99},
        {"limit": True}, {"limit": -1}, {"limit": "2"}, {"limit": 21}, {"offset": -1},
    )], snapshot)
    pages = []
    for response in responses[:4]:
        assert not response["result"]["isError"]
        block, = response["result"]["content"]
        assert len(block["text"]) < 1000
        page = json.loads(block["text"])
        assert page["total_cases"] == 7
        assert page["counts"] == {"decision": 3, "executing": 2, "verified": 1, "evidence": 4}
        assert page["verified_historical_runs"] == 2
        assert page["decision_breakdown"] == {"awaiting_approval": 2, "awaiting_selection": 1}
        pages.append(page)
    assert [page["next_offset"] for page in pages] == [None, 2, 4, None]
    assert [[case["case_id"] for case in page["cases"]] for page in pages] == [
        [], ["case-0", "case-1"], ["case-2", "case-3"], []]
    assert all(response["result"]["isError"] for response in responses[4:])
    for broken in ({"cases": None}, {"runs": None}, {"cases": [{}]}):
        response, = _run(tmp_path, [request({})], {**snapshot, **broken})
        assert response["result"]["isError"]
