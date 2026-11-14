"""Assertions shared by the disposable Docker acceptance driver and seam tests."""
from __future__ import annotations

import ast
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

GUARD = "FORGEFLOW_WORKBENCH_ISOLATED_TEST=1"
PROVIDER_READY = (
    "import socket,ssl,urllib.request; "
    "c=ssl.create_default_context(cafile='/etc/forgeflow/cert.pem'); "
    "assert urllib.request.urlopen('https://qdrant:8443/',context=c,timeout=2).read()"
    "==b'ci-fixture-not-a-model'; "
    "s=socket.create_connection(('qdrant',6333),timeout=2); "
    "c.wrap_socket(s,server_hostname='qdrant').close()"
)

SERVICES = frozenset({'setup', 'control-db', 'odoo-db', 'odoo-init', 'odoo', 'qdrant',
                      'bootstrap', 'api', 'supervisor', 'docmind', 'test-provider'})
EXCEPTION_TYPES = frozenset({
    'AssertionError', 'AttributeError', 'BootstrapError', 'CalledProcessError', 'ConfigError',
    'ConnectionError', 'EmbeddingError', 'FileNotFoundError', 'ImportError', 'IndexError',
    'InterfaceError', 'KeyError', 'ModuleNotFoundError', 'NameError', 'OSError',
    'OperationalError', 'PermissionError', 'ProgrammingError', 'QdrantError',
    'ReferencePackError', 'RuntimeError', 'SeedError', 'SemanticConfigError',
    'Odoo20Error', 'Odoo20Unavailable', 'SemanticServiceError', 'SemanticUnavailable',
    'SemanticAuthentication', 'SemanticRateLimited', 'SemanticInvalidResponse',
    'RerankError', 'IndexQuotaExceeded', 'CheckViolation', 'ForeignKeyViolation',
    'NotNullViolation', 'UniqueViolation', 'InvalidTextRepresentation', 'InvalidForeignKey',
    'TimeoutError', 'TimeoutExpired', 'TypeError', 'UndefinedColumn', 'UndefinedTable', 'ValueError',
})
ERROR_CODES = frozenset({
    'configuration_missing', 'configuration_unreadable', 'configuration_invalid',
    'configuration_locked', 'configuration_too_large', 'unsafe_configuration_permissions',
    'unsafe_configuration_path', 'valid_provider_keys_required', 'unsupported_model_profile',
    'invalid_configuration_fields', 'model_configuration_changed_retest', 'invalid_display_name',
    'dashscope_api_key_format_invalid', 'incomplete_local_tls_configuration',
    'local_single_operator_invite_required', 'local_odoo_configuration_missing',
    'local_operator_login_mismatch', 'local_operator_company_scope_invalid',
    'local_odoo_contract_incomplete', 'local_database_installation_mismatch',
    'local_operator_binding_changed', 'local_database_not_fresh',
    'local_reference_release_changed', 'local_reference_publication_incomplete',
    'unsafe_odoo_bootstrap_configuration', 'invalid_odoo_bootstrap_configuration',
    'odoo_bootstrap_database_mismatch', 'odoo_installation_mismatch',
    'odoo_operator_already_exists_without_installation', 'odoo_database_not_fresh',
    'odoo_native_addon_not_installed', 'local_admission_path_invalid',
    'local_admission_busy', 'local_admission_marker_invalid', 'local_admission_runnable_invalid',
    'embedding_unavailable', 'embedding_unauthorized', 'embedding_error',
    'embedding_invalid_response', 'embedding_count_mismatch', 'collection_missing',
    'qdrant_unavailable', 'qdrant_unauthorized', 'qdrant_error', 'distance_mismatch',
    'vector_not_on_disk', 'tenant_index_missing', 'semantic_ca_file_invalid',
})
_COMPONENTS = SERVICES | {'odoo_seed', 'odoo_init', 'doctor', 'quiesce', 'resume-admission'}
_STATES = frozenset({'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'})
_HEALTH = frozenset({'healthy', 'unhealthy', 'starting'})
_EXCEPTION = re.compile(r'(?:^|\s)(?:[A-Za-z_][A-Za-z0-9_]*\.)*('
                        + '|'.join(sorted(EXCEPTION_TYPES)) + r'):')
DIAGNOSTIC_BYTES = 65536


def _seed_attribute_names() -> frozenset[str]:
    """Only source-defined attribute names may appear in a seed diagnostic."""
    try:
        path = Path(__file__).resolve().parents[1] / 'deploy/workbench/odoo_bootstrap.py'
        tree = ast.parse(path.read_text())
        seed = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'seed')
        return frozenset(node.attr for node in ast.walk(seed) if isinstance(node, ast.Attribute))
    except (OSError, SyntaxError, StopIteration):
        return frozenset()


_SEED_ATTRIBUTES = _seed_attribute_names()


