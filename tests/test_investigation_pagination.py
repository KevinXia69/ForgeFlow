"""Byte pages, view consistency and durable page formation (no provider)."""

import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from investigation_support import business_record, make_case
from test_investigation_service import RUN_ID, setup_service, state
from test_investigation_sources import ReadOnlyConnection

from forgeflow.executor.daemon import ExecutorServer, Handlers
from forgeflow.executor.protocol import MAX_FRAME_BYTES, write_frame
from forgeflow.investigation import sources
from forgeflow.investigation.event import _entities
from forgeflow.investigation.evidence import related
from forgeflow.investigation.grading import _agent_observations
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import OdooSource, SourceError
from forgeflow.investigation.transport import PAGE_BYTES, cli_response, page_size, response_sizes
from forgeflow.recovery.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]


def long_records(count=20, text="A" * 4000):
    return [business_record("purchase_order", 1000 + index, name=f"PO-LONG-{index:03}",
                            state="purchase", supplier_id="1", origin=text)
            for index in range(count)]


def long_source(count=20, text="A" * 4000):
    source, contract, _ = make_case()
    source.records.extend(long_records(count, text))
    return source, contract


def query_payload(**changes):
    return {"kind": "entities", "arguments": {"entity": "purchase_order", "query": "PO-LONG", "limit": 50}, **changes}


