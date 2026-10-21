"""Product verifier access must never appear in autonomous/default bundles."""
from __future__ import annotations

import builtins
import copy
import os
import pwd
import subprocess
import sys

import pytest
from test_recovery_service import system  # noqa: F401

from forgeflow.executor import main, protocol
from forgeflow.executor.handlers import RecoveryHandlers

RUN = "local-" + "1" * 32
PLAN = "a" * 64


@pytest.mark.parametrize("field,value", [
    ("container", "arbitrary"), ("command", "bash"), ("path", "/arbitrary"),
    ("approval", {}), ("request", {}), ("uid", "other-owner"),
])
def test_reconcile_wire_allows_no_caller_execution_configuration(field, value):
    payload = {"schema_version": 1, "op": "reconcile", "run_id": RUN, "plan_id": PLAN, field: value}
    with pytest.raises(protocol.ProtocolError) as error:
        protocol.validate_envelope(payload)
    assert error.value.code == "unknown_key"


@pytest.mark.parametrize("autonomous", [False, True])
def test_default_handler_cannot_import_product_or_connect_to_erp(tmp_path, monkeypatch, autonomous):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("forgeflow.product"):
            pytest.fail("default/autonomous verifier path imported product code")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    handler = RecoveryHandlers(
        connection_factory=lambda: pytest.fail("disabled verifier must not connect"),
        store=tmp_path / "store", bundle=tmp_path / "bundle", require_grant=not autonomous,
    )
    with pytest.raises(protocol.ProtocolError) as error:
        handler.reconcile(RUN, PLAN)
    assert error.value.code == "product_reconciliation_disabled"


def test_autonomous_rejects_even_injected_product_verifier(tmp_path):
    handler = RecoveryHandlers(connection_factory=lambda: pytest.fail("must not connect"),
                               store=tmp_path / "store", bundle=tmp_path / "bundle",
                               require_grant=False, product_verifier=lambda: pytest.fail("must not verify"))
    with pytest.raises(protocol.ProtocolError) as error:
        handler.reconcile(RUN, PLAN)
    assert error.value.code == "product_reconciliation_disabled"


def test_bundle_default_entrypoint_starts_without_product_package(request):
    _, _, options, _ = request.getfixturevalue("system")
    bundle = options["bundle"]
    assert not (bundle / "forgeflow/product").exists()
    code = """
import os, pathlib, pwd, sys
sys.path.insert(0, sys.argv[1])
from forgeflow.executor.main import ExecutorConfig, build_server
p = pathlib.Path(sys.argv[1]).parent
c = ExecutorConfig(socket_path=p/'test.sock', store=p/'store', bundle=pathlib.Path(sys.argv[1]),
                   key_path=p/'key', agent_user=pwd.getpwuid(os.getuid()).pw_name,
                   require_grant=False)
s = build_server(c)
assert s.handlers.reconcile is None
assert not any(n.startswith('forgeflow.product') for n in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(bundle)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_product_verifier_configuration_rejects_autonomous_launch(tmp_path):
    args = ["--socket", str(tmp_path / "s"), "--store", str(tmp_path / "store"),
            "--bundle", str(tmp_path / "bundle"), "--key", str(tmp_path / "key"),
            "--agent-user", pwd.getpwuid(os.getuid()).pw_name,
            "--product-verifier-container", "fixed-product-container"]
    config = main.config_from_args(args)
    assert config.product_verifier_container == "fixed-product-container"
    with pytest.raises(SystemExit):
        main.config_from_args(args + ["--autonomous"])
    unsafe = copy.copy(config)
    object.__setattr__(unsafe, "require_grant", False)
    with pytest.raises(ValueError, match="product_verifier_requires_approval_mode"):
        main.build_server(unsafe)
