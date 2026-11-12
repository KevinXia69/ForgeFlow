"""The investigation endpoint cannot bypass admission or acquire ERP write authority."""
from pathlib import Path

import pytest

from forgeflow.executor.handlers import RecoveryHandlers
from forgeflow.executor.main import config_from_args
from forgeflow.executor.protocol import ProtocolError, validate_envelope


def test_raw_prepare_and_apply_are_disabled_before_connection(tmp_path):
    def forbidden():
        pytest.fail("bypass reached ERP")

    handlers = RecoveryHandlers(connection_factory=forbidden, store=tmp_path / "store",
                                bundle=tmp_path / "bundle", investigation_mode=True)
    with pytest.raises(ProtocolError) as error:
        handlers.prepare("run", {})
    assert error.value.reason == "investigation_validation_required"
    with pytest.raises(ProtocolError) as error:
        handlers.apply("run", "a" * 64, None)
    assert error.value.code == "approval_rejected"


def test_investigation_cannot_enable_autonomous_execution(tmp_path):
    with pytest.raises(ValueError, match="prepare_only"):
        RecoveryHandlers(connection_factory=lambda: None, store=tmp_path,
                         bundle=tmp_path, investigation_mode=True, require_grant=False)


@pytest.mark.parametrize("action", [None, [], "apply", "product", "authorize"])
def test_investigation_protocol_rejects_non_read_actions(action):
    with pytest.raises(ProtocolError):
        validate_envelope({"schema_version": 1, "op": "investigation", "run_id": "test",
                           "action": action, "payload": {}})


def test_investigation_requires_explicit_executor_flag(tmp_path):
    handlers = RecoveryHandlers(connection_factory=lambda: None, store=tmp_path,
                                bundle=tmp_path)
    with pytest.raises(ProtocolError) as error:
        handlers.investigation("run", "status", {})
    assert error.value.code == "investigation_operations_disabled"


def test_executor_rejects_missing_policy_or_autonomous_investigation():
    args = ["--socket", "/tmp/s", "--store", "/tmp/store", "--bundle", "/tmp/bundle",
            "--key", "/tmp/key", "--agent-user", "forgeflow", "--investigation"]
    with pytest.raises(SystemExit):
        config_from_args(args)
    with pytest.raises(SystemExit):
        config_from_args([*args, "--policy-file", "/tmp/policy", "--autonomous"])
    with pytest.raises(SystemExit):
        config_from_args([*args, "--policy-file", "/tmp/policy"])
    config = config_from_args([*args, "--policy-file", "/tmp/policy",
                               "--investigation-event-file", "/tmp/event"])
    assert config.investigation and config.require_grant
    assert config.policy_file == Path("/tmp/policy")
    assert config.investigation_event_file == Path("/tmp/event")
