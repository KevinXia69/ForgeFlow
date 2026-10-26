from __future__ import annotations

import copy
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.api.evidence import ReadOnlyEvidenceProjection, snapshot_scope_key
from forgeflow.api.evidence_budget import EvidenceTurnBudget

ROOT = Path(__file__).resolve().parents[1]


def projection(tmp_path, case):
    source = {"schema_version": 1, "scope_id": "a" * 64, "cases": [case], "runs": [], "run_details": []}
    result = object.__new__(ReadOnlyEvidenceProjection)
    result.settings = SimpleNamespace(approvals_root=tmp_path)
    result.snapshot = lambda _uid: copy.deepcopy(source)
    return result, source


def mcp(path):
    spec = importlib.util.spec_from_file_location("snapshot_test_mcp", ROOT / "scripts/assistant_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.EVIDENCE = str(path)
    return module


def request(module, name="get_case", **arguments):
    return module._response({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})["result"]


def case():
    return json.loads((ROOT / "web/e2e/fixtures/approved-case.json").read_text())


def test_content_is_immutable_but_revocation_invalidates_every_prior_version(tmp_path):
    record = case()
    store, source = projection(tmp_path, record)
    first = store.publish("owner")
    original = first.read_bytes()
    assert store.publish("owner") == first
    source["cases"][0]["plan"]["actions"][0]["quantity"] = 19
    second = store.publish("owner")
    assert second != first and first.read_bytes() == original
    assert snapshot_scope_key(first) == snapshot_scope_key(second)
    assert store.is_current("owner", first)
    assert mcp(first)._call("get_case", {"case_id": record["case_id"]})["plan"]["actions"][0]["quantity"] == 3
    assert mcp(second)._call("get_case", {"case_id": record["case_id"]})["plan"]["actions"][0]["quantity"] == 19
    assert store.snapshot_case(first, record["case_id"])["plan"]["actions"][0]["quantity"] == 3
    source["cases"] = []
    assert not store.is_current("owner", first)
    for path in (first, second):
        with pytest.raises(ValueError, match="evidence_scope_changed"):
            mcp(path)._call("get_case", {"case_id": record["case_id"]})


def test_modified_content_cannot_be_served_as_the_original_snapshot(tmp_path):
    record = case()
    store, _ = projection(tmp_path, record)
    path = store.publish("owner")
    value = json.loads(path.read_text())
    value["cases"][0]["state"] = "forged"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="evidence_content_changed"):
        mcp(path)._call("get_case", {"case_id": record["case_id"]})


def test_document_content_cannot_override_its_host_selected_source_path(tmp_path):
    record = case()
    index = next(i for i, row in enumerate(record["plan"]["costs"]["lines"]) if row["action"] == "keep")
    record["plan"]["costs"]["lines"][index]["source_field"] = "another_case.private_record"
    store, _ = projection(tmp_path, record)
    result = mcp(store.publish("owner"))._call("get_case", {
        "case_id": record["case_id"], "section": "preserved_documents", "limit": 1})
    assert result["items"][0]["value"]["source_field"] == f"plan.costs.lines.{index}"


