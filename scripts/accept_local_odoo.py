#!/usr/bin/env python3
"""Fast disposable Odoo/PG first-install probe, without an application image.

Uses the actual installer configuration writer, pinned Compose PostgreSQL image,
Odoo initializer and seed/idempotence probe. No provider or production access.
Only the bounded receipt is suitable for uploading; raw logs stay private.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import yaml
from local_stack_acceptance import EXCEPTION_TYPES, GUARD, checked_json, collect_diagnostics

ROOT = Path(__file__).resolve().parents[1]


def odoo_only_compose(source: dict, image: str, temporary: Path) -> dict:
    services = {name: copy.deepcopy(source['services'][name])
                for name in ('odoo-db', 'odoo-init', 'odoo')}
    for service in services.values():
        service.pop('ports', None)
        service['restart'] = 'no'
    services['odoo-db']['volumes'] = [
        'odoo-db:/var/lib/postgresql/data', f'{temporary / "odoo-secret"}:/run/forgeflow:ro']
    for name in ('odoo-init', 'odoo'):
        services[name]['image'] = image
        services[name]['volumes'] = [
            f'{temporary / "odoo-config"}:/etc/forgeflow:ro', 'odoo-data:/var/lib/odoo']
    services['odoo']['volumes'].append(
        f'{ROOT / "tests/integrations/workbench_seed_probe.py"}:/acceptance/seed_probe.py:ro')
    return {'services': services, 'volumes': {'odoo-db': {}, 'odoo-data': {}},
            'networks': {'backend': copy.deepcopy(source['networks']['backend'])}}


def main() -> int:
    from forgeflow.local_workbench.config import save_configuration

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--odoo-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.output.chmod(0o700)
    endpoint = os.environ.get('DOCKER_HOST', '')
    if os.environ.get('DOCKER_CONTEXT') or not endpoint:
        endpoint = subprocess.check_output(
            ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    if not endpoint.startswith(('unix://', 'npipe://')):
        raise RuntimeError('disposable_acceptance_requires_local_docker')
    command = ['docker', 'compose']
    if subprocess.run([*command, 'version'], capture_output=True).returncode:
        if not shutil.which('docker-compose'):
            raise RuntimeError('docker_compose_required')
        command = ['docker-compose']
    result = {'ok': False, 'scope': 'disposable_real_odoo_first_install_and_seed',
              'commit': os.environ.get('GITHUB_SHA'), 'stage': 'generate_configuration',
              'real_provider_requests': 0, 'application_stack_tested': False, 'checks': {}}
    with tempfile.TemporaryDirectory(prefix='forgeflow-odoo-diagnostic-') as directory:
        temporary = Path(directory)
        mounts = {'control': temporary / 'control-secret', 'odoo': temporary / 'odoo-secret',
                  'odoo_config': temporary / 'odoo-config', 'qdrant': temporary / 'qdrant-config'}
        save_configuration({
            'profile': 'deepseek_flash', 'model_api_key': 'ci-model-fixture-key',
            'dashscope_api_key': 'sk-ci-parser-fixture-key',
            'siliconflow_api_key': 'ci-embedding-fixture-key', 'display_name': 'CI synthetic operator',
        }, path=temporary / 'config/runtime.json', mounts=mounts)
        # The outer temporary directory stays 0700. Only these two directories
        # are bind-mounted. File permissions remain the production 0640/0444.
        mounts['odoo'].chmod(0o755)
        mounts['odoo_config'].chmod(0o755)
        config = odoo_only_compose(yaml.safe_load(
            (ROOT / 'deploy/workbench/compose.yaml').read_text()), args.odoo_image, temporary)
        compose_path = temporary / 'compose.yaml'
        compose_path.write_text(yaml.safe_dump(config, sort_keys=False))
        project = 'forgeflow-odoo-test-' + uuid.uuid4().hex[:12]
        compose = [*command, '-p', project, '-f', str(compose_path)]
        log_path = args.output / 'containers.log'
        with log_path.open('w') as log:
            log_path.chmod(0o600)

            def run(arguments, *, source=None, capture=False):
                try:
                    return subprocess.run([*compose, *arguments], input=source, text=True,
                                          stdout=subprocess.PIPE if capture else log, stderr=log,
                                          timeout=900, check=True)
                except subprocess.CalledProcessError as error:
                    if capture and error.stdout:
                        log.write(error.stdout)
                        log.flush()
                    raise

            try:
                # This one-shot root process can write only the fresh two-file
                # bind mount; it has only CHOWN, no network or host socket.
                result['stage'] = 'prepare_configuration_permissions'
                subprocess.run([
                    'docker', 'run', '--rm', '--network', 'none', '--read-only',
                    '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--user', '0',
                    '--security-opt', 'no-new-privileges:true',
                    '-v', f'{mounts["odoo_config"]}:/configuration', args.odoo_image,
                    'python3', '-c', "import os; [os.chown('/configuration/'+name,10001,10001) "
                    "for name in ('odoo.conf','bootstrap.json')]",
                ], stdout=log, stderr=log, timeout=120, check=True)
                result['stage'] = 'start_empty_odoo'
                run(['up', '-d', '--wait', '--wait-timeout', '600'])
                result['checks']['fresh_odoo_healthy'] = True
                result['stage'] = 'native_seed_and_preservation'
                probe = checked_json(run([
                    'exec', '-T', '-e', GUARD, 'odoo', 'odoo', 'shell', '-c',
                    '/etc/forgeflow/odoo.conf', '--no-http',
                ], source="exec(open('/acceptance/seed_probe.py').read())\n", capture=True).stdout,
                    marker='FORGEFLOW_WORKBENCH_SEED_PROBE=')
                assert probe.get('real_odoo') is True
                assert probe.get('repeat_seed_preserved_tables') == 21
                assert probe.get('foreign_installation_rejected') is True
                assert probe.get('synthetic_roles_and_business_verified') is True
                assert probe.get('recovery_approvals_created') == probe.get('recovery_executions_created') == 0
                result['native_seed'] = probe
                result['checks']['native_seed_idempotent'] = True
                result['stage'], result['ok'] = 'complete', True
            except Exception as error:
                name = type(error).__name__
                result['error_type'] = name if name in EXCEPTION_TYPES else 'UnexpectedError'
            finally:
                try:
                    subprocess.run([*compose, 'logs', '--no-color', '--tail', '120'],
                                   stdout=log, stderr=log, timeout=60)
                except subprocess.TimeoutExpired:
                    result['container_log_capture_timed_out'] = True
                log.flush()
                result['diagnostics'] = collect_diagnostics(compose, log_path)
                try:
                    cleanup = subprocess.run([*compose, 'down', '-v', '--remove-orphans'],
                                             stdout=log, stderr=log, timeout=120)
                    result['isolated_cleanup_ok'] = cleanup.returncode == 0
                except subprocess.TimeoutExpired:
                    result['isolated_cleanup_ok'] = False
                result['ok'] = result['ok'] and result['isolated_cleanup_ok']
                receipt = args.output / 'result.json'
                receipt.write_text(json.dumps(result, indent=2) + '\n')
                receipt.chmod(0o600)
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
