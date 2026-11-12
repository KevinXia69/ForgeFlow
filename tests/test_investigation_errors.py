"""Safe query feedback survives the real socket and CLI boundary."""
import base64
import json

import pytest
from investigation_support import make_case
from test_executor_daemon import handlers as handlers
from test_executor_daemon import serve as serve
from test_executor_daemon import workspace as workspace

from forgeflow.executor.daemon import Handlers
from forgeflow.executor.protocol import ProtocolError
from forgeflow.investigation import client
from forgeflow.investigation.sources import SourceError, source_error_feedback


def test_business_name_is_rejected_as_internal_id_with_actionable_safe_feedback():
    source, _, _ = make_case()
    with pytest.raises(SourceError) as error:
        source.query("record", {"entity": "sales_order", "id": "SO-30"})
    feedback = source_error_feedback(error.value.code)
    assert feedback["reason"] == "numeric_record_id_required"
    assert "entities" in feedback["hint"]


def test_numeric_id_feedback_survives_socket_and_cli(serve, handlers, monkeypatch, capsys):
    def query_error(*_args):
        raise ProtocolError("invalid_field", 400, reason="numeric_record_id_required")
    base, _ = handlers
    server = serve(custom=Handlers(base.prepare, base.apply, base.status, investigation=query_error))
    monkeypatch.setenv("FORGEFLOW_EXECUTOR_SOCKET", str(server.socket_path))
    monkeypatch.setenv("FORGEFLOW_RUN_ID", "query-test")
    payload = {"kind": "policy", "arguments": {"order_id": "SO-30"}}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    assert client.main(["query", "--payload-base64", encoded]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == source_error_feedback("numeric_record_id_required")


def test_unrecognized_server_reason_never_leaks_diagnostics(serve, handlers, monkeypatch, capsys):
    def query_error(*_args):
        raise ProtocolError("invalid_field", 400, reason="private_business_answer")
    base, _ = handlers
    server = serve(custom=Handlers(base.prepare, base.apply, base.status, investigation=query_error))
    monkeypatch.setenv("FORGEFLOW_EXECUTOR_SOCKET", str(server.socket_path))
    monkeypatch.setenv("FORGEFLOW_RUN_ID", "query-test")
    encoded = base64.b64encode(b'{"kind":"policy","arguments":{}}').decode()
    assert client.main(["query", "--payload-base64", encoded]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "tool_error", "code": "invalid_field"}