def test_brief_and_paginated_sections_distinguish_missing_empty_and_omitted(tmp_path):
    record = case()
    record["plan"]["actions"] = [{"document": f"P{index:05}", "quantity": index,
                                   "summary": "x" * 500} for index in range(120)]
    store, _ = projection(tmp_path, record)
    module = mcp(store.publish("owner"))
    brief = module._call("case_brief", {"case_id": record["case_id"]})
    assert len(json.dumps(brief).encode()) < 6000
    assert "actions" not in brief["plan"] and "actions" in brief["omitted_sections"]
    omitted = module._call("get_case", {"case_id": record["case_id"]})
    assert omitted["coverage"] == "omitted" and omitted["complete"] is False
    first = module._call("get_case", {"case_id": record["case_id"], "section": "actions", "limit": 2})
    second = module._call("get_case", {"case_id": record["case_id"], "section": "actions", "offset": first["next_offset"], "limit": 2})
    assert first["total"] == 120 and not first["complete"]
    assert second["items"][0] == {"field": "plan.actions.2", "value": record["plan"]["actions"][2]}
    preserved = module._call("get_case", {"case_id": record["case_id"], "section": "preserved_documents"})
    assert preserved["total"] == 9
    assert all(row["value"]["source_field"].startswith("plan.costs.lines.") for row in preserved["items"])
    empty, _ = projection(tmp_path / "empty", {"case_id": "empty", "plan": {"actions": []}})
    result = mcp(empty.publish("owner"))._call("get_case", {"case_id": "empty", "section": "actions"})
    assert result["total"] == 0 and result["complete"] and result["coverage"] == "available"
    missing = mcp(empty.publish("owner"))._call("get_case", {"case_id": "empty", "section": "execution"})
    assert missing["total"] is None and missing["coverage"] == "not_recorded"


@pytest.mark.parametrize("arguments", [
    {"section": "../../another-owner"}, {"section": "actions", "limit": True},
    {"section": "actions", "offset": -1}, {"section": "actions", "limit": 51},
    {"section": "actions", "max_calls": 1000}, {"offset": 0},
])
def test_model_cannot_choose_scope_files_or_reset_limits(tmp_path, arguments):
    record = case()
    store, _ = projection(tmp_path, record)
    with pytest.raises(ValueError):
        mcp(store.publish("owner"))._call("get_case", {"case_id": record["case_id"], **arguments})


def test_budget_is_host_started_bounded_and_resets_only_for_a_new_turn(tmp_path):
    record = case()
    store, _ = projection(tmp_path, record)
    path = store.publish("owner")
    module = mcp(path)
    assert request(module, "case_brief", case_id=record["case_id"])["isError"]
    lease = EvidenceTurnBudget(path, timeout=60)
    lease.start()
    try:
        for _ in range(12):
            assert request(module, "case_brief", case_id=record["case_id"])["isError"] is False
        denied = request(module, "case_brief", case_id=record["case_id"])
        assert denied["isError"] and denied["content"][0]["text"] == "evidence_budget_exhausted"
        lease.cancel()
        assert request(module, "case_brief", case_id=record["case_id"])["isError"]
    finally:
        lease.close()
    fresh = EvidenceTurnBudget(path, timeout=60)
    fresh.start()
    try:
        assert fresh.nonce != lease.nonce
        assert request(module, "case_brief", case_id=record["case_id"])["isError"] is False
        ledger = json.loads(fresh.path.read_text())
        assert ledger["calls"] == 1 and ledger["bytes"] > 0
        ledger["expires_at"] = time.time() - 1
        fresh.path.write_text(json.dumps(ledger))
        assert request(module, "case_brief", case_id=record["case_id"])["isError"]
    finally:
        fresh.close()


def test_budget_limits_cumulative_bytes_and_large_rows_are_not_silently_truncated(tmp_path):
    record = {"case_id": "large", "state": "succeeded", "memo": "x" * 20000}
    store, _ = projection(tmp_path, record)
    path = store.publish("owner")
    module = mcp(path)
    lease = EvidenceTurnBudget(path, timeout=60)
    lease.start()
    try:
        results = [request(module, case_id="large") for _ in range(8)]
        assert results[0]["isError"] is False
        assert results[-1]["isError"] is True
        assert results[-1]["content"][0]["text"] == "evidence_budget_exhausted"
        ledger = json.loads(lease.path.read_text())
        assert ledger["bytes"] <= 128 * 1024
    finally:
        lease.close()
    row = {"case_id": "one", "plan": {"actions": [{"summary": "x" * 40000}]}}
    store, _ = projection(tmp_path / "row", row)
    value = mcp(store.publish("owner"))._call("get_case", {"case_id": "one", "section": "actions"})
    assert value["coverage"] == "item_too_large" and not value["complete"]
    assert not value["items"] and value["next_offset"] is None


