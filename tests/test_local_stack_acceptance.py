"""Command-seam regressions; these deliberately never start Docker or a provider."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stack_acceptance", ROOT / "scripts/local_stack_acceptance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("raw", ["", "an exit-zero shell with no marker", "MARK={}",
                                 'MARK={"ok":false}', 'MARK={"ok":1}',
                                 'MARK={"ok":true}\nMARK={"ok":true}', 'MARK=[]'])
def test_missing_ambiguous_or_unsuccessful_markers_fail_closed(raw):
    with pytest.raises(ValueError):
        module.checked_json(raw, marker="MARK=")


def test_shell_banner_is_not_a_probe_and_one_explicit_success_is_parsed():
    assert module.checked_json('shell banner\nMARK={"ok":true}\n', marker="MARK=") == {"ok": True}


class Commands:
    def __init__(self, *, fault=None):
        self.calls = []
        self.fault = fault
        self.counts = {}

    def __call__(self, command, *, source=None, **kwargs):
        self.calls.append(command)
        name = None
        if source:
            name = "seed" if "seed_probe" in source else "state"
        elif command[-1].endswith("_probe.py"):
            name = Path(command[-1]).stem.removeprefix("workbench_").removesuffix("_probe")
        if name:
            self.counts[name] = self.counts.get(name, 0) + 1
            if name == "seed":
                value = {"ok": True, "real_odoo": True, "repeat_seed_preserved_tables": 21,
                         "foreign_installation_rejected": True, "synthetic_roles_and_business_verified": True,
                         "recovery_approvals_created": 0, "recovery_executions_created": 0}
                if self.fault == "seed":
                    return SimpleNamespace(stdout="Odoo shell exited normally")
            elif name in {"state", "control"}:
                digest = "a" * 32
                if self.fault == "read_write" and name == "state" and self.counts[name] == 2:
                    digest = "b" * 32
                if self.fault == "restart_write" and name == "state" and self.counts[name] >= 5:
                    digest = "c" * 32
                value = {"ok": True, "read_only": True,
                         "tables": {str(i): {"count": 1, "digest": digest} for i in range(24)}}
                if self.fault == "empty_snapshot":
                    value["tables"] = {}
            elif name == "http":
                value = {"ok": True, "documents": 12, "blocks": 120, "checks": {"http": True},
                         "catalog_sha256": "a" * 64, "erp_mutation_requests": 0, "real_provider_requests": 0}
                if self.fault == "catalog":
                    value["documents"] = 11
            else:
                value = {"ok": True, "checks": {"recovery": True}, "approved_plans": 1,
                         "actions": 2, "real_provider_requests": 0}
                if self.fault == "recovery":
                    value["ok"] = False
            return SimpleNamespace(stdout=f"FORGEFLOW_WORKBENCH_{name.upper()}_PROBE=" + json.dumps(value))
        if command[-1] == "bootstrap":
            value = {"ok": True, "reference_indexed_this_run": False,
                     "reference_documents": 12, "migrations_applied": 0}
            if self.fault == "reindex":
                value["reference_indexed_this_run"] = True
        elif "doctor" in command:
            value = {"ok": True, "checks": {"heartbeats": True}, "idle": True, "admission_locked": True}
            if self.fault == "doctor":
                value["ok"] = False
        else:
            value = {"ok": True}
        return SimpleNamespace(stdout=json.dumps(value))


def test_complete_sequence_rechecks_doctor_bootstrap_and_state_after_restart():
    commands = Commands()
    result = {"ok": False, "checks": {}}
    module.exercise_stack(commands, result, sleep=lambda _: None)
    assert result["ok"] and result["stage"] == "complete"
    assert sum(call[-1] == "bootstrap" for call in commands.calls) == 2
    assert sum(call[-1] == "doctor" for call in commands.calls) == 2
    assert commands.counts["http"] == 2
    stops = [call for call in commands.calls if call[0] == "stop"]
    assert stops == [["stop", "-t", "-1", "api"], ["stop", "-t", "-1", "supervisor", "docmind"]]
    assert all(module.GUARD in call for call in commands.calls if "/acceptance/" in " ".join(call))


@pytest.mark.parametrize("fault", ["seed", "read_write", "restart_write", "empty_snapshot",
                                   "catalog", "recovery", "reindex", "doctor"])
def test_partial_or_mutating_execution_is_not_accepted(fault):
    result = {"ok": False, "checks": {}}
    with pytest.raises((ValueError, AssertionError, RuntimeError)):
        module.exercise_stack(Commands(fault=fault), result, sleep=lambda _: None)
    assert result["ok"] is False and result["stage"] != "complete"


def test_readiness_checks_both_tls_services_without_disabling_certificate_validation():
    assert "ssl.create_default_context" in module.PROVIDER_READY
    assert "server_hostname='qdrant'" in module.PROVIDER_READY
    assert "8443" in module.PROVIDER_READY and "6333" in module.PROVIDER_READY
    assert "CERT_NONE" not in module.PROVIDER_READY
    compile(module.PROVIDER_READY, "provider-health", "exec")


def test_probe_sources_compile_and_http_never_requests_a_model():
    for name in ("http", "recovery", "control", "state"):
        path = ROOT / f"tests/integrations/workbench_{name}_probe.py"
        compile(path.read_text(), str(path), "exec")
    http = (ROOT / "tests/integrations/workbench_http_probe.py").read_text()
    assert "/assistant/" not in http
    assert "cookie=saved_cookie" in http and "== 401" in http
