"""Tool trace snapshots retain the values delivered at dispatch time."""
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts/investigation_evaluate.py"
spec = importlib.util.spec_from_file_location("receipt_review_runner", PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def make_episode(tmp_path):
    episode = object.__new__(runner.Episode)
    episode.arm = "baseline"
    episode.out = tmp_path
    episode.seconds = 3600
    episode.started = time.monotonic()
    episode.terminal = None
    episode.usage = {"queries": 0, "prepares": 0, "validations": 0}
    episode.limits = SimpleNamespace(queries=120, prepares=3)
    episode.single_prepare = episode.single_draft = False
    episode.prepare_retry_blocked = episode.draft_retry_blocked = False
    episode.trace = []
    episode.source = SimpleNamespace(query=lambda *args: {
        "records": [{"entity": "product", "id": "1", "fields": {"default_code": "A"}}],
        "complete": True, "next_cursor": None})
    episode.begin = lambda: None
    return episode


def test_early_status_receipt_does_not_change_after_later_calls(tmp_path):
    episode = make_episode(tmp_path)
    received = episode.dispatch("status", {})
    wire_snapshot = json.loads(json.dumps(received))
    episode.dispatch("query", {"kind": "record", "arguments": {"entity": "product", "id": 1}})
    persisted = json.loads((tmp_path / "trace.json").read_text())
    assert wire_snapshot["usage"]["queries"] == 0
    assert episode.usage["queries"] == 1
    assert persisted[0]["result"] == wire_snapshot
    assert received == wire_snapshot


def test_trace_captures_nested_payload_and_result_at_dispatch_time(tmp_path):
    episode = make_episode(tmp_path)
    payload = {"kind": "record", "arguments": {"entity": "product", "id": 1}}
    received = episode.dispatch("query", payload)
    expected = json.loads((tmp_path / "trace.json").read_text())[0]
    payload["arguments"]["id"] = 2
    received["records"][0]["fields"]["default_code"] = "B"
    episode.dispatch("status", {})
    persisted = json.loads((tmp_path / "trace.json").read_text())
    assert persisted[0] == expected