def test_cancelled_waiter_cannot_reset_the_active_turn_budget(tmp_path):
    store, _ = projection(tmp_path, case())
    path = store.publish("owner")
    active = EvidenceTurnBudget(path, timeout=60)
    active.start()
    before = active.path.read_bytes()
    waiting = EvidenceTurnBudget(path, timeout=60)
    try:
        with pytest.raises(TimeoutError, match="turn_queue_timeout"):
            waiting.start(queue_timeout=0)
        assert active.path.read_bytes() == before
        with pytest.raises(RuntimeError, match="cancelled"):
            waiting.start(check_cancelled=lambda: (_ for _ in ()).throw(RuntimeError("cancelled")))
        assert active.path.read_bytes() == before
    finally:
        active.close()


@pytest.mark.parametrize("adapter", ["cli", "sdk"])
def test_adapters_open_fresh_host_budget_for_each_dispatch(tmp_path, monkeypatch, adapter):
    from forgeflow.api.dsh_harness import DshHarness
    from forgeflow.api.dsh_sdk import SdkDshHarness

    record = case()
    store, _ = projection(tmp_path, record)
    path = store.publish("owner")
    module = mcp(path)  # Same process object across SDK turns.
    nonces = []

    def run(question, **_kwargs):
        nonces.append(json.loads(path.with_suffix(".budget.json").read_text())["nonce"])
        for _ in range(12):
            assert not request(module, "case_brief", case_id=record["case_id"])["isError"]
        assert request(module, "case_brief", case_id=record["case_id"])["isError"]
        return SimpleNamespace(events=[], final_response="bounded answer", session_id="test",
                               returncode=0, stdout=json.dumps({"type": "final", "text": "bounded answer"}))

    cls = DshHarness if adapter == "cli" else SdkDshHarness
    harness = cls(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    harness.evidence_path = path
    monkeypatch.setattr(harness, "configured", lambda: True)
    if adapter == "cli":
        monkeypatch.setattr(harness, "_invoke", lambda question, session_id: run(question))
    else:
        monkeypatch.setattr(harness, "_session", lambda key: SimpleNamespace(run=run))
    assert harness.ask("first", "same-chat").answer == "bounded answer"
    assert harness.ask("second", "same-chat").answer == "bounded answer"
    assert nonces[0] != nonces[1]
    assert not json.loads(path.with_suffix(".budget.json").read_text())["active"]


def test_sdk_cancel_revokes_tool_lease_until_underlying_request_finishes(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from forgeflow.api.dsh_harness import DshUnavailable
    from forgeflow.api.dsh_sdk import SdkDshHarness

    store, _ = projection(tmp_path, case())
    path = store.publish("owner")
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def run(question, **_kwargs):
        calls.append(question)
        entered.set()
        release.wait(2)
        return SimpleNamespace(events=[], final_response="done", session_id="test")

    harness = SdkDshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path,
                           queue_timeout=0.05)
    harness.evidence_path = path
    monkeypatch.setattr(harness, "configured", lambda: True)
    monkeypatch.setattr(harness, "_session", lambda key: SimpleNamespace(run=run))
    with ThreadPoolExecutor() as pool:
        running = pool.submit(lambda: list(harness.stream("first", "same", cancel_event=cancelled)))
        assert entered.wait(1)
        cancelled.set()
        assert running.result(timeout=1) == []
        assert harness.is_busy
        assert request(mcp(path), "list_cases")["isError"]
        with pytest.raises(DshUnavailable, match="turn_queue_timeout"):
            harness._reply("new conversation", "different")
        assert len(calls) == 1
        release.set()
        harness.queue_timeout = 1
        assert harness._reply("later turn", "different")[0].answer == "done"
        assert not harness.is_busy
