"""Command-seam regressions; these deliberately never start Docker or a provider."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

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


@pytest.mark.parametrize('ndjson', [False, True])
def test_diagnostics_keep_only_closed_service_and_error_fields(ndjson):
    secret = 'synthetic-credential'
    rows = [
        {'Service': 'odoo-init', 'State': 'exited', 'ExitCode': 1, 'Health': '',
         'Command': secret, 'Labels': {'api_key': secret}, 'Name': secret},
        {'Service': 'api', 'State': 'running', 'Health': 'healthy', 'ExitCode': 0},
        {'Service': secret, 'State': 'running', 'ExitCode': 0},
        {'Service': ['api'], 'State': 'running', 'ExitCode': 0},
    ]
    states = '\n'.join(json.dumps(row) for row in rows) if ndjson else json.dumps(rows)
    log = '\n'.join([
        'odoo-init-1 | ' + json.dumps({'ok': False, 'component': 'odoo_seed',
             'error': 'odoo_native_addon_not_installed', 'message': secret, 'body': secret}),
        'odoo-init-1 | ValueError: ' + secret,
        json.dumps({'ok': False, 'component': secret, 'error': secret}),
        json.dumps({'ok': False, 'component': 'api', 'error': {'api_key': secret}}),
        json.dumps({'ok': True, 'error': 'local_database_not_fresh'}),
        'Authorization: Bearer ' + secret,
    ])
    result = module.safe_diagnostics(states, log)
    assert result == {'services': [
        {'service': 'api', 'state': 'running', 'health': 'healthy', 'exit_code': 0},
        {'service': 'odoo-init', 'state': 'exited', 'exit_code': 1}],
        'errors': [{'component': 'odoo_seed', 'code': 'odoo_native_addon_not_installed'},
                   {'type': 'ValueError'}], 'service_state_available': True}
    assert secret not in json.dumps(result)


@pytest.mark.parametrize('states', ['', 'invalid-json', '[null, 42]', 'x' * 70000])
def test_invalid_diagnostics_never_replace_failure_with_untrusted_data(states):
    result = module.safe_diagnostics(states, 'synthetic-credential')
    assert result == {'services': [], 'errors': [], 'service_state_available': False}


def test_diagnostics_reject_untyped_state_values_and_bound_old_messages():
    secret = 'synthetic-credential'
    states = json.dumps([{'Service': 'api', 'State': secret, 'ExitCode': True,
                         'Health': {'detail': secret}}] * 20)
    log = 'ModuleNotFoundError: ' + secret + '\n' + 'x' * 70000 + '\nPermissionError: ' + secret
    result = module.safe_diagnostics(states, log)
    assert result['services'] == [{'service': 'api'}]
    assert result['errors'] == [{'type': 'PermissionError'}]
    assert secret not in json.dumps(result)


def test_diagnostics_collection_uses_only_given_project_and_bounded_private_tail(tmp_path, monkeypatch):
    log = tmp_path / 'private.log'
    log.write_bytes(b'synthetic-credential\n' * 20000 + b'RuntimeError: synthetic-credential\n')
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs['timeout'] == 15
        if 'logs' in command:
            assert kwargs['stderr'] is module.subprocess.DEVNULL
            kwargs['stdout'].write(b'RuntimeError: synthetic-credential\n')
            return SimpleNamespace(returncode=0)
        assert kwargs['capture_output'] is True
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {'Service': 'bootstrap', 'State': 'exited', 'ExitCode': 1}))
    monkeypatch.setattr(module.subprocess, 'run', run)
    compose = ['docker', 'compose', '-p', 'isolated-test', '-f', '/tmp/isolated-compose.yaml']
    result = module.collect_diagnostics(compose, log)
    assert commands == [[*compose, 'ps', '-a', '--format', 'json'],
                        [*compose, 'logs', '--no-color', '--tail', '20', 'bootstrap']]
    assert result['errors'] == [{'type': 'RuntimeError', 'component': 'bootstrap'}]
    assert result['services'] == [{'service': 'bootstrap', 'state': 'exited', 'exit_code': 1}]
    assert 'synthetic-credential' not in json.dumps(result)


def test_failed_service_diagnostic_survives_chatty_aggregate_and_keeps_only_known_errors(tmp_path, monkeypatch):
    log = tmp_path / 'private.log'
    log.write_text('bootstrap | {"ok":false,"error":"SemanticInvalidResponse"}\n' + 'x' * 70000)
    states = [{'Service': 'bootstrap', 'State': 'exited', 'ExitCode': 1},
              {'Service': 'odoo', 'State': 'running', 'Health': 'healthy', 'ExitCode': 0}]
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if 'ps' in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps(states))
        assert command[-1] == 'bootstrap'
        kwargs['stdout'].write(b'x' * 70000 + json.dumps({
            'ok': False, 'component': 'bootstrap', 'error': 'SemanticInvalidResponse',
            'message': 'synthetic-credential', 'env': {'API_KEY': 'synthetic-credential'},
        }).encode() + b'\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    result = module.collect_diagnostics(['docker', 'compose', '-p', 'isolated-test'], log)
    assert result['errors'] == [{'component': 'bootstrap', 'type': 'SemanticInvalidResponse'}]
    assert len(commands) == 2
    assert 'synthetic-credential' not in json.dumps(result)


def test_seed_diagnostics_retain_source_location_but_not_unknown_identifiers():
    rows = [
        {'ok': False, 'component': 'odoo_seed', 'error': 'AttributeError',
         'error_line': 83, 'error_attribute': 'context', 'message': 'synthetic-credential'},
        {'ok': False, 'component': 'odoo_seed', 'error': 'AttributeError',
         'error_line': True, 'error_attribute': 'synthetic_private_credential'},
        {'ok': False, 'component': 'api', 'error': 'AttributeError',
         'error_line': 12, 'error_attribute': 'context'},
        {'ok': False, 'component': 'odoo_seed', 'error': 'ValueError',
         'error_line': 10001, 'error_attribute': 'context'},
    ]
    result = module.safe_diagnostics('', '\n'.join(json.dumps(row) for row in rows))
    assert result['errors'] == [
        {'component': 'odoo_seed', 'type': 'AttributeError', 'error_line': 83,
         'error_attribute': 'context'},
        {'component': 'odoo_seed', 'type': 'AttributeError'},
        {'component': 'api', 'type': 'AttributeError'},
        {'component': 'odoo_seed', 'type': 'ValueError'},
    ]
    assert 'synthetic' not in json.dumps(result)


def test_minimal_odoo_probe_reuses_pins_without_exposing_other_services(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('accept_local_odoo', ROOT / 'scripts/accept_local_odoo.py')
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    source = yaml.safe_load((ROOT / 'deploy/workbench/compose.yaml').read_text())
    original = json.dumps(source, sort_keys=True)
    result = driver.odoo_only_compose(source, 'local-tested-image', tmp_path)
    assert set(result['services']) == {'odoo-db', 'odoo-init', 'odoo'}
    assert result['services']['odoo-db']['image'] == source['services']['odoo-db']['image']
    assert '@sha256:' in result['services']['odoo-db']['image']
    assert all('ports' not in service for service in result['services'].values())
    assert set(result['volumes']) == {'odoo-db', 'odoo-data'}
    assert result['networks'] == {'backend': source['networks']['backend']}
    assert result['services']['odoo-init']['depends_on'] == {'odoo-db': {'condition': 'service_healthy'}}
    assert result['services']['odoo']['image'] == result['services']['odoo-init']['image'] == 'local-tested-image'
    assert json.dumps(source, sort_keys=True) == original