def safe_diagnostics(states: str, log_tail: str) -> dict:
    """Project closed enums/source locations, never raw messages or identifiers."""
    report = {'services': [], 'errors': [], 'service_state_available': False}
    try:
        if len(states.encode()) > DIAGNOSTIC_BYTES:
            raise ValueError
        try:
            rows = json.loads(states)
            rows = rows if isinstance(rows, list) else [rows]
        except ValueError:
            rows = [json.loads(line) for line in states.splitlines() if line.strip()]
        if len(rows) > 64:
            raise ValueError
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get('Service')
            if not isinstance(name, str) or name not in SERVICES or name in seen:
                continue
            seen.add(name)
            item = {'service': name}
            if isinstance(row.get('State'), str) and row['State'] in _STATES:
                item['state'] = row['State']
            if isinstance(row.get('Health'), str) and row['Health'] in _HEALTH:
                item['health'] = row['Health']
            if type(row.get('ExitCode')) is int and 0 <= row['ExitCode'] <= 255:
                item['exit_code'] = row['ExitCode']
            report['services'].append(item)
        report['service_state_available'] = bool(report['services'])
    except (ValueError, TypeError, RecursionError):
        report['services'] = []
    report['services'].sort(key=lambda item: item['service'])

    errors = []
    for line in log_tail[-DIAGNOSTIC_BYTES:].splitlines()[-120:]:
        item = {}
        try:
            start = line.index('{')
            row = json.loads(line[start:])
            if isinstance(row, dict) and row.get('ok') is False:
                component = row.get('component') or row.get('stage')
                if isinstance(component, str) and component in _COMPONENTS:
                    item['component'] = component
                for key in ('error', 'error_type'):
                    value = row.get(key)
                    if isinstance(value, str) and value in ERROR_CODES:
                        item['code'] = value
                    elif isinstance(value, str) and value in EXCEPTION_TYPES:
                        item['type'] = value
                if item.get('component') == 'odoo_seed':
                    location = row.get('error_line')
                    if type(location) is int and 1 <= location <= 10000:
                        item['error_line'] = location
                    attribute = row.get('error_attribute')
                    if (item.get('type') == 'AttributeError' and isinstance(attribute, str)
                            and attribute in _SEED_ATTRIBUTES):
                        item['error_attribute'] = attribute
        except (ValueError, TypeError, RecursionError):
            pass
        match = _EXCEPTION.search(line)
        if match:
            item['type'] = match.group(1)
        if ('type' in item or 'code' in item) and item not in errors:
            errors.append(item)
    report['errors'] = errors[-12:]
    return report