@pytest.mark.parametrize("text", ["A" * 4000, "中" * 4000, "\n\t\"\\" * 1000, "😀" * 4000])
def test_whole_record_pages_reconstruct_the_exact_ordered_view(text):
    source, _ = long_source(text=text)
    payload, all_rows, cursors = query_payload(), [], []
    while True:
        page = source.query(payload["kind"], payload["arguments"])
        assert page_size(page["records"], page["next_cursor"]) <= PAGE_BYTES
        assert page["records"]
        assert all(row["fields"]["origin"] == text for row in page["records"])
        all_rows.extend(page["records"])
        if page["complete"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"].startswith("v2:")
        assert page["next_cursor"] not in cursors
        cursors.append(page["next_cursor"])
        payload["arguments"]["cursor"] = page["next_cursor"]
    assert len(cursors) >= 1
    assert all_rows == long_records(text=text)


@pytest.mark.parametrize("change", ["update", "delete", "insert"])
def test_cursor_refuses_a_changed_query_view(change):
    source, _ = long_source()
    payload = query_payload()
    first = source.query(**payload)
    if change == "update":
        source.records[-1]["fields"]["origin"] = "changed"
    elif change == "delete":
        source.records.pop()
    else:
        source.records.extend(long_records(21)[-1:])
    payload["arguments"]["cursor"] = first["next_cursor"]
    with pytest.raises(SourceError, match="source_view_changed"):
        source.query(**payload)
    payload["arguments"].pop("cursor")
    assert source.query(**payload)["records"]


def test_cursor_binds_query_and_legacy_cursor_is_explicitly_rejected():
    source, _ = long_source()
    cursor = source.query(**query_payload())["next_cursor"]
    for bad in ("v1:0000000000000000:0:5", cursor):
        with pytest.raises(SourceError, match="invalid_source_arguments"):
            source.query("entities", {"entity": "purchase_order", "query": "different", "cursor": bad})


def test_more_than_twenty_small_pages_do_not_shrink_internal_scope(monkeypatch):
    source, _ = long_source(count=60, text="中" * 4000)
    monkeypatch.setattr(sources, "PAGE_BYTES", 30 * 1024)
    assert len(_entities(source, "purchase_order", "PO-LONG")) == 60
    assert len([row for row in related(source, "supplier", "1") if row["entity"] == "purchase_order"]) == 61


def test_single_oversized_record_and_view_cap_fail_explicitly(monkeypatch):
    source, _ = long_source()
    monkeypatch.setattr(sources, "PAGE_BYTES", 2000)
    with pytest.raises(SourceError, match="source_record_too_large"):
        source.query(**query_payload())
    with pytest.raises(SourceError, match="source_record_too_large"):
        source.query("record", {"entity": "purchase_order", "id": "1000"})
    monkeypatch.setattr(sources, "MAX_VIEW_RECORDS", 10)
    with pytest.raises(SourceError, match="source_view_limit"):
        source.query(**query_payload())


def test_actual_single_record_ascii_escape_expansion_cannot_be_truncated():
    source, _ = long_source(count=1, text="😀" * 4096)
    with pytest.raises(SourceError, match="source_record_too_large"):
        source.query(**query_payload())


def test_odoo_and_fixture_sources_share_byte_pages_and_cursors():
    fixture, contract = long_source(text="中" * 4000)
    connection = ReadOnlyConnection()
    connection.models["purchase.order"].extend([
        {"id": 1000 + index, "name": f"PO-LONG-{index:03}", "state": "purchase",
         "partner_id": [1, "Supplier A"], "origin": "中" * 4000} for index in range(20)])
    live = OdooSource(connection, contract)
    payload = query_payload()
    while True:
        expected = fixture.query(**payload)
        assert live.query(**payload) == expected
        if expected["complete"]:
            break
        payload["arguments"]["cursor"] = expected["next_cursor"]
    assert all(method in {"fields_get", "search", "read"} for _, method, _ in connection.calls)


def test_successful_pages_fit_actual_protocol_and_stdout_and_match_common_grader(tmp_path):
    service, _, _ = setup_service(tmp_path)
    service.source, _ = long_source(text="中" * 4000)
    payload, trace = query_payload(), []
    while True:
        response = service.dispatch(RUN_ID, "query", payload)
        stream = io.BytesIO()
        write_frame(stream, {"schema_version": 1, "ok": True, "op": "investigation", "investigation": response})
        assert len(stream.getvalue()) <= MAX_FRAME_BYTES
        assert len(cli_response(response).encode()) <= MAX_FRAME_BYTES
        assert max(response_sizes(response).values()) <= MAX_FRAME_BYTES
        trace.append({"action": "query", "payload": copy.deepcopy(payload), "result": response})
        if response["complete"]:
            break
        payload["arguments"]["cursor"] = response["next_cursor"]
    observations, _, authentic = _agent_observations(trace, len(trace), service.source)
    assert authentic and len(observations) == 20
    stored = state(service)
    assert stored["usage"]["queries"] == len(trace)
    assert len(stored["observations"]) == 20
    assert all(event["page_formed"] for event in stored["events"])


def test_unserializable_page_is_charged_but_has_no_successful_observation(tmp_path):
    service, _, _ = setup_service(tmp_path)
    # Real latest-issues metadata can exhaust the reserved header. Admission
    # checks the complete response rather than assuming the reserve is enough.
    service.dispatch(RUN_ID, "status", {})
    stored = state(service)
    stored["drafts"] = [{"validation": {"issues": [{"detail": "中" * 20000}]}}]
    atomic_json(service.root / "state.json", stored, replace=True)
    with pytest.raises(SourceError, match="query_response_too_large"):
        service.dispatch(RUN_ID, "query", query_payload(operation_id="page-1"))
    stored = state(service)
    assert stored["usage"]["queries"] == 1
    assert stored["observations"] == {}
    assert stored["events"][-1]["action"] == "query_failed"
    assert not stored.get("query_receipts")
    assert not list(service.root.glob("query-page-*.json"))


def test_lost_delivery_replays_committed_page_across_restart_without_recharge(tmp_path):
    service, _, _ = setup_service(tmp_path)
    service.source, _ = long_source()
    payload = query_payload(operation_id="retry-1")
    formed = service.dispatch(RUN_ID, "query", payload)  # Caller loses this response.
    service.source.records[-1]["fields"]["origin"] = "changed after page formation"
    restarted = InvestigationService(source=service.source, contract=service.policy, store=service.store,
        bundle=service.bundle, event=service.event, connection_factory=lambda: object(), clock=lambda: 1000)
    assert restarted.dispatch(RUN_ID, "query", payload) == formed
    assert state(restarted)["usage"]["queries"] == 1
    assert len(state(restarted)["events"]) == 1
    with pytest.raises(SourceError, match="query_operation_conflict"):
        restarted.dispatch(RUN_ID, "query", {**payload, "arguments": {**payload["arguments"], "limit": 1}})
    with pytest.raises(SourceError, match="source_view_changed"):
        restarted.dispatch(RUN_ID, "query", {"kind": "entities", "arguments": {
            **payload["arguments"], "cursor": formed["next_cursor"]}, "operation_id": "retry-2"})


def test_failed_state_commit_never_claims_an_orphan_page_was_observed(tmp_path, monkeypatch):
    service, _, _ = setup_service(tmp_path)
    save = service._save
    def interrupted(stored):
        if stored.get("query_receipts"):
            raise OSError("simulated_commit_failure")
        save(stored)
    monkeypatch.setattr(service, "_save", interrupted)
    payload = {"kind": "policy", "arguments": {}, "operation_id": "page-1"}
    with pytest.raises(OSError, match="simulated_commit_failure"):
        service.dispatch(RUN_ID, "query", payload)
    assert state(service)["observations"] == {}
    assert not state(service).get("query_receipts")
    assert len(list(service.root.glob("query-page-*.json"))) == 1
    monkeypatch.setattr(service, "_save", save)
    response = service.dispatch(RUN_ID, "query", payload)
    assert response["usage"]["queries"] == 2  # Failed attempt remains charged.
    assert len(state(service)["observations"]) == 1


def test_real_python_socket_cli_and_javascript_pages(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for real JS/Python/Unix-socket transport")
    service, _, _ = setup_service(tmp_path)
    service.source, _ = long_source(text="中\n\t\\" * 1000)
    def investigate(run_id, action, payload):
        return {"investigation": service.dispatch(run_id, action, payload)}
    handlers = Handlers(lambda *_: {}, lambda *_: {}, lambda *_: {}, investigation=investigate)
    with tempfile.TemporaryDirectory(prefix="ff-page-", dir="/tmp") as directory:
        server = ExecutorServer(Path(directory) / "s.sock", handlers, agent_uid=os.getuid(), peer_check=lambda *_: None)
        server.bind()
        stop = threading.Event()
        worker = threading.Thread(target=server.serve_forever, args=(stop,), daemon=True)
        worker.start()
        script = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
const {createInvestigationTools}=await import(pathToFileURL(process.argv[1]));
const sizes=[],operations=[];
const pi={exec:async(_python,args)=>{
  const action=args[2],encoded=args.at(-1);
  const input=JSON.parse(Buffer.from(encoded,'base64').toString());
  assert.equal(typeof input.operation_id,'string');operations.push(input.operation_id);
  const command=['-B','-m','forgeflow.investigation.client',action,'--payload-base64',encoded];
  const options={env:{...process.env,PYTHONPATH:process.argv[3],FORGEFLOW_EXECUTOR_SOCKET:process.argv[4],FORGEFLOW_RUN_ID:'episode-1'},encoding:'utf8',timeout:10000};
  const first=spawnSync(process.argv[2],command,options);
  assert.equal(first.status,0,first.stderr);sizes.push(Buffer.byteLength(first.stdout));
  // Simulate losing a response at the adapter: a transport retry resubmits the
  // exact operation, using the actual Python client and executor again.
  const retry=spawnSync(process.argv[2],command,options);
  assert.equal(retry.status,0,retry.stderr);assert.equal(retry.stdout,first.stdout);
  return {code:retry.status,killed:false,stdout:retry.stdout,stderr:retry.stderr};
}};
const tool=createInvestigationTools(pi).find(t=>t.name==='investigation_query');
const ids=[];let cursor=null;
do {const r=await tool.execute('query',{kind:'entities',arguments:{entity:'purchase_order',query:'PO-LONG',limit:50,cursor}});
 const value=r.details;assert.ok(Buffer.byteLength(r.content[0].text)<=65536);
 ids.push(...value.records.map(r=>r.id));cursor=value.next_cursor;
 assert.equal(value.complete,cursor===null);
}while(cursor);
assert.deepEqual(ids,Array.from({length:20},(_,i)=>String(1000+i)));
assert.equal(new Set(operations).size,operations.length);
assert.ok(sizes.every(n=>n<=65536));console.log(JSON.stringify({ids,pages:sizes.length,sizes}));
"""
        try:
            completed = subprocess.run([node, "--input-type=module", "-e", script,
                str(ROOT / "src/forgeflow/agents/native_investigation.js"), sys.executable,
                str(ROOT / "src"), str(server.socket_path)], capture_output=True, text=True, timeout=40)
            assert completed.returncode == 0, completed.stderr
            report = json.loads(completed.stdout)
            assert state(service)["usage"]["queries"] == report["pages"]
            assert len(state(service)["observations"]) == 20
        finally:
            stop.set()
            worker.join(timeout=3)
            server.close()
        assert not worker.is_alive()