def collect_diagnostics(compose: list[str], log_path: Path) -> dict:
    """Read only this acceptance project's states and the bounded private log tail."""
    states, tail = '', ''
    try:
        result = subprocess.run([*compose, 'ps', '-a', '--format', 'json'],
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            states = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with log_path.open('rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - DIAGNOSTIC_BYTES))
            tail = handle.read(DIAGNOSTIC_BYTES).decode('utf-8', errors='replace')
    except OSError:
        pass
    report = safe_diagnostics(states, tail)
    # Compose aggregates per-service tails. A verbose Odoo startup can push a
    # short bootstrap error outside the aggregate byte/line bound. Read failed
    # services individually, still bounded and without exporting their raw log.
    failures = [item['service'] for item in report['services']
                if item.get('exit_code', 0) != 0 or item.get('health') == 'unhealthy']
    errors = []
    for service in failures:
        try:
            with tempfile.TemporaryFile() as private:
                result = subprocess.run([*compose, 'logs', '--no-color', '--tail', '20', service],
                                        stdout=private, stderr=subprocess.DEVNULL, timeout=15)
                if result.returncode:
                    continue
                private.seek(0, 2)
                private.seek(max(0, private.tell() - DIAGNOSTIC_BYTES))
                raw = private.read(DIAGNOSTIC_BYTES).decode('utf-8', errors='replace')
            for item in safe_diagnostics('', raw)['errors']:
                item.setdefault('component', service)
                if item not in errors:
                    errors.append(item)
        except (OSError, subprocess.TimeoutExpired):
            continue
    if errors:
        report['errors'] = errors[-12:]
    return report


def checked_json(raw: str, *, marker: str | None = None) -> dict:
    """Require explicit structured success; exit-zero alone is insufficient."""
    if marker:
        matches = [line.removeprefix(marker) for line in raw.splitlines() if line.startswith(marker)]
        if len(matches) != 1:
            raise ValueError("probe_marker_missing_or_ambiguous")
        raw = matches[0]
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise ValueError("probe_did_not_confirm_success")
    return value


def exercise_stack(run, result: dict, *, sleep=time.sleep) -> None:
    """Run against already started services. Injectable process seam holds no keys."""
    def stage(name):
        result["stage"] = name
        print(f"Local acceptance: {name}", flush=True)

    def runtime(command, *, service="supervisor", extra=()):
        return checked_json(run(["exec", "-T", service, "python", "-m",
                                "forgeflow.local_workbench.runtime", command, *extra], capture=True).stdout)

    def doctor():
        for attempt in range(30):
            try:
                value = runtime("doctor", service="api")
                if value.get("checks") and all(item is True for item in value["checks"].values()):
                    return value
            except (subprocess.CalledProcessError, ValueError):
                pass
            if attempt < 29:
                sleep(2)
        raise RuntimeError("runtime_doctor_failed")

    def probe(name):
        if name in {"seed", "state"}:
            command = ["exec", "-T", "-e", GUARD, "odoo", "odoo", "shell", "-c",
                       "/etc/forgeflow/odoo.conf", "--no-http"]
            raw = run(command, source=f"exec(open('/acceptance/{name}_probe.py').read())\n",
                      capture=True).stdout
        else:
            raw = run(["exec", "-T", "-e", GUARD, "api", "python",
                       f"/acceptance/workbench_{name}_probe.py"], capture=True).stdout
        value = checked_json(raw, marker=f"FORGEFLOW_WORKBENCH_{name.upper()}_PROBE=")
        if name in {"state", "control"}:
            assert value.get("read_only") is True
            tables = value.get("tables", {})
            assert len(tables) >= (24 if name == "state" else 11)
            assert all(isinstance(row.get("count"), int) and row["count"] >= 0
                       and isinstance(row.get("digest"), str) and len(row["digest"]) == 32
                       for row in tables.values())
        return value

    def repeated_bootstrap():
        value = checked_json(run(["run", "--rm", "--no-deps", "-T", "bootstrap"], capture=True).stdout)
        if (value.get("reference_indexed_this_run") is not False
                or value.get("reference_documents") != 12 or value.get("migrations_applied") != 0):
            raise ValueError("bootstrap_was_not_a_noop")

    stage("doctor_and_native_seed")
    result["doctor_before"] = doctor()
    seed = probe("seed")
    assert seed.get("real_odoo") is True and seed.get("repeat_seed_preserved_tables", 0) >= 20
    assert seed.get("foreign_installation_rejected") is True
    assert seed.get("synthetic_roles_and_business_verified") is True
    assert seed.get("recovery_approvals_created") == seed.get("recovery_executions_created") == 0
    result["native_seed"] = seed
    result["checks"]["native_seed_idempotent"] = True
    stage("http_reads_and_no_business_write")
    native_before, control_before = probe("state"), probe("control")
    result["http"] = probe("http")
    assert result["http"].get("documents") == 12 and result["http"].get("blocks", 0) >= 12
    assert result["http"].get("erp_mutation_requests") == result["http"].get("real_provider_requests") == 0
    assert result["http"].get("checks") and all(v is True for v in result["http"]["checks"].values())
    assert probe("state") == native_before and probe("control") == control_before
    result["checks"]["http_reads_preserve_native_and_catalog_state"] = True
    repeated_bootstrap()
    assert probe("state") == native_before and probe("control") == control_before
    result["checks"]["bootstrap_preserves_initial_data"] = True
    stage("explicit_synthetic_recovery")
    runtime("quiesce")
    idle = runtime("doctor", extra=("--require-idle",))
    assert idle.get("idle") is True and idle.get("admission_locked") is True
    result["recovery"] = probe("recovery")
    assert result["recovery"].get("checks") and all(v is True for v in result["recovery"]["checks"].values())
    assert result["recovery"].get("approved_plans") == 1 and result["recovery"].get("actions", 0) > 0
    assert result["recovery"].get("real_provider_requests") == 0
    result["checks"]["explicit_http_approval_execution_and_readback"] = True
    stage("safe_stop_and_resume")
    # Stop ingress before idle certification. Workers receive unlimited grace.
    control_before = probe("control")
    run(["stop", "-t", "-1", "api"])
    runtime("doctor", extra=("--require-idle",))
    native_before = probe("state")
    run(["stop", "-t", "-1", "supervisor", "docmind"])
    run(["up", "-d", "--wait", "--wait-timeout", "300", "api", "supervisor", "docmind"])
    runtime("resume-admission")
    result["doctor_after"] = doctor()
    assert probe("state") == native_before and probe("control") == control_before
    repeated_bootstrap()
    assert probe("state") == native_before and probe("control") == control_before
    result["http_after_restart"] = probe("http")
    assert result["http_after_restart"]["catalog_sha256"] == result["http"]["catalog_sha256"]
    assert probe("state") == native_before and probe("control") == control_before
    result["checks"]["stop_resume_and_bootstrap_preserve_data"] = True
    result["stage"] = "complete"
    result["ok"] = all(value is True for value in result["checks"].values())
